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

from abc import ABC, abstractmethod
from typing import Any

import xarray as xr

#: Fallback wait for one schedule when the caller names no timeout — a backend driven
#: directly, or a routine running its own acquisition loop. The DAG passes
#: ``routine_timeout_s``, which is the number an operator actually sets.
DEFAULT_ACQUISITION_TIMEOUT_S = 60


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
    def run(self, schedule: Any, timeout_s: float | None = None) -> xr.Dataset:
        """Compile, execute and retrieve *schedule* as an acquisition dataset.

        *timeout_s* bounds the wait on the instruments, and the DAG passes the
        routine's own ``routine_timeout_s``. It has to be passed down rather than
        checked afterwards: the wait blocks, so a ceiling applied to the elapsed
        time once it returns cannot interrupt a sequencer that never stops.
        """

    def idle(self, schedule: Any, duration: float) -> None:
        """Append an idle of *duration* seconds — the delay every T1/T2 sweep needs.

        Both schedulers spell this ``IdlePulse``; it is a method rather than a
        raw class so a backend whose idle is expressed differently has one place
        to say so.
        """
        schedule.add(self.IdlePulse(duration=duration))
