"""What a routine needs from a scheduler, and nothing more (RFC 0004 §6.2).

`quantify-scheduler` and `qblox-scheduler` expose the same gate vocabulary —
``Rxy``, ``X``, ``Reset``, ``Measure``, ``CZ``, ``IdlePulse`` — under two import
paths, and differ only in how a schedule is built and run. A routine written
against that vocabulary is therefore backend-agnostic, which is why there is one
set of routines here rather than one per tuner.

A :class:`SchedulerBackend` is the seam: it supplies the operation classes a
routine composes, and it knows how to turn a finished schedule into a dataset.
Everything backend-specific lives behind it.
"""

import logging
import math
from abc import ABC, abstractmethod
from typing import Any

import xarray as xr

from qpi_driver.tuners.base.config import DEFAULT_ROUTINE_TIMEOUT_S

log = logging.getLogger(__name__)

#: The instrument's own timeout resolution. quantify floors the wait to whole
#: minutes, so an allowance that is not a multiple of 60 waits the multiple below it.
_TIMEOUT_GRID_S = 60.0


class SchedulerBackend(ABC):
    """The scheduler operations a routine composes, plus a way to run the result.

    Subclasses bind the concrete classes from one scheduler package. The
    attribute names are deliberately the scheduler's own, so a routine reads
    like the scheduler's documentation.
    """

    #: How wide a DRAG sweep to run by default, in this backend's own units for
    #: the parameter named by ``drag_parameter``. It has to come from the backend
    #: because the two schedulers' DRAG parameters are not the same quantity:
    #: quantify's ``motzoi`` is the dimensionless ratio of the derivative
    #: component to the Gaussian, while qblox's ``beta`` is in seconds, larger by
    #: one pulse sigma. A span sized for either is meaningless for the other.
    #:
    #: Both defaults bracket the same physical value — about ``-1/(2*alpha)`` for
    #: an anharmonicity ``alpha`` in rad/s, a tenth of a ratio or a few hundred
    #: picoseconds — with room for a chip whose anharmonicity is not typical, and
    #: not so much room that the straight line the fit assumes stops holding.
    drag_span: float

    #: How far either side of its local oscillator a port can be driven, in Hz. A
    #: property of the instrument family rather than of the scheduler, but it belongs
    #: here for the same reason `drag_span` does: it is the backend that knows what its
    #: hardware reaches. Both schedulers agree at 500 MHz — quantify-scheduler's and
    #: qblox-scheduler's ``NCO_FREQ_LIMIT_STEPS / NCO_FREQ_STEPS_PER_HZ`` are both
    #: ``2e9 / 4`` — so no divergence is being modelled speculatively; the hook exists
    #: so a non-Qblox backend has somewhere to disagree.
    if_limit_hz: float = 500e6

    #: Scheduler operation classes, bound by the subclass. Named as the
    #: schedulers name them so routines stay readable.
    Schedule: Any
    Reset: Any
    Measure: Any
    Rxy: Any
    X: Any
    Y: Any
    Rz: Any
    CZ: Any
    IdlePulse: Any
    SquarePulse: Any
    #: Advances a clock's phase for everything played on it afterwards.
    #: Cumulative, so a sweep that shifts must shift back.
    ShiftClockPhase: Any
    #: Declares a clock a raw pulse can reference. Device-level clocks
    #: (``.01``, ``.ro``, ``.12``) come from the element; an edge's ``.cz``
    #: is added inside the CZ gate's own subschedule, so a routine playing a
    #: raw pulse on it has to declare it.
    ClockResource: Any

    def drag_pulse(
        self,
        *,
        amp: float,
        drag: float,
        duration: float,
        port: str,
        clock: str,
        phase_deg: float = 0.0,
    ) -> Any:
        """A Gaussian pulse with a derivative quadrature, as a *raw* pulse.

        A method rather than a bound class, because the two schedulers disagree about
        both the names and the units: quantify takes ``G_amp``/``D_amp`` where the
        second is a dimensionless ratio, qblox takes ``amplitude``/``beta`` where the
        second is in seconds — the same divergence :attr:`drag_span` exists for. A
        routine sweeping *drag* therefore sweeps this backend's own units.

        Raw, because the EF transition has no gate: `Rxy` is built for the ``.01``
        clock, and a shaped pulse on ``.12`` has to be assembled. It carries a phase
        of its own, which is what a `SquarePulse` lacks — and that lack is why an EF
        Ramsey could not be built out of phase advances.
        """
        raise NotImplementedError

    BinMode: Any

    @abstractmethod
    def new_schedule(self, name: str, repetitions: int = 1) -> Any:
        """An empty schedule this backend's compiler will accept."""

    @abstractmethod
    def run(
        self, schedule: Any, timeout_s: float = DEFAULT_ROUTINE_TIMEOUT_S
    ) -> xr.Dataset:
        """Compile, execute and retrieve *schedule* as an acquisition dataset.

        *timeout_s* bounds the wait on the instruments, and the DAG passes the
        routine's own ``routine_timeout_s``. It has to be passed down rather than
        checked afterwards: the wait blocks, so a ceiling applied to the elapsed
        time once it returns cannot interrupt a sequencer that never stops.

        The default is that setting's own default, so a routine running its own
        acquisition loop — which sees a `RoutineConfig` and not the ceiling — waits
        as long as every other routine rather than a shorter time of its own.
        """

    #: What the last `run` was actually prepared to wait for, in seconds — see
    #: `allow`. Zero until a backend records one, which is how a backend that cannot
    #: know its schedule's duration leaves the DAG's own check at the configured
    #: ceiling, exactly as before this existed.
    last_allowance_s: float = 0.0

    #: The same, summed over every `run` since :meth:`start_accounting`. A routine that
    #: overrides `measure` runs several schedules under one ceiling, and the last one's
    #: allowance says nothing about what the ones before it were owed — a wide search
    #: followed by two narrow sweeps is three waits, and bounding the loop by the third
    #: alone fails a routine that was inside its allowance at every step.
    total_allowance_s: float = 0.0

    def start_accounting(self) -> None:
        """Begin a fresh allowance total, for one routine on one target."""
        self.total_allowance_s = 0.0

    def allow(self, timeout_s: float, expected_s: float | None) -> float:
        """The wait to give the instruments for a schedule expected to take *expected_s*.

        The ceiling exists so a sequencer that never stops cannot hang the worker for
        the life of the driver. A sweep whose *pulses* outlast it is not that: killing
        it makes ``routine_timeout_s`` a limit on how large an experiment may be, and
        the routine fails for being big rather than for being stuck. So the schedule's
        own duration raises the ceiling, and never lowers it.

        This is not an unbounded wait. The number is arithmetic on the compiled
        schedule, so a routine allowed an hour is a routine whose shots and setpoints
        ask for an hour of pulses — visible in the warning, and fixed in the config
        rather than by waiting less. A hang still ends at the allowance.

        Rounded up to the whole minute the instrument floors to, plus one for upload
        and arming: passing 130s exactly would floor to two minutes and time out
        10 seconds early, which is the same failure with a subtler cause.
        """
        allowance = float(timeout_s)
        if expected_s and expected_s > 0:
            needed = math.ceil(expected_s / _TIMEOUT_GRID_S) * _TIMEOUT_GRID_S
            needed += _TIMEOUT_GRID_S
            if needed > allowance:
                # Both numbers, because they are not the same one and the difference is
                # the whole reason this fires. 256s of pulses reads as comfortably inside
                # a 300s ceiling and is not: it rounds up to the instrument's 60s grid,
                # and a minute is added for upload and arming, so what has to fit is
                # 360s. Naming only the pulse time made the warning contradict itself.
                #
                # And the advice names the figure an operator can act on. "Bring it under
                # routine_timeout_s" is wrong by exactly the grid: with a 300s ceiling the
                # pulses have to come in under 240.
                log.warning(
                    "schedule needs %.1fs of pulses, which is %.0fs once rounded to the "
                    "instrument's %.0fs timeout grid with %.0fs for upload and arming — "
                    "more than the %.0fs ceiling, so waiting %.0fs. Lower 'shots' or the "
                    "number of setpoints to bring the pulses under %.0fs, or raise "
                    "routine_timeout_s past %.0fs",
                    expected_s,
                    needed,
                    _TIMEOUT_GRID_S,
                    _TIMEOUT_GRID_S,
                    allowance,
                    needed,
                    max(allowance - _TIMEOUT_GRID_S, 0.0),
                    needed,
                )
                allowance = needed
        self.last_allowance_s = allowance
        self.total_allowance_s += allowance
        return allowance

    def idle(self, schedule: Any, duration: float) -> None:
        """Append an idle of *duration* seconds — the delay every T1/T2 sweep needs.

        Both schedulers spell this ``IdlePulse``; it is a method rather than a
        raw class so a backend whose idle is expressed differently has one place
        to say so.
        """
        schedule.add(self.IdlePulse(duration=duration))
