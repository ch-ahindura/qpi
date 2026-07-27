"""The QPU expressed as a QPI driver (RFC 0001 §4).

A QPU handles :attr:`EventType.JOB_DISPATCH` by running quantum jobs on its executor
and emitting :attr:`EventType.JOB_RESULT` with the outcome. Execution happens in a worker
subprocess so a heavy or crashing executor never blocks or takes down the receive loop.
"""

import json
import logging
import multiprocessing
import os
import threading
from pathlib import Path
from typing import Any

from qpi_driver.events import Event, EventType
from qpi_driver.executors import Executor, JobPayload
from qpi_driver.paths import validate_safe_path
from qpi_driver.sdk import DEFAULT_RECV_TIMEOUT_MS, QpiDriver

log = logging.getLogger(__name__)

# The executor devices the `process` operation can run. Each maps to run_process
# in the builtins registry (RFC 0001 §4).
PROCESS_DEVICES = ("mock", "qiskit_aer", "quantify", "qblox", "presto")


class QpuDriver(QpiDriver):
    """A QPI driver that runs quantum jobs on an executor."""

    OPERATION = "process"

    def __init__(
        self,
        qpi_addr: str = "http://127.0.0.1:8090",
        token: str = "",
        name: str = "qpu_sim_01",
        executor: str | type[Executor] | Executor = "mock",
        custom_executors: dict[str, type[Executor]] | None = None,
        data_dir: Path = Path("bin/data"),
        ca_fingerprint: str = "",
        ca_file_path: Path = Path("./bin/qpi.ca.pem"),
        recv_timeout_ms: int = DEFAULT_RECV_TIMEOUT_MS,
        **executor_options: Any,
    ) -> None:
        super().__init__(
            qpi_addr=_normalize_qpi_addr(qpi_addr),
            token=token,
            name=_sanitize_name(name),
            ca_fingerprint=ca_fingerprint,
            ca_file_path=Path(ca_file_path).as_posix(),
            recv_timeout_ms=recv_timeout_ms,
        )
        self.executor = executor
        self.custom_executors = custom_executors
        self.data_dir = data_dir
        self.executor_options = executor_options

        self._job_queue: multiprocessing.Queue | None = None
        self._result_queue: multiprocessing.Queue | None = None
        self._worker: multiprocessing.Process | None = None
        self._result_pump: threading.Thread | None = None

    def handle_event(self, event: Event) -> None:
        """Run dispatched jobs; ignore everything else (RFC 0001 §8).

        A JobDispatch payload is the job envelope QPI-UI's dispatcher builds —
        ``{job_id, payload}`` — which is exactly what the worker consumes.
        """
        if event.type is EventType.JOB_DISPATCH:
            log.info("Received job %s", event.payload.get("job_id"))
            self._job_queue.put(event.payload)
        else:
            log.warning(
                "dropping event %s: QPU driver does not handle %s",
                event.id,
                event.type.value,
            )

    def _on_start(self) -> None:
        self._job_queue = multiprocessing.Queue()
        self._result_queue = multiprocessing.Queue()

        worker_options = {**self.executor_options, "name": self.name}
        self._worker = multiprocessing.Process(
            target=execute_job,
            kwargs={
                "job_queue": self._job_queue,
                "result_queue": self._result_queue,
                "executor": self.executor,
                "custom_executors": self.custom_executors,
                "data_dir": self.data_dir,
                **worker_options,
            },
            name="QPI-Worker",
            daemon=True,
        )
        self._worker.start()

        self._result_pump = threading.Thread(
            target=self._pump_results, name="QPI-ResultPump", daemon=True
        )
        self._result_pump.start()

    def _pump_results(self) -> None:
        """Drain executor results and emit each as a JobResult event."""
        while True:
            item = self._result_queue.get()
            if item is None:
                log.info("Result pump received shutdown signal")
                return

            job_id = item["job_id"]
            results = {"error": item["error"]} if "error" in item else item["results"]
            log.info("Emitting result for job %s", job_id)
            self.emit(
                Event(
                    type=EventType.JOB_RESULT,
                    driver=self.name,
                    payload={"job_id": job_id, "results": results},
                )
            )

    def _on_stop(self) -> None:
        if self._job_queue is not None:
            _safe_put(self._job_queue, None)
        if self._result_queue is not None:
            _safe_put(self._result_queue, None)

        if self._worker is not None:
            self._worker.join(timeout=2)
            if self._worker.is_alive():
                log.warning("Terminating worker process...")
                self._worker.terminate()
                self._worker.join()


def run_driver(
    qpi_addr: str = "http://127.0.0.1:8090",
    token: str = "",
    name: str = "qpu_sim_01",
    executor: str | type[Executor] | Executor = "mock",
    custom_executors: dict[str, type[Executor]] | None = None,
    data_dir: Path = Path("bin/data"),
    ca_fingerprint: str = "",
    ca_file_path: Path = Path("./bin/qpi.ca.pem"),
    recv_timeout_ms: int = DEFAULT_RECV_TIMEOUT_MS,
    **executor_options: Any,
) -> None:
    """Run a QPU on the event-based driver framework."""
    QpuDriver(
        qpi_addr=qpi_addr,
        token=token,
        name=name,
        executor=executor,
        custom_executors=custom_executors,
        data_dir=data_dir,
        ca_fingerprint=ca_fingerprint,
        ca_file_path=ca_file_path,
        recv_timeout_ms=recv_timeout_ms,
        **executor_options,
    ).run()


def run_process(
    *,
    device: str,
    options: dict[str, str],
    qpi_addr: str,
    token: str,
    name: str,
    ca_fingerprint: str,
    ca_file_path: str,
    recv_timeout_ms: int,
) -> None:
    """Run a QPU (process) driver on executor *device*, config from -o options."""
    data_dir = Path(options.get("data_dir", "./bin/data"))
    validate_safe_path(data_dir, "data_dir")

    executor_kwargs: dict[str, Any] = {
        "qpi_addr": qpi_addr,
        "token": token,
        "name": name,
        "executor": device,
        "data_dir": data_dir,
        "ca_fingerprint": ca_fingerprint,
        "ca_file_path": Path(ca_file_path),
        "is_dummy": _as_bool(options.get("is_dummy")),
        "quantify_hardware_config": Path(
            options.get("quantify_hardware_config", "./quantify.hardware.json")
        ),
        "quantify_device_config": Path(
            options.get("quantify_device_config", "./quantify.device.yml")
        ),
        "job_timeout": int(options.get("job_timeout", 10)),
        "recv_timeout_ms": recv_timeout_ms,
    }

    run_driver(**executor_kwargs)


def execute_job(
    job_queue: multiprocessing.Queue,
    result_queue: multiprocessing.Queue,
    executor: str | type[Executor] | Executor,
    custom_executors: dict[str, type[Executor]] | None,
    data_dir: Path,
    **executor_options: Any,
) -> None:
    """
    Worker process: pulls job dicts from job_queue, executes them using the resolved
    executor, converts results to Qiskit-format dicts via ``executor.process_result()``,
    and pushes the result dicts to result_queue.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="[WorkerProcess] %(levelname)s %(message)s",
        force=True,
    )
    w_log = logging.getLogger("worker")
    w_log.info("Worker process started")

    from qpi_driver.executors import resolve_executor

    try:
        options = executor_options.copy()
        if "data_dir" not in options:
            options["data_dir"] = data_dir
        executor_instance = resolve_executor(executor, custom_executors, **options)
    except Exception as exc:
        w_log.exception("Failed to resolve executor")
        result_queue.put(
            {
                "job_id": "init_error",
                "error": f"Failed to resolve executor: {_sanitize_exception_msg(exc)}",
            }
        )
        return

    os.makedirs(data_dir, exist_ok=True)

    while True:
        try:
            job = job_queue.get()
            if job is None:  # Poison pill
                w_log.info("Worker process received shutdown signal")
                break

            job_id = job.get("job_id", "unknown")
            w_log.info("Worker process executing job %s", job_id)

            try:
                payload_dict = job.get("payload", {})
                if isinstance(payload_dict, str):
                    try:
                        payload_dict = json.loads(payload_dict)
                    except Exception:
                        payload_dict = {}

                payload_dict.update(dict(id=job_id))
                payload = JobPayload.from_dict(payload_dict)
                dataset = executor_instance.execute(payload)
                result_dict = executor_instance.process_result(dataset, job_id)
                result_queue.put({"job_id": job_id, "results": result_dict})
                w_log.info("Worker process completed job %s", job_id)
            except Exception as exc:
                w_log.exception("Worker process failed job %s", job_id)
                result_queue.put(
                    {"job_id": job_id, "error": _sanitize_exception_msg(exc)}
                )
        except KeyboardInterrupt:
            break
        except Exception:
            w_log.exception("Worker loop exception")

    executor_instance.close()


def _normalize_qpi_addr(qpi_addr: str) -> str:
    if "://" not in qpi_addr:
        qpi_addr = f"http://{qpi_addr}"
    return qpi_addr.rstrip("/")


def _sanitize_name(name: str) -> str:
    return name.replace("-", "_")


def _sanitize_exception_msg(exc: Exception) -> str:
    if isinstance(exc, ValueError):
        return str(exc)
    return f"{type(exc).__name__}: job execution failed, see driver logs for details"


def _as_bool(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _safe_put(queue: multiprocessing.Queue, item: Any) -> None:
    try:
        queue.put(item)
    except Exception:
        pass
