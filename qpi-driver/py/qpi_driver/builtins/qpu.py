"""The QPU expressed as a QPI driver (RFC 0001 §4).

A QPU handles :attr:`EventType.JOB_DISPATCH` by running quantum jobs on its executor
and emitting :attr:`EventType.JOB_RESULT` with the outcome. Execution happens in a worker
subprocess so a heavy or crashing executor never blocks or takes down the receive loop.
"""

import functools
import json
import logging
import multiprocessing
import os
import threading
from pathlib import Path
from typing import Any

from qpi_driver.builtins.registry import DeviceSpec, Operation, OptionSpec, as_bool
from qpi_driver.events import Event, EventType
from qpi_driver.executors import Executor, JobPayload
from qpi_driver.paths import as_safe_dir
from qpi_driver.sdk import DEFAULT_RECV_TIMEOUT_MS, QpiDriver

log = logging.getLogger(__name__)

# The worker subprocess logs under its own name, configured in `job_worker`.
_worker_log = logging.getLogger("worker")

# The executor devices the `process` operation can run. Every one of them is the
# same driver over a different executor, so they share one builder and differ
# only in their spec (RFC 0003 §5).
PROCESS_DEVICES = ("mock", "qiskit_aer", "quantify", "qblox", "presto")

# Which install target ships each executor. Empty means the base `[cli]` extra
# is enough; kept in step with qpi-ui/internal/drivers/catalog.go.
_DEVICE_EXTRAS = {
    "mock": "",
    "presto": "",
    "qiskit_aer": "qpi-driver[cli,aer]",
    "quantify": "qpi-driver[cli,quantify]",
    "qblox": "qpi-driver[cli,qblox]",
}

_DEVICE_SUMMARIES = {
    "mock": "Qiskit BasicSimulator — the default, needs no hardware.",
    "presto": "Presto control system (not yet implemented).",
    "qiskit_aer": "Qiskit Aer simulator.",
    "quantify": "Quantify-scheduler over Qblox instruments.",
    "qblox": "Qblox scheduler (legacy).",
}


class QpuDriver(QpiDriver):
    """A QPI driver that runs quantum jobs on an executor."""

    def __init__(
        self,
        qpi_addr: str = "http://127.0.0.1:8090",
        token: str = "",
        executor: str | type[Executor] | Executor = "mock",
        data_dir: Path = Path("bin/data"),
        ca_fingerprint: str = "",
        ca_file_path: Path = Path("./bin/qpi.ca.pem"),
        recv_timeout_ms: int = DEFAULT_RECV_TIMEOUT_MS,
        **executor_options: Any,
    ) -> None:
        super().__init__(
            qpi_addr=_normalize_qpi_addr(qpi_addr),
            token=token,
            ca_fingerprint=ca_fingerprint,
            ca_file_path=Path(ca_file_path).as_posix(),
            recv_timeout_ms=recv_timeout_ms,
        )
        self.executor = executor
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

        # The executor keeps its own name, which is what a dataset's "backend"
        # attribute is for. This used to be overridden with the driver's display
        # label — the reason a _sanitize_name existed at all — so a cryostat called
        # "lab-1" produced datasets claiming a backend of "lab_1".
        self._worker = multiprocessing.Process(
            target=job_worker,
            kwargs={
                "job_queue": self._job_queue,
                "result_queue": self._result_queue,
                "executor": self.executor,
                "data_dir": self.data_dir,
                **self.executor_options,
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


def build_from_options(
    *,
    executor: str | type[Executor] | Executor,
    options: dict[str, Any],
    qpi_addr: str,
    token: str,
    ca_fingerprint: str,
    ca_file_path: str,
    recv_timeout_ms: int,
) -> QpuDriver:
    """Build an unstarted QPU driver over *executor* from parsed ``-o`` options.

    *options* is what :meth:`DeviceSpec.parse_options` returns for a process
    device — already checked and coerced against :data:`OPTIONS`, so there is
    nothing to validate or convert here. Everything but ``data_dir``, which the
    driver takes in its own right, goes to the executor untouched, so a new entry
    in :data:`OPTIONS` reaches the executor without editing this function.

    The driver is returned rather than started so a caller — including a test —
    decides when, and whether, to connect (RFC 0003 §7).
    """
    executor_options = {
        key: value for key, value in options.items() if key != "data_dir"
    }

    return QpuDriver(
        qpi_addr=qpi_addr,
        token=token,
        executor=executor,
        data_dir=options["data_dir"],
        ca_fingerprint=ca_fingerprint,
        ca_file_path=Path(ca_file_path),
        recv_timeout_ms=recv_timeout_ms,
        **executor_options,
    )


# The runtime settings every process device reads, whichever executor it runs.
# All of them have working defaults, so a QPU needs no -o options at all.
OPTIONS = (
    OptionSpec(
        key="data_dir",
        help="Directory the executor writes datasets and artefacts to.",
        parse=as_safe_dir,
        default="./bin/data",
        example="./bin/data",
    ),
    OptionSpec(
        key="job_timeout",
        help="Seconds a single job may run before it is abandoned.",
        parse=int,
        default="10",
        example="30",
    ),
    OptionSpec(
        key="is_dummy",
        help="Run against the vendor's dummy instruments instead of real hardware.",
        parse=as_bool,
        default="false",
        example="true",
    ),
    OptionSpec(
        key="quantify_hardware_config",
        help="Path to the quantify hardware configuration JSON.",
        parse=Path,
        default="./quantify.hardware.json",
        example="./quantify.hardware.json",
    ),
    OptionSpec(
        key="quantify_device_config",
        help="Path to the quantify device configuration YAML.",
        parse=Path,
        default="./quantify.device.yml",
        example="./quantify.device.yml",
    ),
)


def device_spec(
    name: str,
    executor: str | type[Executor] | Executor | None = None,
    *,
    extra: str = "",
    summary: str = "",
    options: tuple[OptionSpec, ...] = (),
    accepts_any_option: bool = False,
) -> DeviceSpec:
    """Describe a ``process`` device that runs *executor* on the QPU driver.

    Every process device is this one driver over a different executor, so they
    all share :func:`build_from_options` with the executor bound. *executor*
    defaults to *name*, which is how the built-in executors register; pass a
    class or instance to wrap one the SDK does not ship.

    *options* are this executor's own settings, added to the :data:`OPTIONS` every
    process device reads. Declaring them is what gets them into ``--help`` and gets
    their values checked and converted; *accepts_any_option* is the alternative,
    letting undeclared options through to the executor's constructor as strings —
    which is all a device named by import path can do, its executor being one this
    module has never seen.
    """
    return DeviceSpec(
        name=name,
        operation=Operation.PROCESS,
        build=functools.partial(
            build_from_options, executor=name if executor is None else executor
        ),
        options=OPTIONS + tuple(options),
        extra=extra,
        summary=summary,
        accepts_any_option=accepts_any_option,
    )


DEVICE_SPECS = tuple(
    device_spec(
        name,
        extra=_DEVICE_EXTRAS[name],
        summary=_DEVICE_SUMMARIES[name],
    )
    for name in PROCESS_DEVICES
)


def job_worker(
    job_queue: multiprocessing.Queue,
    result_queue: multiprocessing.Queue,
    executor: str | type[Executor] | Executor,
    data_dir: Path,
    **executor_options: Any,
) -> None:
    """Worker process: resolve the executor, then run queued jobs until told to stop.

    Runs in its own subprocess so a heavy or crashing executor never blocks or
    takes down the receive loop. A failure to resolve the executor at all is
    reported as an ``init_error`` result rather than a silent exit.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="[WorkerProcess] %(levelname)s %(message)s",
        force=True,
    )
    _worker_log.info("Worker process started")

    from qpi_driver.executors import resolve_executor

    try:
        # data_dir is a parameter of this function, so it can never arrive in
        # **executor_options — the executor is told about it here instead.
        executor_instance = resolve_executor(
            executor, **executor_options, data_dir=data_dir
        )
    except Exception as exc:
        _worker_log.exception("Failed to resolve executor")
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
                _worker_log.info("Worker process received shutdown signal")
                break

            _execute_job(job, executor_instance, result_queue)
        except KeyboardInterrupt:
            break
        except Exception:
            _worker_log.exception("Worker loop exception")

    executor_instance.close()


def _execute_job(
    job: dict[str, Any],
    executor: Executor,
    result_queue: multiprocessing.Queue,
) -> None:
    """Run one job, pushing either its results or its error onto *result_queue*.

    Results are converted to Qiskit-format dicts via ``executor.process_result()``.
    A job that raises is reported and the worker carries on with the next one.
    """
    job_id = job.get("job_id", "unknown")
    _worker_log.info("Worker process executing job %s", job_id)

    try:
        payload_dict = job.get("payload", {})
        if isinstance(payload_dict, str):
            try:
                payload_dict = json.loads(payload_dict)
            except Exception:
                payload_dict = {}

        payload_dict.update(dict(id=job_id))
        payload = JobPayload.from_dict(payload_dict)
        dataset = executor.execute(payload)
        result_dict = executor.process_result(dataset, job_id)
        result_queue.put({"job_id": job_id, "results": result_dict})
        _worker_log.info("Worker process completed job %s", job_id)
    except Exception as exc:
        _worker_log.exception("Worker process failed job %s", job_id)
        result_queue.put({"job_id": job_id, "error": _sanitize_exception_msg(exc)})


def _normalize_qpi_addr(qpi_addr: str) -> str:
    if "://" not in qpi_addr:
        qpi_addr = f"http://{qpi_addr}"
    return qpi_addr.rstrip("/")


def _sanitize_exception_msg(exc: Exception) -> str:
    if isinstance(exc, ValueError):
        return str(exc)
    return f"{type(exc).__name__}: job execution failed, see driver logs for details"


def _safe_put(queue: multiprocessing.Queue, item: Any) -> None:
    try:
        queue.put(item)
    except Exception:
        pass
