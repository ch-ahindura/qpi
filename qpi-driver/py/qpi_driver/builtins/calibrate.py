"""Calibration expressed as a QPI driver (RFC 0004 §6.5).

A calibrate driver handles :attr:`EventType.CALIBRATE_DISPATCH` by running
calibration routines on its tuner and emitting :attr:`EventType.CALIBRATION_RESULT`
with the outcome. Calibration happens in a worker subprocess, as job execution
does, so a heavy or crashing tuner never blocks or takes down the receive loop.

Where it differs from a QPU is duration. A full DAG walk is hours, not seconds,
and that shapes three things here: the worker checks for shutdown between
routines rather than only between jobs, a routine that hangs is bounded by
``routine_timeout_s`` rather than by a per-job timeout, and the driver refuses
to queue a second calibration behind one already running.
"""

import functools
import logging
import multiprocessing
import threading
from pathlib import Path
from typing import Any

from qpi_driver.builtins.registry import DeviceSpec, Operation
from qpi_driver.events import Event, EventType
from qpi_driver.options import Options
from qpi_driver.reload import ConfigFile
from qpi_driver.sdk import DEFAULT_RECV_TIMEOUT_MS, QpiDriver
from qpi_driver.tuners import Tuner

log = logging.getLogger(__name__)

# The worker subprocess logs under its own name, configured in `calibrate_worker`.
_worker_log = logging.getLogger("worker")

# The tuner devices the `calibrate` operation can run. Both are the same driver
# over a different tuner, so they share one builder and differ only in the name
# they register under (RFC 0003 §5).
CALIBRATE_DEVICES = ("quantify_tuner", "qblox_tuner")

#: Job id the driver gives its own periodic drift checks, so a report that was
#: not asked for by an operator says so.
DRIFT_CHECK_JOB_ID = "drift_check"


class CalibrateDriver(QpiDriver):
    """A QPI driver that calibrates a chip through a tuner backend."""

    def __init__(
        self,
        *,
        tuner: str | type[Tuner] | Tuner,
        calibration_config: Path,
        qpi_addr: str = "http://127.0.0.1:8090",
        token: str = "",
        drift_check_interval: float = 0,
        fidelity_threshold: float = 0.999,
        fidelity_2q_threshold: float = 0.99,
        ca_fingerprint: str = "",
        ca_file_path: Path = Path("./bin/qpi.ca.pem"),
        recv_timeout_ms: int = DEFAULT_RECV_TIMEOUT_MS,
        **tuner_options: Any,
    ) -> None:
        from qpi_driver.builtins.qpu import _normalize_qpi_addr

        super().__init__(
            qpi_addr=_normalize_qpi_addr(qpi_addr),
            token=token,
            ca_fingerprint=ca_fingerprint,
            ca_file_path=Path(ca_file_path).as_posix(),
            recv_timeout_ms=recv_timeout_ms,
        )
        self.tuner = tuner
        self.calibration_config_path = Path(calibration_config)
        self.drift_check_interval = drift_check_interval
        self.fidelity_threshold = fidelity_threshold
        self.fidelity_2q_threshold = fidelity_2q_threshold
        self.tuner_options = tuner_options

        self._job_queue: multiprocessing.Queue | None = None
        self._result_queue: multiprocessing.Queue | None = None
        self._worker: multiprocessing.Process | None = None
        self._result_pump: threading.Thread | None = None
        self._busy = threading.Event()
        #: What QPI-UI last said this QPU's state is. Assumed online until told
        #: otherwise, so a driver whose server predates the event still calibrates.
        self._qpu_state = "online"

        # Registered here, started by `run` after `_on_start` has made the queue
        # — the order QpiDriver.run guarantees and BlueforsGen1Driver relies on.
        if self.drift_check_interval > 0:
            self.every(self.drift_check_interval, self._check_fidelity)

    def handle_event(self, event: Event) -> None:
        """Queue a dispatched calibration; ignore everything else."""
        if event.type is EventType.QPU_STATE:
            self._qpu_state = str(event.payload.get("state") or "online")
            log.info("QPU is %s", self._qpu_state)
            return

        if event.type is not EventType.CALIBRATE_DISPATCH:
            log.warning(
                "dropping event %s: calibrate driver does not handle %s",
                event.id,
                event.type.value,
            )
            return

        payload = dict(event.payload)
        job_id = payload.get("job_id", "unknown")
        if self._qpu_state == "disabled":
            log.warning("rejecting calibration %s: the QPU is switched off", job_id)
            self._emit_result(job_id, {"error": "this QPU is switched off"})
            return
        if self._busy.is_set():
            # A calibration takes hours and the worker runs one at a time.
            # Queueing a second is not patience, it is a report nobody will
            # connect to a request, so it is refused and said so.
            log.warning("rejecting calibration %s: one is already running", job_id)
            self._emit_result(
                job_id, {"error": "a calibration is already running on this driver"}
            )
            return

        log.info("Received calibration %s (mode=%s)", job_id, payload.get("mode"))
        self._busy.set()
        self._job_queue.put(payload)

    def _check_fidelity(self) -> None:
        """Queue a periodic drift check, unless something says not to.

        The one path no server-side gate reaches: it runs on this driver's clock and
        never asks. So the state event is what stops it.
        """
        if self._qpu_state != "online":
            log.info("skipping drift check: the QPU is %s", self._qpu_state)
            return
        if self._busy.is_set():
            log.info("skipping drift check: a calibration is already running")
            return
        log.info("Queuing periodic drift check")
        self._busy.set()
        self._emit_queued(DRIFT_CHECK_JOB_ID, "fidelity_check", [], "the drift timer")
        self._job_queue.put(
            {
                "mode": "fidelity_check",
                "job_id": DRIFT_CHECK_JOB_ID,
                "fidelity_threshold": self.fidelity_threshold,
                "fidelity_2q_threshold": self.fidelity_2q_threshold,
            }
        )

    def _on_start(self) -> None:
        self._job_queue = multiprocessing.Queue()
        self._result_queue = multiprocessing.Queue()

        self._worker = multiprocessing.Process(
            target=calibrate_worker,
            kwargs={
                "job_queue": self._job_queue,
                "result_queue": self._result_queue,
                "tuner": self.tuner,
                "calibration_config_path": self.calibration_config_path,
                **self.tuner_options,
            },
            name="QPI-CalibrateWorker",
            daemon=True,
        )
        self._worker.start()

        self._result_pump = threading.Thread(
            target=self._pump_results, name="QPI-CalibrateResultPump", daemon=True
        )
        self._result_pump.start()

    def _pump_results(self) -> None:
        """Drain the worker's reports and emit each as a CalibrationResult.

        Every item is handled inside a guard, because this is a daemon thread and the
        failure it can have is the quiet one. An unhandled exception here kills the
        pump: the calibration that raised it is never reported, *every later one is
        lost too*, and `_busy` has already been cleared — so the driver goes on
        accepting work and looks healthy while nothing reaches the dashboard again.
        What an operator sees is a calibration that finished and a UI that never
        heard about it.
        """
        while True:
            item = self._result_queue.get()
            if item is None:
                log.info("Result pump received shutdown signal")
                return
            try:
                self._pump_one(item)
            except Exception:  # noqa: BLE001 - a dead pump loses every later result
                log.exception(
                    "Result pump failed on %s; reporting what it can and staying up",
                    item.get("job_id", "unknown"),
                )
                self._busy.clear()
                self._report_pump_failure(item)

    def _report_pump_failure(self, item: dict[str, Any]) -> None:
        """Last resort: say the calibration happened, even if its report cannot go.

        Guarded in turn, because whatever broke the emit above is liable to break this
        one — and a log line is still better than an operator left wondering whether
        the chip was touched at all.
        """
        try:
            self._emit_result(
                item.get("job_id", "unknown"),
                _failed_report(
                    {
                        **item,
                        "error": "the driver finished this calibration but could "
                        "not report it; see the driver log",
                    }
                ),
            )
        except Exception:  # noqa: BLE001 - nothing left to try
            log.exception("Could not report the pump failure either")

    def _pump_one(self, item: dict[str, Any]) -> None:
        """Turn one queued item into the event it describes."""
        # Both before the clear below: neither is an outcome, and treating one
        # as such would free the driver to accept another calibration.
        if "plan" in item:
            self._emit_queued(
                item["job_id"],
                item["mode"],
                item.get("target_qubits") or [],
                "the walk it is about to make",
                plan=item["plan"],
            )
            return
        if "progress" in item:
            self._emit_progress(item["job_id"], item["progress"])
            return

        self._busy.clear()
        job_id = item.get("job_id", "unknown")
        if "error" in item:
            self._emit_result(job_id, _failed_report(item))
            return

        report = item["report"]
        log.info("Emitting calibration result for %s: %s", job_id, report["status"])
        self._emit_result(job_id, report)

        for follow_up in item.get("follow_up", []):
            log.info("Drift detected; queuing recalibration of %s", follow_up)
            self._busy.set()
            self._emit_queued(
                follow_up["job_id"],
                follow_up["mode"],
                follow_up.get("target_qubits") or [],
                f"drift measured by {job_id}",
            )
            self._job_queue.put(follow_up)

    def _emit_queued(
        self,
        job_id: str,
        mode: str,
        target_qubits: list[str],
        reason: str,
        plan: dict[str, Any] | None = None,
    ) -> None:
        """Say that this driver has queued a calibration nobody dispatched.

        A drift check and the recalibration it triggers run on this driver's clock,
        so QPI-UI has no queued row for either and its Calibration tab showed hours
        of nothing. This is what gives them one — announced before the job is queued,
        so the row exists before any progress reports against it.

        Emitted a second time, with *plan*, once the walk has resolved the graph it
        will actually take (RFC 0006 §5.1). The two facts are known in different
        processes and at different moments, and the server's handler is already
        idempotent, so sending one small event twice is cheaper than a second event
        type — and a dispatched calibration, which makes no announcement of its own,
        gets its plan by the same route.

        Best-effort: the calibration is the point and the announcement is not, so a
        socket that will not carry it costs the dashboard a row, not the chip a
        recalibration.
        """
        payload: dict[str, Any] = {
            "job_id": job_id,
            "mode": mode,
            "target_qubits": list(target_qubits),
            "reason": reason,
        }
        if plan is not None:
            payload["plan"] = plan
        try:
            self.emit(
                Event(
                    type=EventType.CALIBRATION_QUEUED,
                    driver=self.name,
                    payload=payload,
                )
            )
        except Exception:  # noqa: BLE001 - see above
            log.warning("could not announce calibration %s", job_id, exc_info=True)

    def _emit_progress(self, job_id: str, update: dict[str, Any]) -> None:
        """Emit one CalibrationProgress, so the dashboard can show a walk in flight.

        Flat beside ``job_id`` for the same reason the result is: QPI-UI unmarshals
        the payload straight into its own struct.
        """
        self.emit(
            Event(
                type=EventType.CALIBRATION_PROGRESS,
                driver=self.name,
                payload={"job_id": job_id, **update},
            )
        )

    def _emit_result(self, job_id: str, report: dict[str, Any]) -> None:
        """Emit one CalibrationResult.

        The report's fields are the payload's own — QPI-UI unmarshals this
        straight into ``CalibrationResultPayload``, so nesting them under a
        ``results`` key would leave every field at its zero value.
        """
        self.emit(
            Event(
                type=EventType.CALIBRATION_RESULT,
                driver=self.name,
                payload={"job_id": job_id, **report},
            )
        )

    def _on_stop(self) -> None:
        from qpi_driver.builtins.qpu import _safe_put

        if self._job_queue is not None:
            _safe_put(self._job_queue, None)
        if self._result_queue is not None:
            _safe_put(self._result_queue, None)

        if self._worker is not None:
            # A full DAG walk will not finish inside this window, so the join is
            # a courtesy to a worker that is between routines; the terminate is
            # the expected path mid-calibration.
            self._worker.join(timeout=5)
            if self._worker.is_alive():
                log.warning("Terminating calibration worker mid-run...")
                self._worker.terminate()
                self._worker.join()


def build_from_options(
    *,
    tuner: str | type[Tuner] | Tuner,
    options: Options,
    qpi_addr: str,
    token: str,
    ca_fingerprint: str,
    ca_file_path: str,
    recv_timeout_ms: int,
    data_dir: Path = Path("./bin/data"),
    pass_through: bool = False,
) -> CalibrateDriver:
    """Build an unstarted calibrate driver over *tuner* from its ``-o`` options.

    The options a tuner reads are the ones read here, and all have working
    defaults, so a tuner needs no ``-o`` at all. The ones shared with `process`
    keep their names deliberately: a tuner and the QPU beside it read the same
    files and write under the same directory, and an operator spelling one path
    two ways will eventually spell it two values.
    """
    tuner_options: dict[str, Any] = {
        "is_dummy": options.get_bool("is_dummy"),
        "is_simulated": options.get_bool("is_simulated"),
        "quantify_hardware_config": options.get_path(
            "quantify_hardware_config", "./quantify.hardware.json"
        ),
        "quantify_device_config": options.get_path(
            "quantify_device_config", "./quantify.device.yml"
        ),
        "data_dir": options.get_dir("data_dir", data_dir, default_name="--data-dir"),
        # qblox-scheduler only; quantify's backend keeps no raw data either way.
        "save_raw_data": options.get_bool("save_raw_data"),
        # Where the SPI rack is, for chips whose couplers are parked by one.
        # Empty is correct for a chip with no tunable couplers, and for one
        # whose couplers are biased from inside the cluster.
        "spi_rack_address": options.get_str("spi_rack_address"),
    }

    calibration_config = options.get_path("calibration_config", "./calibration.yml")
    drift_check_interval = options.get_float("drift_check_interval", 0.0)
    fidelity_threshold = options.get_float("fidelity_threshold", 0.999)
    fidelity_2q_threshold = options.get_float("fidelity_2q_threshold", 0.99)

    if pass_through:
        tuner_options.update(options.remaining())

    return CalibrateDriver(
        tuner=tuner,
        calibration_config=calibration_config,
        qpi_addr=qpi_addr,
        token=token,
        drift_check_interval=drift_check_interval,
        fidelity_threshold=fidelity_threshold,
        fidelity_2q_threshold=fidelity_2q_threshold,
        ca_fingerprint=ca_fingerprint,
        ca_file_path=Path(ca_file_path),
        recv_timeout_ms=recv_timeout_ms,
        **tuner_options,
    )


def device_spec(
    name: str,
    tuner: str | type[Tuner] | Tuner | None = None,
    *,
    pass_through: bool = False,
) -> DeviceSpec:
    """A ``calibrate`` device that runs *tuner* on the calibrate driver.

    *tuner* defaults to *name* without its ``_tuner`` suffix, which is how the
    built-in tuners register; pass a class or instance to wrap one the SDK does
    not ship.
    """
    return DeviceSpec(
        name=name,
        operation=Operation.CALIBRATE,
        build=functools.partial(
            build_from_options,
            tuner=name.removesuffix("_tuner") if tuner is None else tuner,
            pass_through=pass_through,
        ),
    )


DEVICE_SPECS = tuple(device_spec(name) for name in CALIBRATE_DEVICES)


def calibrate_worker(
    job_queue: multiprocessing.Queue,
    result_queue: multiprocessing.Queue,
    tuner: str | type[Tuner] | Tuner,
    calibration_config_path: Path,
    **tuner_options: Any,
) -> None:
    """Worker process: resolve the tuner, then run queued calibrations.

    A failure to resolve the tuner, or to read ``calibration.yml``, is reported
    as an ``init_error`` result rather than a silent exit — the driver would
    otherwise sit there accepting dispatches that go nowhere.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="[CalibrateWorker] %(levelname)s %(message)s",
        force=True,
    )
    _worker_log.info("Calibration worker process started")

    from qpi_driver.builtins.qpu import _sanitize_exception_msg
    from qpi_driver.tuners import resolve_tuner
    from qpi_driver.tuners.base.config import CalibrationConfig

    try:
        tuner_instance = resolve_tuner(tuner, **tuner_options)
    except Exception as exc:
        _worker_log.exception("Failed to resolve tuner")
        result_queue.put(
            {
                "job_id": "init_error",
                "error": f"Failed to resolve tuner: {_sanitize_exception_msg(exc)}",
            }
        )
        return

    try:
        config = CalibrationConfig.from_yaml(Path(calibration_config_path))
    except FileNotFoundError:
        _worker_log.error("calibration config %s not found", calibration_config_path)
        result_queue.put(
            {
                "job_id": "init_error",
                "error": (
                    f"calibration config {calibration_config_path} not found. A tuner "
                    "with no config would calibrate nothing and report success."
                ),
            }
        )
        tuner_instance.close()
        return
    except Exception as exc:
        _worker_log.exception("Failed to load calibration config")
        result_queue.put(
            {
                "job_id": "init_error",
                "error": f"Failed to load calibration config: {_sanitize_exception_msg(exc)}",
            }
        )
        tuner_instance.close()
        return

    watched_config = ConfigFile(Path(calibration_config_path))

    while True:
        try:
            job = job_queue.get()
            if job is None:  # Poison pill
                _worker_log.info("Worker process received shutdown signal")
                break
            if watched_config.changed():
                reloaded = _reload_calibration_config(watched_config, job, result_queue)
                if reloaded is None:
                    continue
                config = reloaded
            _execute_calibration(job, tuner_instance, config, result_queue)
        except KeyboardInterrupt:
            break
        except Exception:
            _worker_log.exception("Worker loop exception")

    tuner_instance.close()


def _reload_calibration_config(
    watched: Any,
    job: dict[str, Any],
    result_queue: multiprocessing.Queue,
) -> Any:
    """Re-read the calibration config, or fail *job* and return None.

    A file that will not parse fails the calibration rather than running the
    previous config, unlike the device config which carries on: that one holds
    parameters a job can still use, this one decides *which routines run*.
    """
    from qpi_driver.builtins.qpu import _sanitize_exception_msg
    from qpi_driver.tuners.base.config import CalibrationConfig

    try:
        config = CalibrationConfig.from_yaml(watched.path)
    except Exception as exc:
        _worker_log.exception("Failed to reload calibration config")
        result_queue.put(
            {
                "job_id": job.get("job_id", "unknown"),
                "error": (
                    f"calibration config {watched.path} changed and no longer loads: "
                    f"{_sanitize_exception_msg(exc)}"
                ),
            }
        )
        return None

    watched.mark_read()
    _worker_log.info("reloaded calibration config from %s", watched.path)
    return config


def _execute_calibration(
    job: dict[str, Any],
    tuner: Tuner,
    config: Any,
    result_queue: multiprocessing.Queue,
) -> None:
    """Run one calibration, pushing its report or its error onto *result_queue*."""
    from qpi_driver.builtins.qpu import _sanitize_exception_msg

    job_id = job.get("job_id", "unknown")
    mode = job.get("mode", "full")
    target_qubits = job.get("target_qubits") or []
    _worker_log.info("Running calibration %s (mode=%s)", job_id, mode)

    # Set per calibration, because the update has to name the job it belongs to.
    tuner.on_progress = functools.partial(_queue_progress, result_queue, job_id, mode)

    try:
        if mode == "fidelity_check":
            report = tuner.check_fidelity(config)
        elif mode == "partial":
            if not target_qubits:
                raise ValueError(
                    "a 'partial' calibration needs target_qubits; refusing to "
                    "silently run a full one instead"
                )
            report = tuner.recalibrate(list(target_qubits), config)
        elif mode == "full":
            report = tuner.calibrate(config)
        else:
            raise ValueError(
                f"unknown calibration mode {mode!r}; expected one of "
                "'full', 'partial', 'fidelity_check'"
            )

        result: dict[str, Any] = {
            "job_id": job_id,
            "report": report.to_event_payload(),
        }
        if mode == "fidelity_check":
            drifted = _drifted_targets(report, job)
            if drifted:
                result["follow_up"] = [
                    {
                        "mode": "partial",
                        "job_id": f"{job_id}_recalibrate",
                        "target_qubits": drifted,
                    }
                ]
        result_queue.put(result)
        _worker_log.info("Calibration %s finished: %s", job_id, report.summary())
    except Exception as exc:
        _worker_log.exception("Calibration %s failed", job_id)
        # With the mode, because `_failed_report` needs one: the server validates `mode`
        # and `status` against their select values and refuses a payload carrying neither.
        result_queue.put(
            {"job_id": job_id, "mode": mode, "error": _sanitize_exception_msg(exc)}
        )


def _failed_report(item: dict[str, Any]) -> dict[str, Any]:
    """A minimal report for a calibration that raised, in the shape the server accepts.

    The errored path emitted ``{job_id, error}`` and nothing else. QPI-UI validates ``mode``
    and ``status`` against their select values and refuses a report carrying neither, so a
    failed calibration reached the dashboard as nothing at all: the driver logged the
    failure, emitted, and the record was rejected on arrival for being blank. What an
    operator saw was a calibration that stopped and a UI that never mentioned it.

    ``full`` when the item cannot say, because the field may not be empty and a wrong mode
    on a failed run is a far smaller lie than no record of the run.
    """
    from qpi_driver.tuners.base.dag import utc_timestamp

    return {
        "timestamp": utc_timestamp(),
        "duration_s": 0.0,
        "mode": item.get("mode") or "full",
        "status": "failed",
        "routine_results": [],
        "benchmarks": [],
        "errors": [item["error"]],
    }


def _queue_progress(
    result_queue: multiprocessing.Queue,
    job_id: str,
    mode: str,
    update: dict[str, Any],
) -> None:
    """Pass one update from the walk up to the main process, tagged with its job.

    A ``plan`` key means the walk is describing its graph rather than its position,
    which the result pump turns into a `CalibrationQueued` instead of a
    `CalibrationProgress`. A drift check's plan is dropped here: four disconnected
    benchmark nodes is not a picture worth drawing (RFC 0006 D6), and dropping it
    keeps the most frequent calibration the cheapest to store.
    """
    if "plan" in update:
        if mode != "fidelity_check":
            result_queue.put({"job_id": job_id, "mode": mode, **update})
        return
    result_queue.put({"job_id": job_id, "progress": {"mode": mode, **update}})


def _drifted_targets(report: Any, job: dict[str, Any]) -> list[str]:
    """Targets whose measured fidelity has fallen below their threshold.

    An edge is held to the two-qubit threshold and a qubit to the one-qubit one,
    since a CZ is an order of magnitude worse than a single-qubit gate and one
    threshold for both would either never fire or always fire.
    """
    one_qubit = float(job.get("fidelity_threshold", 0.999))
    two_qubit = float(job.get("fidelity_2q_threshold", 0.99))

    drifted: list[str] = []
    for target, fidelity in report.fidelities().items():
        threshold = two_qubit if "_" in target else one_qubit
        if fidelity < threshold:
            _worker_log.warning(
                "%s fidelity %.5f is below its threshold %.5f",
                target,
                fidelity,
                threshold,
            )
            # An edge is recalibrated through the qubits it joins; the routines
            # that fix a CZ are keyed on those.
            drifted.extend(target.split("_") if "_" in target else [target])
    return sorted(set(drifted))
