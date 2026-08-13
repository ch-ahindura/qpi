"""One node in the calibration DAG (RFC 0004 §6.4).

A routine is three steps over one target: build a schedule, fit the acquired
data, write the fitted parameters back to the device. It declares what it
depends on, which is what the DAG orders it by, and whether it targets qubits
or edges.

Routines are backend-agnostic — they compose operations from the
:class:`~qpi_driver.tuners.base.backend.SchedulerBackend` they are handed, so
the same routine runs under quantify-scheduler and qblox-scheduler alike.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Literal

import xarray as xr

from qpi_driver.tuners.base.backend import SchedulerBackend
from qpi_driver.tuners.base.config import DEFAULT_ROUTINE_TIMEOUT_S, RoutineConfig
from qpi_driver.tuners.fitting.core import MIN_LINE_REACH, OutOfRange

log = logging.getLogger(__name__)

#: The instrument plays pulses on a 1 ns grid and the compiler refuses anything
#: else, so any *time* written to a device is only usable once it is a whole number
#: of nanoseconds.
GRID_NS = 1e-9

#: Sweep axes that are a single number rather than a list of setpoints, so escalation
#: multiplies the number and leaves the routine to build the grid from it.
#:
#: Only ``span`` so far, and it is the one that matters: a frequency sweep centred on a
#: configured value cannot be widened by stretching its setpoints without losing the
#: centring, the resolution and the NCO band clamp all at once. See `_scalar_axis`.
SCALAR_AXES = frozenset({"span"})

#: Points in a span-based sweep when the operator names none. Shared with
#: `_frequency_sweep`, which is where the grid is actually built.
DEFAULT_SWEEP_POINTS = 51

#: The most points escalation will put in one sweep.
#:
#: Measured rather than estimated, after the first version of this got it wrong. A
#: `resonator_punchout` of 846 acquisitions compiled to **12700** Q1ASM instructions on a
#: QRM-RF — 15.0 per acquisition, against a ceiling of 12288. The rule of thumb of "roughly
#: 950 acquisitions" assumed one instruction group per point, and a frequency sweep is
#: three: `Reset`, `SetClockFrequency`, `Measure`. So 900 points is 13511 instructions and
#: over the limit, which is what this constant exists to prevent.
#:
#: 700 leaves 14% headroom at that measured rate. quantify warns rather than raising when a
#: program is too long, and the sequencer may accept it — but a program past the documented
#: maximum risks being truncated, and a truncated sweep loses its *last* setpoints, which is
#: to say the far end of the range escalation just widened to reach. Silently getting the
#: half of the sweep you already had is the worst available outcome.
MAX_SWEEP_POINTS = 700


class RoutineError(Exception):
    """A routine could not produce a usable result.

    Raised rather than returned so a failure is recorded against the routine
    that caused it. A fit that silently returns zeros would be written to the
    device as though it were a measurement (RFC 0004 §10).
    """


@dataclass
class CheckOutcome:
    """Whether a routine's parameters still hold, and by how much (RFC 0005 §8).

    Attributes:
        passed: whether the parameters are still within specification.
        margin: how far inside — or outside — expressed as a fraction of the
            tolerance the check applied. One is exactly at the limit, so 0.2
            is comfortable and 3.0 is badly out. Recorded because "failed" on
            its own does not distinguish drift from a broken instrument.
        detail: what was measured, for the report and the log.
    """

    passed: bool
    margin: float
    detail: str = ""


class CalibrationRoutine(ABC):
    """A single calibration experiment.

    Attributes:
        name: How ``calibration.yml`` and the DAG refer to this routine.
        depends_on: Routine names whose parameters this one relies on. The DAG
            is built from these, so they are the whole of the ordering.
        targets: Whether this routine runs per qubit or per edge.
        updates: Device parameters this routine writes. Empty means it measures
            without calibrating — true of a benchmark, and also of a
            characterisation like T1 that reports a number nothing is tuned from.
        reads: Device parameters this routine needs in order to measure anything,
            as dotted paths on its own target. The counterpart of ``updates``, and
            what lets the DAG decline to run a node whose input was never produced
            instead of letting it measure an uncalibrated chip and fit the noise
            (RFC 0007 §11). Declared rather than derived at run time because the
            set is static, and because the two routines that override
            :meth:`measure` have no schedule to inspect beforehand;
            ``test_a_routine_declares_every_parameter_it_reads`` derives it from
            an instrumented `read_path` and fails if a declaration is short —
            **but only for paths that go through `read_path` at all.** A gate's
            frequency and amplitude are resolved off the element by the gate
            library, and no probe can derive those: compiling reads *every*
            parameter through `generate_device_config`. So
            ``test_a_routine_playing_a_gate_declares_the_gate_parameters``
            asserts the rule instead — play a gate, declare `clock_freqs.f01`
            and, unless you supply your own amplitude, `rxy.amp180`. RFC 0007
            §11.1, where under-declaring turned one fault into eight on a chip.
        benchmark: Whether this routine's output is a gate fidelity. Declared
            rather than inferred from an empty ``updates``: T1 writes nothing
            either, and recording it as a benchmark would put a ``None``
            fidelity in front of the drift check.
    """

    name: str = ""
    depends_on: tuple[str, ...] = ()
    targets: Literal["qubits", "edges"] = "qubits"
    updates: tuple[str, ...] = ()
    reads: tuple[str, ...] = ()
    benchmark: bool = False

    @abstractmethod
    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        """Compose the schedule for this experiment over *target*."""

    @abstractmethod
    def analyse(
        self, dataset: xr.Dataset, target: str, device: Any, config: RoutineConfig
    ) -> dict[str, Any]:
        """Fit *dataset* and return the extracted parameters.

        Raises:
            RoutineError: if the data cannot be fitted, or the fit lands outside
                the range that produced it.
        """

    def apply(self, device: Any, target: str, params: dict[str, Any]) -> None:
        """Write the fitted parameters back to the in-memory device.

        The default is to write nothing, which is right for a benchmark: it
        measures a fidelity and calibrates nothing. A routine that does
        calibrate overrides this and lists what it writes in :attr:`updates`.
        """

    def build_check_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        """A short schedule testing whether this routine's parameters still hold.

        A check answers "does this parameter still hold?" without re-deriving it
        (RFC 0005 §8). It is cheap because it is a *different, simpler* experiment,
        not because it is the calibration with fewer setpoints: checking a pi pulse
        means playing the calibrated one and reading the population, which a
        narrow Rabi sweep does not do more cheaply.

        This pair mirrors `build_schedule`/`analyse` deliberately, so that the DAG
        runs both through the same seam and inherits its timeout and error
        recording. ``None`` — the default — means this routine cannot be checked,
        so its state is *unknown* rather than stale, which is what stops
        `diagnose` blaming a node it has no evidence against, and is what makes
        all of this additive.
        """
        return None

    def analyse_check(
        self, dataset: xr.Dataset, target: str, device: Any, config: RoutineConfig
    ) -> CheckOutcome:
        """Whether the parameters still hold, from the check schedule's data.

        Only called when :meth:`build_check_schedule` returned a schedule.

        Raises:
            RoutineError: if the check itself could not be evaluated. That is
                *not* evidence of drift — an unevaluable check leaves the node
                unknown, exactly as having no check does.
        """
        raise RoutineError(f"{self.name} has no check analysis")

    def measure(
        self,
        target: str,
        device: Any,
        config: Any,
        backend: Any,
        bias: Any = None,
        timeout_s: float = DEFAULT_ROUTINE_TIMEOUT_S,
    ) -> dict[str, Any]:
        """Run the whole measurement, for a routine one schedule cannot express.

        The ordinary path is `build_schedule` then `analyse`: one schedule, one
        acquisition, one fit. That covers most of the graph, because most sweeps in it
        are of *pulse* parameters at setpoints known before the run, and a schedule can
        hold those. Two nodes are not, for two unrelated reasons.

        A coupler's parking bias is not a pulse. It is a DC current held for as long
        as the fridge is cold, delivered out of band over qcodes — through an SPI rack
        or a cluster output, but either way not by the sequencer. So a routine that
        sweeps it has to set instrument state, run a schedule, read it, and repeat,
        which is a loop no single schedule contains.

        `qubit_spectroscopy`'s setpoints are not all known in advance. When no line
        turns up near the configured f01 it widens the search, and where the sweep after
        that looks depends on what the wide one found — which a single schedule cannot
        express either, since its setpoints are compiled before any acquisition runs.

        Overriding this takes that loop into the routine rather than giving every node
        a second sweep axis it does not need — which is what RFC 0005 §11 argues for,
        against the reference pipelines' `external_samplespace`. The DAG calls this
        instead of the standard path, and *bias* is whatever can park a coupler, or
        ``None`` when nothing can.

        *timeout_s* is the walk's ``routine_timeout_s``, and an override has to hand
        it to every `backend.run` it makes. It cannot be read from *config*, which is
        this routine's own `RoutineConfig` and knows nothing of the walk — so a loop
        that drops it silently bounds each of its acquisitions by the default instead
        of by what the operator set.

        Returns the same fitted parameters `analyse` would.
        """
        raise NotImplementedError

    #: How many times `escalating` may widen a sweep before giving up. Three, because
    #: each attempt is a full acquisition and the point is to cover a chip an order of
    #: magnitude from the default, not to search indefinitely: at the fourfold default
    #: step, three attempts reach 64 times the original extent.
    MAX_ESCALATIONS = 3

    def escalating(
        self,
        target: str,
        device: Any,
        config: RoutineConfig,
        backend: SchedulerBackend,
        timeout_s: float,
    ) -> dict[str, Any]:
        """Build, run and analyse, widening the sweep if the fit says the window was wrong.

        The ordinary path already refuses a curve no taller than its own noise (RFC 0007
        §6.1). For a coherence sweep that refusal is usually an instruction rather than a
        verdict — *the decay was never seen in this window* means lengthen the delays — so
        a guard that can say which axis was wrong raises `OutOfRange`, and this follows it.

        Bounded on both sides. It stops after :attr:`MAX_ESCALATIONS`, and it re-raises the
        last refusal rather than inventing a range, so a chip with no decay in it still
        fails and says why. An operator who named the axis themselves is left alone: they
        have made a statement about their chip, and widening past it would be overruling a
        measurement with a default.
        """
        # Which axes the *operator* named, captured once. Widening puts its own setpoints
        # into the config, so asking "is this axis configured?" after the first attempt
        # would find this method's own work and mistake it for an instruction.
        operator_set = frozenset(config.params)
        attempted: list[str] = []
        for attempt in range(self.MAX_ESCALATIONS + 1):
            try:
                schedule = self.build_schedule(target, device, config, backend)
                dataset = backend.run(schedule, timeout_s=timeout_s)
                return self.analyse(dataset, target, device, config)
            except OutOfRange as refusal:
                attempted.append(f"{refusal.axis} x{refusal.factor**attempt:g}")
                if attempt == self.MAX_ESCALATIONS or refusal.axis in operator_set:
                    raise
                widened = _widened(self, config, refusal)
                if widened is config:
                    raise
                config = widened
                log.info(
                    "%s on %s: %s — widening %s by %gx and trying again (%d of %d)",
                    self.name,
                    target,
                    refusal,
                    refusal.axis,
                    refusal.factor,
                    attempt + 1,
                    self.MAX_ESCALATIONS,
                )
        raise RoutineError(  # pragma: no cover - the loop above always returns or raises
            f"{self.name} exhausted its escalations on {target}: {', '.join(attempted)}"
        )

    @property
    def measures_itself(self) -> bool:
        """Whether this routine overrides :meth:`measure`."""
        return type(self).measure is not CalibrationRoutine.measure

    def applies_to(self, device: Any, target: str) -> bool:
        """Whether this routine has anything to measure on *target*.

        Defaults to yes. A routine overrides it when a target can be of a kind the
        routine simply does not describe — not uncalibrated, but *not applicable*. The
        case it was added for is a chip carrying both edge kinds: a parametric coupler
        has a CZ drive frequency to find and a DC-flux edge does not, and running
        `cz_spectroscopy` on the second is not a failure to report, it is a question
        that does not arise.

        Distinct from a missing parameter, which stays an error. `drag` raising
        because an element has nowhere to write its optimum means something is wrong;
        this means nothing is.
        """
        return True

    @property
    def has_check(self) -> bool:
        """Whether this routine overrides :meth:`build_check_schedule`."""
        return (
            type(self).build_check_schedule
            is not CalibrationRoutine.build_check_schedule
        )

    @property
    def is_benchmark(self) -> bool:
        """Whether this routine's result is a gate fidelity."""
        return self.benchmark

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name!r}>"


def setpoints_of(config: RoutineConfig, key: str, default: list[Any]) -> list[Any]:
    """Read a sweep axis from *config*, falling back to *default*.

    Numeric strings are converted, because YAML will hand us plenty of them: PyYAML only
    reads an exponent as a float when the mantissa carries a decimal point, so ``4e-9`` in
    a config file is the *string* ``"4e-9"`` while ``4.0e-9`` is a number. The two look
    identical in a file and `calibration.example.yml` shipped the first form for every time
    axis, which put strings where the schedule wanted seconds.

    Integers are left alone. ``depths`` and ``repetitions`` are counts rather than
    quantities, and turning them into floats would push them into APIs that want an ``int``.

    Raises:
        RoutineError: if the configured value is not a non-empty sequence, or if a
            setpoint is not a number. A sweep with no points would otherwise compile to
            an empty schedule and report success having measured nothing.
    """
    values = config.get(key, default)
    if not isinstance(values, (list, tuple)) or not values:
        raise RoutineError(
            f"{key!r} must be a non-empty list of setpoints, got {values!r}"
        )
    converted = []
    for value in values:
        if isinstance(value, str):
            try:
                value = float(value)
            except ValueError:
                raise RoutineError(
                    f"{key!r} setpoint {value!r} is not a number"
                ) from None
        converted.append(value)
    return converted


def linear_setpoints(start: float, stop: float, count: int) -> list[float]:
    """*count* evenly spaced points from *start* to *stop* inclusive."""
    if count < 2:
        return [start]
    step = (stop - start) / (count - 1)
    return [start + step * i for i in range(count)]


def grid_duration(seconds: float) -> float:
    """*seconds* rounded to the hardware's pulse-time grid.

    Any fit that interpolates between setpoints reports more precision than the
    instrument can play, and writing that to the device makes every *later* schedule
    fail to compile — the routine looks like it succeeded and the next one dies with
    a complaint about a time value, some way from the cause.

    Two routines have produced that bug. `cz_chevron` refines the round trip between
    its duration setpoints; `time_of_flight` fits an arrival to a fraction of a
    sample. Both are correct measurements and neither is a playable time.
    """
    return round(float(seconds) / GRID_NS) * GRID_NS


def require_resolved_line(
    fitted: dict[str, Any], frequencies: list[float], *, axis: str | None = None
) -> None:
    """Refuse a line the sweep could not have seen, or that is not above the noise.

    Two ways a Lorentzian fit reports a confident centre for a line that was never
    measured, and the centre is written straight to the device as f01 or the readout
    frequency, so both have to be refused rather than reported.

    **Too narrow for the sweep.** A line narrower than the spacing between setpoints
    did not appear in the data; whatever the fit converged on came from noise between
    the points. Seen in practice: narrowing the line to 63 kHz while the sweep still
    stepped 5 MHz made the routine report a frequency 377 MHz from the qubit, with a
    tidy fit and no complaint.

    **Too shallow to believe.** The opposite shape, and the one the width test cannot
    catch: a *broad* fit through flat data. Measured on a chip whose readout had gone
    off resonance, `qubit_spectroscopy` returned a 1.53 MHz line — clearing the width
    test by a factor of eleven — 5 MHz from the two runs either side of it, from data
    flat to 0.7%. It wrote that to f01, which put `ramsey_12`'s detuning 1.5 MHz out and
    cost the run.

    Judged on the fitted curve's own travel rather than on ``snr``; see
    :data:`MIN_LINE_REACH` for why, and for the 1800 noise fits that decided it. The two
    tests are complementary and both are needed: the width test catches a fit that
    latched onto one bin, which reach cannot, because such a fit has a large span and
    tiny residuals. Reach catches the broad shallow fit, which the width test cannot.

    *axis* names the config key a caller may change and try again with, which turns both
    refusals into an `OutOfRange` carrying the direction each one wants. They want opposite
    things — a flat window wants more spectrum, a line thinner than the grid wants more
    grid — so the direction has to travel with the refusal rather than be inferred from it.
    Left ``None`` both stay a plain `RoutineError`, which is what a caller with no sweep to
    change should see.

    Raises:
        RoutineError: naming the number that failed and what to change, since a
            too-narrow line wants a finer sweep and a too-shallow one wants more
            shots or a drive amplitude that shows the transition.
        OutOfRange: the same, when *axis* says which sweep to change.
    """
    reach = float(fitted.get("reach", float("inf")))
    if reach < MIN_LINE_REACH:
        raise _unresolved(
            f"the fitted line travels only {reach:.2f}x the scatter left around it, "
            f"below the {MIN_LINE_REACH:g}x a measured line clears, so its centre is "
            "not a frequency — average more shots, or drive at an amplitude where "
            "the transition actually appears",
            # A flat window is the one refusal here that wants *reach*: either the line is
            # somewhere else, or there is no line. Widening tries the first and stays
            # bounded, so the second still fails and still says why.
            axis=axis,
            direction="wider",
        )

    if len(frequencies) < 2:
        return
    step = abs(frequencies[1] - frequencies[0])
    linewidth = float(fitted["linewidth"])
    if linewidth < step:
        raise _unresolved(
            f"fitted linewidth {linewidth:.4g} Hz is narrower than the "
            f"{step:.4g} Hz spacing of the sweep, so the line was never "
            "measured — the fit is of the noise between setpoints. Scan the "
            "same span with more points, or narrow the span.",
            # The opposite response to the one above, which is why the direction has to
            # travel with the refusal: a line thinner than the grid needs the grid, not
            # more of the spectrum, and widening would make it worse.
            axis=axis,
            direction="finer",
        )


def _unresolved(message: str, *, axis: str | None, direction: str) -> Exception:
    """The refusal `require_resolved_line` raises: escalatable when an axis is named."""
    if axis is None:
        return RoutineError(message)
    return OutOfRange(message, axis=axis, direction=direction, factor=2.0)


def _widened(
    routine: CalibrationRoutine, config: RoutineConfig, refusal: OutOfRange
) -> RoutineConfig:
    """*config* with the axis *refusal* named stretched by its factor.

    The setpoints come from what the routine actually built rather than from the config,
    because the default case is the one that matters: a config with no ``delays`` in it is
    exactly the config whose sweep needs widening, and reading only the config would find
    nothing to stretch. Every routine keeps its setpoints as ``_<axis>`` for `analyse` to
    fit against, which is what makes this readable from outside.

    Only the setpoints move. Everything else the operator set is carried through, because
    a wider sweep is still their sweep — and the axis is stored under its own config key,
    so the next attempt reads it exactly as though it had been asked for.

    A **scalar** axis is widened rather than the setpoints it would produce, and that
    distinction is what makes this usable on a frequency sweep. Stretching a list of
    frequencies gets three things wrong at once: it anchors at the low end and reaches
    only upward, so it widens away from a resonator that sits below the window; it holds
    the point count, so a wider span steps over a narrow line; and it lands in the config
    as an explicit ``frequencies``, which `_frequency_sweep` passes through *unclamped*,
    so the next attempt asks the NCO for a frequency it cannot reach. Widening ``span``
    instead leaves centring, resolution and the band clamp where they already live.
    """
    scalar = _scalar_axis(routine, config, refusal)
    if scalar is not None:
        return scalar

    current = list(
        config.get(refusal.axis) or getattr(routine, f"_{refusal.axis}", ()) or ()
    )
    if not current:
        return config
    low, high = min(current), max(current)
    ceiling = getattr(routine, f"_{refusal.axis}_ceiling", None)
    if refusal.direction == "finer":
        # The same window, sampled harder. An aliased fringe needs resolution, not reach —
        # and lengthening the sweep would make the aliasing worse while costing more.
        stretched = linear_setpoints(low, high, int(len(current) * refusal.factor))
    elif low < 0:
        # Symmetric about its centre, and it has to be: a DRAG parameter's optimum may be
        # either sign — `drag` measured -0.1437 on the B chip — and anchoring at the centre
        # and growing upward only, as the one-sided branch below does, would put the whole
        # negative half out of reach. Widening 0.4 by 4x gave [0, 1.6] rather than
        # [-0.8, 0.8].
        centre = (high + low) / 2.0
        reach = (high - low) * refusal.factor / 2.0
        stretched = linear_setpoints(centre - reach, centre + reach, len(current))
        if ceiling is not None:
            limit = float(ceiling)
            if reach >= limit:
                return config
            stretched = linear_setpoints(
                max(centre - reach, -limit), min(centre + reach, limit), len(current)
            )
    else:
        extent = (high - low) * refusal.factor
        centre = low
        top = centre + extent
        if ceiling is not None:
            # A drive amplitude has a hardware ceiling and reaching past it does not fail
            # politely: the compiler refuses `awg_gain_0` outside [-1, 1], naming a pulse
            # rather than the routine. `rabi` starts at half scale precisely so escalation
            # can reach the rest, and said so in a comment while nothing enforced it — a
            # 4x widening of 0-0.5 asked for 0-2.0 and died at the 1.05 setpoint.
            top = min(top, float(ceiling))
            if top <= high:
                # Already at the ceiling, so there is nothing further to try. Returning the
                # config unchanged lets `escalating` re-raise instead of re-running an
                # identical sweep to get an identical refusal.
                return config
        stretched = linear_setpoints(centre, top, len(current))
    return RoutineConfig(
        enabled=config.enabled, params={**config.params, refusal.axis: stretched}
    )


def _scalar_axis(
    routine: CalibrationRoutine, config: RoutineConfig, refusal: OutOfRange
) -> RoutineConfig | None:
    """*config* with a scalar *refusal* axis multiplied out, or ``None`` if it is a list.

    ``points`` moves with ``span`` so the step size survives the widening: a resonator is
    a few hundred kHz wide and a sweep that quadruples its reach while keeping 51 points
    steps over the very line it was widened to find. Bounded by `MAX_SWEEP_POINTS`,
    because the sequencer's acquisition ceiling is real and a clamped span does not need
    the resolution an unclamped one asked for.
    """
    if refusal.axis not in SCALAR_AXES:
        return None
    # The same fallback the list branch uses, and for the same reason: the default case
    # is a config with no `span` in it, which is exactly the one needing widened.
    current = config.get(refusal.axis, getattr(routine, f"_{refusal.axis}", None))
    if current is None:
        return None

    points = config.get("points", DEFAULT_SWEEP_POINTS)
    if refusal.direction == "finer":
        # The same window, sampled harder — a line thinner than the grid needs the grid.
        # The span is left exactly as it was, so this is not a widening at all.
        return RoutineConfig(
            enabled=config.enabled,
            params={
                **config.params,
                "points": min(int(points * refusal.factor), MAX_SWEEP_POINTS),
            },
        )
    return RoutineConfig(
        enabled=config.enabled,
        params={
            **config.params,
            refusal.axis: float(current) * refusal.factor,
            # Alongside the span, so the step size survives the widening: a resonator is a
            # few hundred kHz wide and a sweep that quadruples its reach on 51 points steps
            # over the very line it was widened to find.
            "points": min(int(points * refusal.factor), MAX_SWEEP_POINTS),
        },
    )
