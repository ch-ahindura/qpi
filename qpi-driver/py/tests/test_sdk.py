"""Unit tests for the driver SDK base class and the QPU driver (RFC 0001 Phase 2).

These cover the framework's own behaviour — handler routing by event type, the
emit encoding, periodic callbacks, and log-and-drop — plus the QPU driver's
translation between the legacy job/result wire shapes and the event model. They
do not open sockets or subprocesses; the transport is exercised by the e2e suite.
"""

import json
import threading
import time

import pytest
from qpi_driver.builtins.qpu import QpuDriver
from qpi_driver.events import Event, EventType
from qpi_driver.sdk import QpiDriver


class RecordingDriver(QpiDriver):
    def __init__(self):
        super().__init__(qpi_addr="http://localhost:8090", token="t")
        self.handled: list[Event] = []

    def handle_event(self, event: Event) -> None:
        self.handled.append(event)


class FakeSocket:
    def __init__(self):
        self.sent: list[bytes] = []

    def send(self, payload: bytes) -> None:
        self.sent.append(payload)


def test_deliver_calls_handle_event():
    driver = RecordingDriver()
    event = Event(type=EventType.JOB_DISPATCH, payload={"job_id": "j1"})

    driver._deliver(event)

    assert driver.handled == [event]


def test_deliver_drops_when_handle_event_raises(caplog):
    class Boom(QpiDriver):
        def handle_event(self, event: Event) -> None:
            raise ValueError("boom")

    driver = Boom(qpi_addr="http://localhost:8090", token="t")

    driver._deliver(Event(type=EventType.JOB_DISPATCH))

    assert "handler failed" in caplog.text


def test_base_driver_is_abstract():
    with pytest.raises(TypeError):
        QpiDriver(qpi_addr="http://localhost:8090", token="t")


def test_emit_encodes_default_envelope_and_sends():
    driver = RecordingDriver()
    driver._out_sock = FakeSocket()

    driver.emit(
        Event(type=EventType.JOB_RESULT, driver="rec", payload={"job_id": "j1"})
    )

    sent = json.loads(driver._out_sock.sent[0])
    assert sent["type"] == "JobResult"
    assert sent["payload"] == {"job_id": "j1"}


def test_emit_before_running_raises():
    driver = RecordingDriver()

    with pytest.raises(RuntimeError):
        driver.emit(Event(type=EventType.JOB_RESULT))


def test_every_runs_registered_callback():
    driver = RecordingDriver()
    ticks: list[int] = []
    driver.every(0.01, lambda: ticks.append(1))

    thread = threading.Thread(
        target=driver._run_periodic, args=(0.01, lambda: ticks.append(1))
    )
    thread.start()
    time.sleep(0.05)
    driver._stop.set()
    thread.join(timeout=1)

    assert len(ticks) >= 1


def test_decode_inbound_parses_envelope():
    driver = RecordingDriver()
    envelope = Event(
        type=EventType.JOB_DISPATCH,
        payload={"job_id": "j1", "payload": {"circuits": []}},
    ).to_json()

    event = driver._decode_inbound(envelope.encode())

    assert event.type is EventType.JOB_DISPATCH
    assert event.payload == {"job_id": "j1", "payload": {"circuits": []}}


def test_decode_inbound_drops_malformed_message():
    driver = RecordingDriver()

    assert driver._decode_inbound(b"not an envelope") is None


class FakeQueue:
    def __init__(self):
        self.items: list = []

    def put(self, item) -> None:
        self.items.append(item)


def test_qpu_handle_event_enqueues_dispatched_job():
    driver = QpuDriver()
    driver._job_queue = FakeQueue()
    job = {"job_id": "j1", "payload": {"circuits": []}}

    driver.handle_event(Event(type=EventType.JOB_DISPATCH, payload=job))

    assert driver._job_queue.items == [job]


def test_qpu_handle_event_ignores_other_types(caplog):
    driver = QpuDriver()
    driver._job_queue = FakeQueue()

    driver.handle_event(Event(type=EventType.JOB_RESULT, payload={"job_id": "j1"}))

    assert driver._job_queue.items == []
    assert "does not handle" in caplog.text
