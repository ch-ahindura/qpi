"""The SDK's transport lifecycle: handshake, receive loop, timers, shutdown.

The e2e suite proves this works against a real server. What it cannot show is the
behaviour when things go wrong — a socket that times out, a handler that raises, a
malformed message, a CA whose fingerprint does not match. Those are the paths that
decide whether a driver on a lab bench keeps running or dies quietly, so they are
asserted here against fakes instead.
"""

import hashlib
import ssl
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
from qpi_driver.events import Event, EventType
from qpi_driver.sdk import Connection, QpiDriver, _download_root_ca_cert


class Recorder(QpiDriver):
    """A driver that records the events handed to it, and can be told to raise."""

    def __init__(self, explode: bool = False, **kwargs):
        kwargs.setdefault("qpi_addr", "http://127.0.0.1:8090")
        kwargs.setdefault("token", "tok")
        super().__init__(**kwargs)
        self.explode = explode
        self.handled: list[Event] = []

    def handle_event(self, event: Event) -> None:
        if self.explode:
            raise RuntimeError("handler is unhappy")
        self.handled.append(event)


class FakeSocket:
    """A pynng-shaped socket: hands out queued messages, then whatever was asked."""

    def __init__(self, messages=(), then=None):
        self.messages = list(messages)
        self.then = then
        self.sent: list[bytes] = []
        self.dialled: list[str] = []
        self.closed = False

    def dial(self, address, block=True):
        self.dialled.append(address)

    def send(self, payload):
        self.sent.append(payload)

    def recv(self):
        if self.messages:
            return self.messages.pop(0)
        if self.then is not None:
            # pynng's exceptions take (msg, errno), so an instance rather than a class.
            raise self.then("socket closed", 0)
        raise AssertionError("recv called with nothing left to give")

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
        return False


def _connection() -> Connection:
    return Connection(
        name="test-driver",
        host="127.0.0.1",
        in_port=1,
        out_port=2,
        ca_file="/tmp/ca.pem",
    )


def _wire(event_type=EventType.JOB_DISPATCH, payload=None) -> bytes:
    return (
        Event(type=event_type, driver="qpi-ui", payload=payload or {})
        .to_json()
        .encode()
    )


class TestRecvLoop:
    def test_it_delivers_each_event_until_the_socket_closes(self):
        import pynng

        driver = Recorder()
        socket = FakeSocket(
            [_wire(), _wire(EventType.CRYOSTAT_READING)], then=pynng.Closed
        )

        with patch("pynng.Pull0", return_value=socket):
            driver._recv_loop(_connection(), tls_config=None)

        assert [event.type for event in driver.handled] == [
            EventType.JOB_DISPATCH,
            EventType.CRYOSTAT_READING,
        ]
        assert socket.dialled == ["tls+tcp://127.0.0.1:1"]
        assert socket.closed is True

    def test_a_timeout_is_not_an_error(self):
        """The timeout is how the loop checks for shutdown; it must just go round."""
        import pynng

        driver = Recorder()

        class TimesOutThenStops(FakeSocket):
            def __init__(self, outer):
                super().__init__()
                self.outer = outer
                self.attempts = 0

            def recv(self):
                self.attempts += 1
                if self.attempts == 1:
                    raise pynng.Timeout("timed out", 0)
                self.outer._stop.set()
                raise pynng.Timeout("timed out", 0)

        socket = TimesOutThenStops(driver)
        with patch("pynng.Pull0", return_value=socket):
            driver._recv_loop(_connection(), tls_config=None)

        assert socket.attempts == 2
        assert driver.handled == []

    def test_stopping_ends_the_loop(self):
        driver = Recorder()
        driver._stop.set()
        socket = FakeSocket()

        with patch("pynng.Pull0", return_value=socket):
            driver._recv_loop(_connection(), tls_config=None)

        assert driver.handled == []

    def test_a_malformed_message_is_dropped_not_fatal(self):
        """One bad message must not take the driver down (RFC 0001 §8)."""
        import pynng

        driver = Recorder()
        socket = FakeSocket([b"{not an envelope", _wire()], then=pynng.Closed)

        with patch("pynng.Pull0", return_value=socket):
            driver._recv_loop(_connection(), tls_config=None)

        assert len(driver.handled) == 1

    def test_a_handler_that_raises_is_logged_and_dropped(self):
        """A rejected event is not a reason to stop receiving the next one."""
        import pynng

        driver = Recorder(explode=True)
        socket = FakeSocket([_wire(), _wire()], then=pynng.Closed)

        with patch("pynng.Pull0", return_value=socket):
            driver._recv_loop(_connection(), tls_config=None)

        assert driver.handled == []  # both raised, neither killed the loop


class TestEmit:
    def test_it_sends_the_envelope_as_given(self):
        """The event goes out as the caller built it.

        Unlike the TypeScript SDK, this one does not stamp the driver name — the
        caller sets it (see `QpuDriver._pump_results`). Asserted here so the
        difference is recorded rather than discovered.
        """
        driver = Recorder()
        driver._out_sock = FakeSocket()

        driver.emit(
            Event(
                type=EventType.JOB_RESULT, driver="test-driver", payload={"job_id": "a"}
            )
        )

        assert len(driver._out_sock.sent) == 1
        decoded = Event.from_json(driver._out_sock.sent[0])
        assert decoded.driver == "test-driver"
        assert decoded.payload == {"job_id": "a"}

    def test_emitting_before_the_socket_exists_is_reported(self):
        with pytest.raises(RuntimeError, match="not connected|before"):
            Recorder().emit(Event(type=EventType.JOB_RESULT))


class TestPeriodic:
    def test_every_registers_a_callback_that_runs_until_stopped(self):
        driver = Recorder()
        ticks = threading.Event()

        driver.every(0.01, ticks.set)
        driver._start_periodic()
        try:
            assert ticks.wait(timeout=5), "expected the periodic callback to run"
        finally:
            driver._stop.set()

    def test_a_periodic_callback_that_raises_keeps_ticking(self):
        """A monitor whose hardware is briefly unreachable must not stop polling."""
        driver = Recorder()
        calls = {"n": 0}

        def unreliable():
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError("cryostat not answering")
            driver._stop.set()

        driver.every(0.01, unreliable)
        driver._start_periodic()
        for thread in driver._threads:
            thread.join(timeout=5)

        assert calls["n"] >= 3


class TestRun:
    """The whole sequence, with pynng and the handshake faked out.

    Worth asserting as a sequence rather than only per-step: the order is what makes
    the driver correct — the outbound channel must be open before `_on_start` spawns
    anything that might emit, and `_shutdown` must run even when the loop raises.
    """

    def _driver(self, **kwargs) -> Recorder:
        driver = Recorder(**kwargs)
        driver._connect = lambda: _connection()
        return driver

    def test_it_dials_out_starts_the_hooks_then_loops_and_shuts_down(self):
        driver = self._driver()
        order: list[str] = []
        out = FakeSocket()

        driver._on_start = lambda: order.append("on_start")
        driver._on_stop = lambda: order.append("on_stop")
        driver._recv_loop = lambda conn, tls: order.append("recv_loop")

        with patch("pynng.Push0", return_value=out), patch("qpi_driver.sdk.TLSConfig"):
            driver.run()

        assert order == ["on_start", "recv_loop", "on_stop"]
        assert out.dialled == ["tls+tcp://127.0.0.1:2"]
        assert out.closed is True

    def test_a_keyboard_interrupt_shuts_down_cleanly(self):
        """Ctrl-C on a lab bench must release the socket, not leave it dangling."""
        driver = self._driver()
        out = FakeSocket()
        stopped = []
        driver._on_stop = lambda: stopped.append(True)

        def interrupted(conn, tls):
            raise KeyboardInterrupt

        driver._recv_loop = interrupted

        with patch("pynng.Push0", return_value=out), patch("qpi_driver.sdk.TLSConfig"):
            driver.run()  # must not propagate

        assert stopped == [True]
        assert out.closed is True

    def test_the_periodic_callbacks_start_before_the_loop(self):
        """A monitor's timer must be running by the time the loop blocks."""
        driver = self._driver()
        ticked = threading.Event()
        driver.every(0.01, ticked.set)
        driver._recv_loop = lambda conn, tls: ticked.wait(timeout=5)

        with (
            patch("pynng.Push0", return_value=FakeSocket()),
            patch("qpi_driver.sdk.TLSConfig"),
        ):
            driver.run()

        assert ticked.is_set()


class TestShutdown:
    def test_it_stops_the_timers_closes_the_socket_and_runs_the_hook(self):
        driver = Recorder()
        driver._out_sock = FakeSocket()
        stopped = []
        driver._on_stop = lambda: stopped.append(True)

        driver._shutdown()

        assert driver._stop.is_set()
        assert stopped == [True]
        assert driver._out_sock is None

    def test_it_is_safe_with_no_socket(self):
        """Shutdown can happen before the outbound channel ever opened."""
        Recorder()._shutdown()


class TestConnect:
    def _response(self, payload):
        class Response:
            def json(self):
                return payload

            def raise_for_status(self):
                pass

        return Response()

    def test_the_handshake_returns_the_ports_and_the_ca(self, tmp_path):
        driver = Recorder(ca_file_path=(tmp_path / "ca.pem").as_posix())
        sent = {}

        def post(url, json, timeout):
            sent["url"] = url
            sent["json"] = json
            return self._response(
                {
                    "name": "cryostat-1",
                    "nng_host": "10.0.0.1",
                    "nng_in_port": "7001",
                    "nng_out_port": 7002,
                }
            )

        with (
            patch("requests.post", post),
            patch(
                "qpi_driver.sdk._download_root_ca_cert",
                lambda *a, **k: "/tmp/ca.pem",
            ),
        ):
            conn = driver._connect()

        assert sent["url"].endswith("/api/op/drivers/connect")
        # The token is the whole of the identity asserted: the driver does not send
        # a name, it is told one.
        assert sent["json"] == {"token": "tok"}
        assert conn.name == "cryostat-1"
        # Ports arrive as strings or ints depending on the server's JSON encoder.
        assert (conn.host, conn.in_port, conn.out_port) == ("10.0.0.1", 7001, 7002)


class TestRootCaDownload:
    """The pinned-CA check, which is the driver's only defence on a shared network."""

    # A self-signed certificate checked in as a fixture, with its SHA-256. Generated
    # rather than built here, so the test needs no certificate library:
    #   openssl req -x509 -newkey rsa:2048 -nodes -days 36500 \
    #     -subj "/CN=qpi-test-fixture" -keyout /dev/null -out cert.pem
    CERT = """-----BEGIN CERTIFICATE-----
MIIDGTCCAgGgAwIBAgIUZnOMPVk1WAZvkR65nQUwSbOYed0wDQYJKoZIhvcNAQEL
BQAwGzEZMBcGA1UEAwwQcXBpLXRlc3QtZml4dHVyZTAgFw0yNjA3MjgxMDM1NTVa
GA8yMTI2MDcwNDEwMzU1NVowGzEZMBcGA1UEAwwQcXBpLXRlc3QtZml4dHVyZTCC
ASIwDQYJKoZIhvcNAQEBBQADggEPADCCAQoCggEBAKtjwAVTfdiiSSIheF7D9aSC
sTpYgnfwZtU7IO5gsm+RN7N8lOx6bd3reF6RR9jbJA/AdhNZiS348NdtQC6nkBdV
p+Pokq9bKvvyf0bAWfa+1aTFNelN/WKSxS6hac9NKQqkEpUOu1YS0JR76RmkMetm
Pc3CZBEuQ25toLbJGku0XvFEYALjd1PWgfE64+zmeg9t2mmp71fPb/xbvLoOXZYN
+LsQKFtL4RkbgatxH6FmiA0ChHT6qD7FxbEpegrzjcLt4mgSaeh7Ieol8Ydu4lhG
paTPVbKfVCtcUIItzm9+2kipE4JDvoCeEqAgk2CZwnG53wh6YDC2DOzzr3CigOcC
AwEAAaNTMFEwHQYDVR0OBBYEFLV0J8A+418QB0vZ5Etv7fWuvrOUMB8GA1UdIwQY
MBaAFLV0J8A+418QB0vZ5Etv7fWuvrOUMA8GA1UdEwEB/wQFMAMBAf8wDQYJKoZI
hvcNAQELBQADggEBAAWNuux3Yqxa6zw8UCHloh44wyb7WCZuea71nFNsiRINUMQG
zzsYRDGP8nopPJFkgbfgGUlUhqPy3fORu/kuvs49Fe9al/c4gyTPuxJpF+hLegDy
s0UmOROWO59sbJnZugf/xDVd5wxbcLvi9tF553uRAqfplmJFf/hYqVsZanPxQyjY
RrI+FF/vryH6YmDFBH2ElU+u30HfIGznj5ssIpdXlOVlXm06fB49lXbOTfO0QYpv
SAwwUH2xu0LHqV4L+2piUtDe/Vhccs0E1ccUBWPcOqCN+5JD+e4/J8o0B0jMYqVI
aPje+9tWIfmFmUdJ1Hi7VlNQyBau8VWy7nJcu6M=
-----END CERTIFICATE-----
"""
    FINGERPRINT = "a33bc5cc4d8f8337c82b500b681facb3078795567467926f810d45235a61b978"

    def _get(self, text):
        class Response:
            def __init__(self):
                self.text = text

            def raise_for_status(self):
                pass

        return lambda url, timeout=10: Response()

    def test_the_fixture_certificate_matches_its_fingerprint(self):
        """Guards the fixture itself: a stale PEM would make the tests below vacuous."""
        der = ssl.PEM_cert_to_DER_cert(self.CERT)
        assert hashlib.sha256(der).hexdigest() == self.FINGERPRINT

    def test_a_matching_fingerprint_writes_the_certificate(self, tmp_path):
        destination = tmp_path / "nested" / "qpi.ca.pem"

        with patch("requests.get", self._get(self.CERT)):
            written = _download_root_ca_cert(
                "http://127.0.0.1:8090", self.FINGERPRINT, destination
            )

        assert Path(written) == destination
        assert destination.read_text() == self.CERT

    def test_a_mismatched_fingerprint_aborts_and_writes_nothing(self, tmp_path):
        """The whole point of pinning: a wrong CA must never reach the disk."""
        destination = tmp_path / "qpi.ca.pem"

        with patch("requests.get", self._get(self.CERT)):
            with pytest.raises(RuntimeError, match="CRITICAL SECURITY ERROR"):
                _download_root_ca_cert("http://127.0.0.1:8090", "deadbeef", destination)

        assert not destination.exists()


def test_decode_and_encode_round_trip():
    driver = Recorder()
    event = Event(type=EventType.JOB_RESULT, driver="d", payload={"job_id": "a"})

    decoded = driver._decode_inbound(driver._encode_outbound(event))

    assert decoded is not None
    assert decoded.type is EventType.JOB_RESULT
    assert decoded.payload == {"job_id": "a"}
