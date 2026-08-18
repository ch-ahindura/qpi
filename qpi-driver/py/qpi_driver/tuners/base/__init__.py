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

from qpi_driver.reload import ConfigFile
from qpi_driver.tuners.base.backend import SchedulerBackend
from qpi_driver.tuners.base.config import (
    CalibrationConfig,
    ConfigError,
    MonitoringConfig,
    ParallelConfig,
    RoutineConfig,
)
from qpi_driver.tuners.base.dag import CalibrationDAG, ProgressSink, utc_timestamp
from qpi_driver.tuners.base.device import component_for, has_path
from qpi_driver.tuners.base.provenance import (
    Provenance,
    ProvenanceStore,
    fit_summary,
)
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
from qpi_driver.tuners.utils.persistence import apply_device_config, save_device_config

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
    "ParallelConfig",
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
        self._watched_device_config: ConfigFile | None = None
        self.__path = None
        #: Where to report progress, set per calibration by the worker that owns the
        #: queue it reports through. An attribute rather than an argument to the
        #: three entry points below, so a tuner that overrides one of them keeps
        #: working and simply reports nothing.
        self.on_progress: ProgressSink | None = None

    # A property so that setting the path arms the watcher with it. Every tuner
    # assigns the path in its constructor; none of them should have to remember
    # a second line to make the reload work.
    @property
    def _device_config_path(self) -> Path | None:
        return self.__path

    @_device_config_path.setter
    def _device_config_path(self, path: Path | None) -> None:
        self.__path = path
        self._watched_device_config = ConfigFile(path) if path is not None else None

    @property
    @abstractmethod
    def backend(self) -> SchedulerBackend:
        """The scheduler this tuner composes and runs schedules through."""

    @property
    @abstractmethod
    def device(self) -> Any:
        """The in-memory ``QuantumDevice`` being calibrated."""

    @property
    def bias(self) -> Any:
        """Something that can park a coupler at a DC current, or ``None``.

        Only `coupler_anticrossing` asks for it, and only because the bias is not a
        pulse: it is held over qcodes for as long as the fridge is cold, so a routine
        sweeping it has to set instrument state between acquisitions. A tuner with no
        rack to talk to returns ``None`` and that routine declines.
        """
        return None

    def routines(self) -> list[CalibrationRoutine]:
        """The routines this tuner can run. Every tuner runs the same set."""
        return all_routines()

    def calibrate(self, config: CalibrationConfig) -> CalibrationReport:
        """Walk the whole enabled DAG, then persist what it calibrated."""
        self._refresh_device_config()
        config.validate_against(routine_names())
        config.validate_targets()
        dag = CalibrationDAG(self.routines(), config, bias=self.bias)
        report = dag.run(
            self.device,
            self.backend,
            config,
            mode="full",
            on_progress=self.on_progress,
            provenance=ProvenanceStore.load(self._device_config_path),
        )
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
        self._refresh_device_config()
        config.validate_against(routine_names())
        config.validate_targets()
        narrowed = self._narrow_to(qubits, config)
        dag = CalibrationDAG(self.routines(), narrowed, bias=self.bias)
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
            self.device,
            self.backend,
            narrowed,
            mode="partial",
            only=order,
            on_progress=self.on_progress,
            provenance=ProvenanceStore.load(self._device_config_path),
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
        self._refresh_device_config()
        config.validate_against(routine_names())
        config.validate_targets()
        routines = self.routines()
        benchmarks = [r.name for r in routines if r.is_benchmark]
        dag = CalibrationDAG(routines, config, bias=self.bias)
        order = [name for name in dag.execution_order() if name in benchmarks]
        return dag.run(
            self.device,
            self.backend,
            config,
            mode="fidelity_check",
            only=order,
            on_progress=self.on_progress,
            provenance=ProvenanceStore.load(self._device_config_path),
        )

    def _narrow_to(
        self, qubits: list[str], config: CalibrationConfig
    ) -> CalibrationConfig:
        """*config* restricted to *qubits*, the edges touching them, and their partners.

        The partners are not an afterthought. An edge is calibrated *through* its two
        qubits — a chevron prepares ``|11>`` with a pi pulse on each — so narrowing to
        the drifted qubit alone would carry the edge in and leave the other end
        wherever it was. `CalibrationConfig.validate_targets` refuses that outright,
        and it is right to: the failure it prevents is a confident, wrong gate rather
        than an error. So a partial recalibration that keeps an edge keeps both its
        ends, which is the smallest honest unit of work.
        """
        wanted = [q for q in qubits if q in config.target_qubits] or list(qubits)
        edges = [
            edge
            for edge in config.target_edges
            if any(part in wanted for part in edge.split("_"))
        ]
        for edge in edges:
            for part in edge.split("_"):
                if part not in wanted:
                    wanted.append(part)
        return CalibrationConfig(
            target_qubits=wanted,
            target_edges=edges,
            routines=config.routines,
            monitoring=config.monitoring,
            routine_timeout_s=config.routine_timeout_s,
        )

    def _refresh_device_config(self) -> None:
        """Re-read the device config if it changed since this tuner last looked.

        Something else may have moved the chip — a hand edit, a restored backup —
        and this tuner would otherwise calibrate from startup values and write them
        back over it.

        At DAG start, never between routines: a device moving mid-walk leaves a fit
        and the parameters it was measured against disagreeing.
        """
        if self._watched_device_config is None:
            return
        if not self._watched_device_config.changed():
            return

        path = self._watched_device_config.path
        try:
            unknown = apply_device_config(self.device, path)
        except Exception:
            log.exception(
                "could not reload %s; calibrating from what is in memory", path
            )
            return

        self._watched_device_config.mark_read()
        if unknown:
            log.warning(
                "%s names %s, which this device does not have; a new element needs a "
                "restart",
                path,
                ", ".join(unknown),
            )
        log.info("reloaded device parameters from %s before calibrating", path)

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
            return

        self._record_provenance(report)

    def _record_provenance(self, report: CalibrationReport) -> None:
        """Note which routine measured each parameter this run wrote (RFC 0008 phase 2).

        After the write-back and only after it succeeds, because provenance describes what
        is *in the file*. Recording it for a value that never reached disk would assert a
        measurement the config does not hold, and a wrong record is worse than none.

        The gate RFC 0008 §7 asks for is `RoutineResult.failed`: a parameter is
        attributable when its producer succeeded, its guards passed, and its value was
        persisted. The DAG appends a result on the failure path too, carrying the sweep a
        guard refused, and attributing a parameter to one of those would assert a
        measurement that was rejected and never written — worse than no record at all.
        """
        store = ProvenanceStore.load(self._device_config_path)
        routines = {routine.name: routine for routine in self.routines()}
        for result in report.routine_results:
            if result.failed:
                continue
            routine = routines.get(result.routine_name)
            if routine is None or not routine.updates:
                continue
            component = component_for(self.device, result.target, routine.targets)
            for path in routine.updates:
                # A declared update this element has nowhere to keep was not written:
                # several are opt-in `CalibratedTransmon` fields, and `apply` skips them.
                if component is not None and not has_path(component, path):
                    continue
                store.record(
                    result.target,
                    path,
                    Provenance(
                        routine=routine.name,
                        at=result.timestamp,
                        run=report.timestamp,
                        fit=fit_summary(result.fit),
                    ),
                )
        store.save()

    def close(self) -> None:
        """Release instruments. Safe to call more than once."""
