"""Calibration driver for transmon qubits.

A calibrate driver handles CalibrateDispatch events by running calibration
routines on its tuner backend and emitting CalibrationResult events.
Calibration happens in a worker subprocess so a heavy or crashing tuner
never blocks or takes down the receive loop.

Optionally runs periodic fidelity checks via self.every(), triggering
recalibration when gate fidelity drops below threshold.
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
from qpi_driver.sdk import DEFAULT_RECV_TIMEOUT_MS, QpiDriver
from qpi_driver.tuners import Tuner
from qpi_driver.tuners.base.config import CalibrationConfig

log = logging.getLogger(__name__)
_worker_log = logging.getLogger("worker")

CALIBRATE_DEVICES = ("quantify_tuner", "qblox_tuner")


class CalibrateDriver(QpiDriver):
    """A QPI driver that runs calibration jobs on a tuner."""

    def __init__(
        self,
        *,
        tuner: str | type[Tuner] | Tuner,
        calibration_config: Path,
        qpi_addr: str = "http://127.0.0.1:8090",
        token: str = "",
        monitor_interval: float = 0,
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
        self.calibration_config_path = calibration_config
        self.monitor_interval = monitor_interval
        self.fidelity_threshold = fidelity_threshold
        self.fidelity_2q_threshold = fidelity_2q_threshold
        self.tuner_options = tuner_options

        self._job_queue: multiprocessing.Queue | None = None
        self._result_queue: multiprocessing.Queue | None = None
        self._worker: multiprocessing.Process | None = None
        self._result_pump: threading.Thread | None = None

        if self.monitor_interval > 0:
            self.every(self.monitor_interval, self._check_fidelity)

    def handle_event(self, event: Event) -> None:
        """Handle CALIBRATE_DISPATCH events by pushing them to the worker."""
        if event.type is EventType.CALIBRATE_DISPATCH:
            log.info("Received calibration job %s", event.payload.get("job_id"))
            if self._job_queue is not None:
                self._job_queue.put(event.payload)
        else:
            log.warning(
                "dropping event %s: calibrate driver does not handle %s",
                event.id,
                event.type.value,
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
        """Drain tuner results and emit each as a CalibrationResult event."""
        while True:
            item = self._result_queue.get()
            if item is None:
                log.info("Result pump received shutdown signal")
                return

            job_id = item.get("job_id")
            results = {"error": item["error"]} if "error" in item else item["results"]
            log.info("Emitting result for calibration job %s", job_id)
            self.emit(
                Event(
                    type=EventType.CALIBRATION_RESULT,
                    driver=self.name,
                    payload={"job_id": job_id, "results": results},
                )
            )

    def _check_fidelity(self) -> None:
        """Periodic fidelity check."""
        log.info("Queuing periodic fidelity check")
        if self._job_queue is not None:
            self._job_queue.put(
                {"mode": "fidelity_check", "job_id": "auto_fidelity_check"}
            )

    def _on_stop(self) -> None:
        from qpi_driver.builtins.qpu import _safe_put

        if self._job_queue is not None:
            _safe_put(self._job_queue, None)
        if self._result_queue is not None:
            _safe_put(self._result_queue, None)

        if self._worker is not None:
            self._worker.join(timeout=2)
            if self._worker.is_alive():
                log.warning("Terminating calibration worker process...")
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
    pass_through: bool = False,
) -> CalibrateDriver:
    """Build an unstarted calibrate driver over *tuner* from its ``-o`` options."""
    tuner_options: dict[str, Any] = {
        "is_dummy": options.get_bool("is_dummy"),
        "quantify_hardware_config": options.get_path(
            "quantify_hardware_config", "./quantify.hardware.json"
        ),
        "quantify_device_config": options.get_path(
            "quantify_device_config", "./quantify.device.yml"
        ),
    }

    calibration_config = options.get_path("calibration_config", "./calibration.yml")
    monitor_interval = options.get_float("monitor_interval", 0.0)
    fidelity_threshold = options.get_float("fidelity_threshold", 0.999)
    fidelity_2q_threshold = options.get_float("fidelity_2q_threshold", 0.99)

    if pass_through:
        tuner_options.update(options.remaining())

    return CalibrateDriver(
        tuner=tuner,
        calibration_config=calibration_config,
        qpi_addr=qpi_addr,
        token=token,
        monitor_interval=monitor_interval,
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
    """A ``calibrate`` device that runs *tuner* on the CalibrateDriver."""
    if tuner is None:
        tuner_name = name.replace("_tuner", "")
    else:
        tuner_name = tuner

    return DeviceSpec(
        name=name,
        operation=Operation.CALIBRATE,
        build=functools.partial(
            build_from_options,
            tuner=tuner_name,
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
    """Worker process: resolve the tuner, then run calibration jobs."""
    logging.basicConfig(
        level=logging.INFO,
        format="[CalibrateWorker] %(levelname)s %(message)s",
        force=True,
    )
    _worker_log.info("Calibration worker process started")

    from qpi_driver.builtins.qpu import _sanitize_exception_msg
    from qpi_driver.tuners import resolve_tuner

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

    config = CalibrationConfig()
    try:
        if calibration_config_path.exists():
            config = CalibrationConfig.from_yaml(calibration_config_path)
    except Exception as exc:
        _worker_log.exception("Failed to load calibration config")
        result_queue.put(
            {
                "job_id": "init_error",
                "error": f"Failed to load calibration config: {_sanitize_exception_msg(exc)}",
            }
        )
        return

    while True:
        try:
            job = job_queue.get()
            if job is None:
                _worker_log.info("Worker process received shutdown signal")
                break

            _execute_calibration(job, tuner_instance, config, result_queue)
        except KeyboardInterrupt:
            break
        except Exception:
            _worker_log.exception("Worker loop exception")


def _execute_calibration(
    job: dict[str, Any],
    tuner: Tuner,
    config: CalibrationConfig,
    result_queue: multiprocessing.Queue,
) -> None:
    job_id = job.get("job_id", "unknown")
    mode = job.get("mode", "full")
    target_qubits = job.get("target_qubits")
    _worker_log.info(
        "Worker process executing calibration job %s (mode: %s)", job_id, mode
    )

    try:
        if mode == "fidelity_check":
            report = tuner.check_fidelity(config)
        elif mode == "partial" and target_qubits:
            report = tuner.recalibrate(target_qubits, config)
        else:
            report = tuner.calibrate(config)

        result_queue.put({"job_id": job_id, "results": report.to_event_payload()})
        _worker_log.info("Worker process completed calibration job %s", job_id)
    except Exception as exc:
        _worker_log.exception("Worker process failed calibration job %s", job_id)
        from qpi_driver.builtins.qpu import _sanitize_exception_msg

        result_queue.put({"job_id": job_id, "error": _sanitize_exception_msg(exc)})
