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
from qpi_driver.tuners.base.dag import CalibrationDAG
from qpi_driver.tuners.base.report import (
    BenchmarkResult,
    CalibrationReport,
    RoutineResult,
)
from qpi_driver.tuners.base.routines import CalibrationRoutine, RoutineError
from qpi_driver.tuners.routines import all_routines, routine_names
from qpi_driver.tuners.utils.persistence import save_device_config

log = logging.getLogger(__name__)

__all__ = [
    "Tuner",
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
        """Recalibrate *qubits* only, running the affected routines.

        The routine subset is everything downstream of the roots — recalibrating
        a frequency invalidates the gates tuned against it — and the target
        subset is *qubits* plus any edge touching one of them. Both narrowings
        matter: this is what a drift check triggers, and it has to be
        meaningfully cheaper than a full run to be worth having.
        """
        config.validate_against(routine_names())
        narrowed = self._narrow_to(qubits, config)
        dag = CalibrationDAG(self.routines(), narrowed)
        report = dag.run(self.device, self.backend, narrowed, mode="partial")
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
