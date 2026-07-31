"""The calibration backend contract: what a tuner is, and what it runs.

A :class:`Tuner` is to ``calibrate`` what an ``Executor`` is to ``process``: the
one thing a device supplies, with everything above it shared. The DAG walk, the
routines, the fitting and the write-back all live here; a tuner supplies a
:class:`~qpi_driver.tuners.base.backend.SchedulerBackend` and a device, and this
class does the rest.
"""

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from qpi_driver.tuners.base.backend import SchedulerBackend
from qpi_driver.tuners.base.config import (
    CalibrationConfig,
    ConfigError,
    MonitoringConfig,
    RoutineConfig,
)
from qpi_driver.tuners.base.dag import CalibrationDAG, utc_timestamp
from qpi_driver.tuners.base.report import (
    BenchmarkResult,
    CalibrationReport,
    RoutineResult,
)
from qpi_driver.tuners.base.routines import (
    CalibrationRoutine,
    CheckOutcome,
    RoutineError,
)
from qpi_driver.tuners.routines import all_routines, routine_names
from qpi_driver.tuners.utils.persistence import save_device_config

log = logging.getLogger(__name__)

#: Where a partial recalibration starts when the checks cannot say (RFC 0005 §8).
#:
#: This used to be the answer. It is now the *fallback*: `CalibrationDAG.diagnose`
#: asks each routine whether its parameters still hold and blames the shallowest
#: node with evidence against it, so which routines run is measured rather than
#: assumed. A routine with no check leaves its state unknown, and with no checks
#: at all every seed below is blamed — which is exactly the behaviour this
#: constant gave on its own, so adding checks can only narrow the run.
#:
#: The seeds are the qubit chain rather than the readout chain for the reason they
#: always were: re-running readout bring-up is most of the cost a partial run
#: exists to avoid. The difference now is that a readout drift is no longer
#: *invisible* — `resonator_spectroscopy` has a check, so if the readout has moved
#: the blame walks up into it and says so.
RECALIBRATION_SEEDS: tuple[str, ...] = ("qubit_spectroscopy",)

#: Retained under its RFC 0004 name; `RECALIBRATION_SEEDS` is what it became.
RECALIBRATION_ROOTS: tuple[str, ...] = RECALIBRATION_SEEDS

__all__ = [
    "Tuner",
    "RECALIBRATION_SEEDS",
    "RECALIBRATION_ROOTS",
    "SchedulerBackend",
    "CalibrationConfig",
    "ConfigError",
    "MonitoringConfig",
    "RoutineConfig",
    "CalibrationDAG",
    "CalibrationReport",
    "BenchmarkResult",
    "RoutineResult",
    "CalibrationRoutine",
    "CheckOutcome",
    "RoutineError",
]


class Tuner(ABC):
    """Runs calibration routines against a quantum device and updates it.

    Subclasses supply :meth:`backend` and :attr:`device`; the three entry points
    below — full calibration, partial recalibration and the drift check — are
    the same DAG walk over different subsets, so they are implemented once here.
    """

    def __init__(self, name: str, **kwargs: Any) -> None:
        self.name = name
        self._device_config_path: Path | None = None

    @property
    @abstractmethod
    def backend(self) -> SchedulerBackend:
        """The scheduler this tuner composes and runs schedules through."""

    @property
    @abstractmethod
    def device(self) -> Any:
        """The in-memory ``QuantumDevice`` being calibrated."""

    def routines(self) -> list[CalibrationRoutine]:
        """The routines this tuner can run. Every tuner runs the same set."""
        return all_routines()

    def calibrate(self, config: CalibrationConfig) -> CalibrationReport:
        """Walk the whole enabled DAG, then persist what it calibrated."""
        config.validate_against(routine_names())
        dag = CalibrationDAG(self.routines(), config)
        report = dag.run(self.device, self.backend, config, mode="full")
        self._persist(report)
        return report

    def recalibrate(
        self, qubits: list[str], config: CalibrationConfig
    ) -> CalibrationReport:
        """Recalibrate *qubits* only, running whichever routines the checks blame.

        Two narrowings, and both matter: this is what a drift check triggers, and
        it has to be meaningfully cheaper than a full run to be worth having.

        The targets narrow to *qubits* plus any edge touching one of them. The
        routines narrow by :meth:`CalibrationDAG.diagnose` — each routine is asked
        whether its parameters still hold, and the shallowest node with evidence
        against it is recalibrated along with everything downstream of it.
        Downstream because recalibrating a frequency invalidates the gates tuned
        against it, so re-running the blamed node alone would leave the chip in a
        worse state than not running at all.

        Where RFC 0004 assumed the boundary, this measures it. A run in which every
        check passes recalibrates nothing and says so, which is the cheapest
        possible outcome and was not previously reachable.
        """
        config.validate_against(routine_names())
        narrowed = self._narrow_to(qubits, config)
        dag = CalibrationDAG(self.routines(), narrowed)
        order, notes = dag.diagnose(
            list(RECALIBRATION_SEEDS), self.device, self.backend, narrowed
        )
        for note in notes:
            log.info("diagnose: %s", note)

        if not order:
            report = CalibrationReport(
                timestamp=utc_timestamp(),
                duration_s=0.0,
                mode="partial",
                backend=self.backend.name,
            )
            report.notes.extend(notes)
            return report

        report = dag.run(
            self.device, self.backend, narrowed, mode="partial", only=order
        )
        report.notes.extend(notes)
        self._persist(report)
        return report

    def check_fidelity(self, config: CalibrationConfig) -> CalibrationReport:
        """Run only the benchmarks, calibrating nothing.

        Returns a report rather than a bare mapping so the caller emits the same
        payload shape it does for every other mode; :meth:`CalibrationReport.fidelities`
        is what reduces it to the per-target numbers a threshold is compared with.
        """
        config.validate_against(routine_names())
        routines = self.routines()
        benchmarks = [r.name for r in routines if r.is_benchmark]
        dag = CalibrationDAG(routines, config)
        order = [name for name in dag.execution_order() if name in benchmarks]
        return dag.run(
            self.device, self.backend, config, mode="fidelity_check", only=order
        )

    def _narrow_to(
        self, qubits: list[str], config: CalibrationConfig
    ) -> CalibrationConfig:
        """*config* restricted to *qubits* and the edges that touch them."""
        wanted = [q for q in qubits if q in config.target_qubits] or list(qubits)
        edges = [
            edge
            for edge in config.target_edges
            if any(part in wanted for part in edge.split("_"))
        ]
        return CalibrationConfig(
            target_qubits=wanted,
            target_edges=edges,
            routines=config.routines,
            monitoring=config.monitoring,
            routine_timeout_s=config.routine_timeout_s,
        )

    def _persist(self, report: CalibrationReport) -> None:
        """Write the calibrated device back, unless there is nothing to write.

        A run that calibrated nothing must not touch the file. A failed run is
        the case that matters: the device may hold half-applied parameters, and
        overwriting a good config with them is how a calibration takes a QPU
        down (RFC 0004 §10).
        """
        if self._device_config_path is None:
            return
        if report.status == "failed":
            log.warning(
                "not writing back device config: calibration failed (%d error(s))",
                len(report.errors),
            )
            return
        if not any(r for r in report.routine_results):
            log.info("not writing back device config: no routine produced a result")
            return

        try:
            save_device_config(self.device, self._device_config_path)
        except Exception as exc:
            log.exception("failed to persist calibrated device config")
            report.errors.append(f"write-back failed: {exc}")
            report.status = "partial_failure"

    def close(self) -> None:
        """Release instruments. Safe to call more than once."""
