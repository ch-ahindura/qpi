"""The QPU driver's job machinery: the worker subprocess and the result pump.

Everything here runs in-process, against plain queues and a fake executor — no
server, no NNG socket, no subprocess. That is worth doing rather than leaving to the
e2e suite, because these are the paths that carry a *failure*: an executor that will
not resolve, a job that raises, a malformed payload. The e2e suite only ever exercises
the happy one.
"""

import multiprocessing
from pathlib import Path
from unittest.mock import patch

import pytest
from qpi_driver.builtins.qpu import (
    QpuDriver,
    _execute_job,
    _safe_put,
    _sanitize_exception_msg,
    job_worker,
)
from qpi_driver.events import Event, EventType
from qpi_driver.executors import Executor


class FakeExecutor(Executor):
    """An executor that records what it was given and can be told to fail."""

    def __init__(self, name: str = "fake", explode: bool = False, **options):
        super().__init__(name=name)
        self.explode = explode
        self.options = options
        self.closed = False
        self.executed: list[str] = []

    def execute(self, payload):
        if self.explode:
            raise RuntimeError("the fridge is warm")
        self.executed.append(payload.id)
        return {"shots": payload.shots}

    def process_result(self, dataset, job_id):
        return {"job_id": job_id, "counts": {"0": dataset["shots"]}}

    def close(self):
        self.closed = True


def _queue() -> multiprocessing.Queue:
    return multiprocessing.Queue()


def _drain(queue: multiprocessing.Queue, count: int) -> list:
    return [queue.get(timeout=5) for _ in range(count)]


# The smallest payload JobPayload accepts: it insists on at least one non-empty
# circuit, which is the shape QPI-UI's dispatcher sends.
QASM = 'OPENQASM 2.0; include "qelib1.inc"; qreg q[1]; creg c[1]; measure q -> c;'


def _payload(shots: int = 7) -> dict:
    return {"shots": shots, "circuits": [{"circuit": QASM}]}


def _job(job_id: str = "job-1", payload=None) -> dict:
    return {"job_id": job_id, "payload": _payload() if payload is None else payload}


class TestExecuteJob:
    def test_a_result_carries_the_processed_dataset(self):
        results = _queue()
        executor = FakeExecutor()

        _execute_job(_job(), executor, results)

        assert results.get(timeout=5) == {
            "job_id": "job-1",
            "results": {"job_id": "job-1", "counts": {"0": 7}},
        }
        assert executor.executed == ["job-1"]

    def test_a_job_that_raises_becomes_an_error_result(self):
        """One bad job is reported and the worker carries on with the next."""
        results = _queue()

        _execute_job(_job(), FakeExecutor(explode=True), results)

        item = results.get(timeout=5)
        assert item["job_id"] == "job-1"
        # The exception type, not its message: an arbitrary exception's text can
        # carry anything, including paths.
        assert item["error"] == (
            "RuntimeError: job execution failed, see driver logs for details"
        )

    def test_a_string_payload_is_parsed_as_json(self):
        """QPI-UI may send the payload as a JSON string rather than an object."""
        import json

        results = _queue()
        executor = FakeExecutor()

        _execute_job(_job(payload=json.dumps(_payload(3))), executor, results)

        assert results.get(timeout=5)["results"]["counts"] == {"0": 3}

    def test_an_unparseable_payload_is_reported_not_dropped(self):
        """A malformed payload becomes an error result, not a silent loss.

        It falls back to an empty dict, which JobPayload then rejects for carrying no
        circuit — so the job is reported as failed rather than vanishing. The message
        survives intact because it is a ValueError, the SDK's own "bad input" signal.
        """
        results = _queue()

        _execute_job(_job(payload="{not json"), FakeExecutor(), results)

        item = results.get(timeout=5)
        assert item["job_id"] == "job-1"
        assert item["error"] == "No QASM string/circuit provided in payload"

    def test_a_job_with_no_id_is_still_reported(self):
        results = _queue()

        _execute_job({"payload": _payload(1)}, FakeExecutor(), results)

        assert results.get(timeout=5)["job_id"] == "unknown"


class TestJobWorker:
    """The worker loop, run in this process — the loop itself is what is under test."""

    def test_it_runs_queued_jobs_until_the_poison_pill(self, tmp_path):
        jobs, results = _queue(), _queue()
        jobs.put(_job("a"))
        jobs.put(_job("b"))
        jobs.put(None)

        job_worker(jobs, results, FakeExecutor, tmp_path / "data")

        assert [item["job_id"] for item in _drain(results, 2)] == ["a", "b"]

    def test_it_makes_the_data_directory(self, tmp_path):
        jobs, results = _queue(), _queue()
        jobs.put(None)
        data_dir = tmp_path / "nested" / "data"

        job_worker(jobs, results, FakeExecutor, data_dir)

        assert data_dir.is_dir()

    def test_an_executor_that_will_not_resolve_reports_init_error(self, tmp_path):
        """A worker that cannot start says so, rather than exiting silently.

        Without this the driver would connect, accept jobs and drop every one of
        them, which looks like a hung QPU rather than a misconfigured one.
        """
        jobs, results = _queue(), _queue()

        job_worker(jobs, results, "no-such-executor", tmp_path / "data")

        item = results.get(timeout=5)
        assert item["job_id"] == "init_error"
        assert "Failed to resolve executor" in item["error"]
        assert "no-such-executor" in item["error"]

    def test_the_executor_gets_the_options_and_is_closed(self, tmp_path):
        """Options reach the executor's constructor, and it is closed on the way out."""
        built: list[FakeExecutor] = []

        class Recording(FakeExecutor):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                built.append(self)

        jobs, results = _queue(), _queue()
        jobs.put(None)

        job_worker(jobs, results, Recording, tmp_path / "data", job_timeout=42)

        assert built[0].options["job_timeout"] == 42
        assert built[0].closed is True

    def test_the_data_dir_reaches_the_executor(self, tmp_path):
        """The executor is told where to write, as well as the worker creating it."""
        built: list[FakeExecutor] = []

        class Recording(FakeExecutor):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                built.append(self)

        jobs, results = _queue(), _queue()
        jobs.put(None)

        job_worker(jobs, results, Recording, tmp_path / "data")

        assert built[0].options["data_dir"] == tmp_path / "data"

    def test_a_keyboard_interrupt_ends_the_loop(self, tmp_path):
        """Ctrl-C reaches the worker too, and means stop rather than log-and-retry."""
        results = _queue()

        class Interrupts:
            def __init__(self):
                self.gets = 0

            def get(self):
                self.gets += 1
                raise KeyboardInterrupt

        jobs = Interrupts()
        job_worker(jobs, results, FakeExecutor, tmp_path / "data")

        assert jobs.gets == 1

    def test_a_failure_inside_the_loop_does_not_stop_it(self, tmp_path):
        """A queue that raises once must not take the worker down with it."""
        results = _queue()
        calls = {"n": 0}

        class OneBadGet:
            def get(self):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise OSError("transient")
                return None

        job_worker(OneBadGet(), results, FakeExecutor, tmp_path / "data")

        assert calls["n"] == 2  # it went round again rather than exiting


class TestResultPump:
    """The thread that turns worker results into JobResult events."""

    def _driver(self) -> QpuDriver:
        driver = QpuDriver(executor="mock", data_dir=Path("./bin/data"))
        driver._result_queue = _queue()
        return driver

    def test_it_emits_one_event_per_result(self):
        driver = self._driver()
        emitted: list[Event] = []
        driver._result_queue.put({"job_id": "a", "results": {"counts": {"0": 1}}})
        driver._result_queue.put(None)

        with patch.object(QpuDriver, "emit", lambda self, event: emitted.append(event)):
            driver._pump_results()

        assert len(emitted) == 1
        assert emitted[0].type is EventType.JOB_RESULT
        assert emitted[0].payload == {"job_id": "a", "results": {"counts": {"0": 1}}}
        assert emitted[0].driver == driver.name

    def test_an_error_result_is_emitted_as_an_error_payload(self):
        """A failed job still reports upward — QPI-UI must not wait forever."""
        driver = self._driver()
        emitted: list[Event] = []
        driver._result_queue.put({"job_id": "b", "error": "the fridge is warm"})
        driver._result_queue.put(None)

        with patch.object(QpuDriver, "emit", lambda self, event: emitted.append(event)):
            driver._pump_results()

        assert emitted[0].payload == {
            "job_id": "b",
            "results": {"error": "the fridge is warm"},
        }

    def test_the_poison_pill_stops_it_without_emitting(self):
        driver = self._driver()
        emitted: list[Event] = []
        driver._result_queue.put(None)

        with patch.object(QpuDriver, "emit", lambda self, event: emitted.append(event)):
            driver._pump_results()

        assert emitted == []


class TestLifecycle:
    def test_start_spawns_a_worker_and_a_pump_then_stop_joins_them(self):
        """The one test that really starts the subprocess, so the wiring is proved."""
        driver = QpuDriver(executor="mock", data_dir=Path("./bin/data"))

        driver._on_start()
        try:
            assert driver._worker is not None and driver._worker.is_alive()
            assert driver._result_pump is not None and driver._result_pump.is_alive()
            assert driver._worker.daemon and driver._result_pump.daemon
        finally:
            driver._on_stop()

        assert not driver._worker.is_alive()

    def test_stop_is_safe_before_start(self):
        """SIGINT during the handshake means stopping something never started."""
        QpuDriver(executor="mock", data_dir=Path("./bin/data"))._on_stop()

    def test_stop_terminates_a_worker_that_will_not_exit(self):
        """A wedged executor must not keep the process alive after shutdown."""
        driver = QpuDriver(executor="mock", data_dir=Path("./bin/data"))
        driver._job_queue = _queue()
        driver._result_queue = _queue()

        class Stubborn:
            def __init__(self):
                self.terminated = False
                self.joins = 0

            def join(self, timeout=None):
                self.joins += 1

            def is_alive(self):
                return not self.terminated

            def terminate(self):
                self.terminated = True

        driver._worker = Stubborn()
        driver._on_stop()

        assert driver._worker.terminated is True
        assert driver._worker.joins == 2  # once with a timeout, once after terminate

    def test_handle_event_enqueues_only_dispatched_jobs(self):
        driver = QpuDriver(executor="mock", data_dir=Path("./bin/data"))
        driver._job_queue = _queue()

        driver.handle_event(Event(type=EventType.JOB_DISPATCH, payload=_job("a")))
        driver.handle_event(Event(type=EventType.CRYOSTAT_READING, payload={}))

        assert driver._job_queue.get(timeout=5)["job_id"] == "a"
        assert driver._job_queue.empty()


class TestHelpers:
    def test_safe_put_swallows_a_closed_queue(self):
        """Shutdown races: the queue may be gone before the poison pill lands."""

        class Closed:
            def put(self, item):
                raise ValueError("queue is closed")

        _safe_put(Closed(), None)  # must not raise

    def test_a_value_error_keeps_its_message(self):
        """A ValueError is the SDK's own "bad input" signal, so it is safe to show."""
        assert _sanitize_exception_msg(ValueError("shots must be positive")) == (
            "shots must be positive"
        )

    def test_any_other_exception_is_reduced_to_its_type(self):
        # An arbitrary exception's text can carry anything, including paths and
        # credentials from a vendor library.
        assert _sanitize_exception_msg(KeyError("/etc/secrets/token")) == (
            "KeyError: job execution failed, see driver logs for details"
        )


@pytest.mark.parametrize("event_type", list(EventType))
def test_handle_event_never_raises(event_type):
    """Whatever arrives, the receive loop must survive it (RFC 0001 §8)."""
    driver = QpuDriver(executor="mock", data_dir=Path("./bin/data"))
    driver._job_queue = _queue()

    driver.handle_event(Event(type=event_type, payload={}))


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("qpi.example.com", "http://qpi.example.com"),
        ("http://qpi:8090/", "http://qpi:8090"),
        ("https://qpi.example.com///", "https://qpi.example.com"),
    ],
)
def test_the_address_gets_a_scheme_and_loses_its_trailing_slash(given, expected):
    """An operator types a host; the SDK needs a URL (and no double slash on join)."""
    driver = QpuDriver(qpi_addr=given, executor="mock", data_dir=Path("./bin/data"))

    assert driver.qpi_addr == expected


def test_a_driver_does_not_name_itself():
    """The display label belongs to the admin who registered the driver.

    Nothing looks a driver up by name — the token is the identity — so a name in the
    connect body only ever overwrote what an admin typed in the dashboard. It comes
    back in the response instead, which is why there is nothing to assert here until
    the driver has connected.
    """
    driver = QpuDriver(executor="mock", data_dir=Path("./bin/data"))

    assert driver.name == ""

    # QpuDriver takes **executor_options, so a leftover name= is not a TypeError: it
    # reaches the executor, whose own name is what a dataset's "backend" attribute
    # records. That is the one place a name still means something here.
    stale = QpuDriver(name="qpu-sim-01", executor="mock")
    assert stale.name == ""
    assert stale.executor_options["name"] == "qpu-sim-01"
