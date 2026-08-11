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

log = logging.getLogger(__name__)

#: The instrument plays pulses on a 1 ns grid and the compiler refuses anything
#: else, so any *time* written to a device is only usable once it is a whole number
#: of nanoseconds.
GRID_NS = 1e-9


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
        benchmark: Whether this routine's output is a gate fidelity. Declared
            rather than inferred from an empty ``updates``: T1 writes nothing
            either, and recording it as a benchmark would put a ``None``
            fidelity in front of the drift check.
    """

    name: str = ""
    depends_on: tuple[str, ...] = ()
    targets: Literal["qubits", "edges"] = "qubits"
    updates: tuple[str, ...] = ()
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
        acquisition, one fit. That covers every node in the graph but one, because
        every sweep in them is a sweep of *pulse* parameters and a schedule can hold
        those.

        A coupler's parking bias is not a pulse. It is a DC current held for as long
        as the fridge is cold, delivered out of band over qcodes — through an SPI rack
        or a cluster output, but either way not by the sequencer. So a routine that
        sweeps it has to set instrument state, run a schedule, read it, and repeat,
        which is a loop no single schedule contains.

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

    Raises:
        RoutineError: if the configured value is not a non-empty sequence. A
            sweep with no points would otherwise compile to an empty schedule
            and report success having measured nothing.
    """
    values = config.get(key, default)
    if not isinstance(values, (list, tuple)) or not values:
        raise RoutineError(
            f"{key!r} must be a non-empty list of setpoints, got {values!r}"
        )
    return list(values)


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


#: How far a fitted line must stand above the residual scatter before its centre
#: counts as a frequency — see `require_resolved_line`.
#:
#: Three above the noise, from the spread of what has actually been measured. The
#: simulated chip returns 127 and lands within 2.5 kHz of the true f01. On hardware the
#: one `qubit_spectroscopy` whose answer reproduced across runs came back at 3.55; the
#: two that did not came back at 1.56, taken through a starved readout, and 1.32,
#: which was 5 MHz out and overwrote f01 with it.
#:
#: The asymmetry is what sets it rather than the gap: a refused fit leaves the last
#: good frequency in place and says why, while an accepted one overwrites it and
#: breaks every node downstream.
MIN_LINE_SNR = 3.0


def require_resolved_line(fitted: dict[str, Any], frequencies: list[float]) -> None:
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
    off resonance, `qubit_spectroscopy` returned a 1.53 MHz line at snr 1.32 — cleared
    the width test by a factor of eleven — 5 MHz from the two runs either side of it,
    from data flat to 0.7%. It wrote that to f01, which put `ramsey_12`'s detuning
    1.5 MHz out and cost the run.

    Raises:
        RoutineError: naming the number that failed and what to change, since a
            too-narrow line wants a finer sweep and a too-shallow one wants more
            shots or a drive amplitude that shows the transition.
    """
    snr = float(fitted.get("snr", float("inf")))
    if snr < MIN_LINE_SNR:
        raise RoutineError(
            f"the fitted line stands only {snr:.2f}x above the residual scatter, "
            f"below the {MIN_LINE_SNR:g}x a measured line clears, so its centre is "
            "not a frequency — average more shots, or drive at an amplitude where "
            "the transition actually appears"
        )

    if len(frequencies) < 2:
        return
    step = abs(frequencies[1] - frequencies[0])
    linewidth = float(fitted["linewidth"])
    if linewidth < step:
        raise RoutineError(
            f"fitted linewidth {linewidth:.4g} Hz is narrower than the "
            f"{step:.4g} Hz spacing of the sweep, so the line was never "
            "measured — the fit is of the noise between setpoints. Scan the "
            "same span with more points, or narrow the span."
        )
