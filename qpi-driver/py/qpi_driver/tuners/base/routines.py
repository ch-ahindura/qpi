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
from qpi_driver.tuners.base.config import RoutineConfig

log = logging.getLogger(__name__)


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

    # --- the check form (RFC 0005 §8) -----------------------------------------
    #
    # A check answers "does this parameter still hold?" without re-deriving it.
    # It is cheap because it is a *different, simpler* experiment, not because it
    # is the calibration with fewer setpoints: checking a pi pulse means playing
    # the calibrated one and reading the population, which a narrow Rabi sweep
    # does not do more cheaply.
    #
    # The pair below mirrors `build_schedule`/`analyse` deliberately, so that the
    # DAG runs both through the same seam and inherits its timeout and error
    # recording. Returning `None` from the first means this routine has no check,
    # which is the default and is what makes all of this additive.

    def build_check_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        """A short schedule testing whether this routine's parameters still hold.

        ``None`` — the default — means this routine cannot be checked, so its
        state is *unknown* rather than stale. That distinction is what stops
        `diagnose` blaming a node it has no evidence against.
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


#: The instrument plays pulses on a 1 ns grid and the compiler refuses anything
#: else, so any *time* written to a device is only usable once it is a whole number
#: of nanoseconds.
GRID_NS = 1e-9


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
