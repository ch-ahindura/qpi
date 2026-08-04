"""Unit tests for the Bluefors Gen. 1 cryostat monitor driver (RFC 0001 §7).

These exercise channel reading, tick-level emit/skip behaviour, and channel
argument parsing against a mocked ``requests.get`` — no real Bluefors Control
API or NNG socket involved. The transport itself is covered by the SDK's own
tests and the e2e suite.
"""

import json
from unittest.mock import Mock, patch

import pytest
from qpi_driver.builtins.bluefors_gen1 import (
    BlueforsGen1Driver,
    build_from_options,
    normalize_channels,
    parse_channels,
)
from qpi_driver.events import Event, EventType
from qpi_driver.options import Options


class FakeSocket:
    def __init__(self):
        self.sent: list[bytes] = []

    def send(self, payload: bytes) -> None:
        self.sent.append(payload)


def _bluefors_response(value: str, status: str = "SYNCHRONIZED") -> Mock:
    resp = Mock()
    resp.raise_for_status = Mock()
    resp.json.return_value = {
        "data": {
            "name": "mapper.bf.tmc",
            "type": "Value.Number.Float",
            "content": {
                "read_only": True,
                "latest_valid_value": {
                    "value": value,
                    "outdated": False,
                    "date": 1631106116076,
                    "status": status,
                    "exception": "",
                },
                "latest_value": {
                    "value": value,
                    "outdated": False,
                    "date": 1631106116076,
                    "status": status,
                    "exception": "",
                },
            },
        }
    }
    return resp


def _driver(**kwargs) -> BlueforsGen1Driver:
    defaults = dict(
        qpi_addr="http://localhost:8090",
        token="t",
        bluefors_base_url="http://localhost:49099",
        channels={"mapper.bf.tmc": "K"},
        poll_interval=0.01,
    )
    defaults.update(kwargs)
    return BlueforsGen1Driver(**defaults)


def test_handle_event_ignores_everything(caplog):
    driver = _driver()

    driver.handle_event(Event(type=EventType.JOB_DISPATCH, payload={"job_id": "j1"}))

    assert "does not handle" in caplog.text


def test_read_channel_parses_latest_valid_value():
    driver = _driver()

    with patch("requests.get", return_value=_bluefors_response("0.0123")) as get:
        reading = driver.read_channel("mapper.bf.tmc", "K")

    url = get.call_args.args[0]
    assert url == "http://localhost:49099/values/mapper/bf/tmc"
    assert reading == {"value": 0.0123, "unit": "K", "status": "SYNCHRONIZED"}


def test_read_channel_sends_api_key_query_param():
    driver = _driver(api_key="secret-key")

    with patch("requests.get", return_value=_bluefors_response("1.0")) as get:
        driver.read_channel("mapper.bf.tmc", "K")

    assert get.call_args.kwargs["params"] == {"key": "secret-key"}


def test_read_channel_reports_error_status_on_http_failure(caplog):
    driver = _driver()

    with patch("requests.get", side_effect=ConnectionError("boom")):
        reading = driver.read_channel("mapper.bf.tmc", "K")

    assert reading == {"value": None, "unit": "K", "status": "ERROR"}
    assert "failed to read channel" in caplog.text


def test_read_channel_reports_error_status_on_malformed_response():
    driver = _driver()
    bad_resp = Mock()
    bad_resp.raise_for_status = Mock()
    bad_resp.json.return_value = {"unexpected": "shape"}

    with patch("requests.get", return_value=bad_resp):
        reading = driver.read_channel("mapper.bf.tmc", "K")

    assert reading["status"] == "ERROR"
    assert reading["value"] is None


def test_poll_emits_event_with_readings():
    driver = _driver()
    driver._out_sock = FakeSocket()

    with patch("requests.get", return_value=_bluefors_response("4.2")):
        driver._poll()

    assert len(driver._out_sock.sent) == 1
    sent = json.loads(driver._out_sock.sent[0])
    assert sent["type"] == "CryostatReading"
    assert sent["payload"]["readings"]["mapper.bf.tmc"] == {
        "value": 4.2,
        "unit": "K",
        "status": "SYNCHRONIZED",
    }


# A fridge under maintenance is exactly when its telemetry matters most, so the
# state event must not quiet a monitor. A SLEEP/WAKE signal would have.
def test_a_monitor_keeps_reporting_whatever_state_the_qpu_is_in():
    from qpi_driver.events import Event, EventType

    for state in ("online", "maintenance", "disabled"):
        driver = _driver()
        driver._out_sock = FakeSocket()
        driver.handle_event(
            Event(type=EventType.QPU_STATE, payload={"state": state}, driver="d")
        )

        with patch("requests.get", return_value=_bluefors_response("4.2")):
            driver._poll()

        assert len(driver._out_sock.sent) == 1, f"silenced while {state}"


def test_poll_skips_emit_when_every_channel_fails():
    driver = _driver()
    driver._out_sock = FakeSocket()

    with patch("requests.get", side_effect=ConnectionError("boom")):
        driver._poll()

    assert driver._out_sock.sent == []


def test_poll_emits_partial_readings_when_some_channels_fail():
    driver = _driver(channels={"mapper.bf.tmc": "K", "mapper.bf.tstill": "K"})
    driver._out_sock = FakeSocket()

    def fake_get(url, params=None, timeout=None):
        if url.endswith("tmc"):
            return _bluefors_response("0.05")
        raise ConnectionError("boom")

    with patch("requests.get", side_effect=fake_get):
        driver._poll()

    assert len(driver._out_sock.sent) == 1
    readings = json.loads(driver._out_sock.sent[0])["payload"]["readings"]
    assert readings["mapper.bf.tmc"]["status"] == "SYNCHRONIZED"
    assert readings["mapper.bf.tstill"]["status"] == "ERROR"


def test_a_supplied_channel_reader_is_the_seam():
    """A monitor for different control software reuses everything but the read.

    Composition, not inheritance — the same way every `process` device is the one
    QPU driver over a different `Executor`. The timer, one bad channel not losing
    the rest of the tick, and the CryostatReading event all stay.
    """
    asked: list[str] = []

    def gen2_reader(channel: str, unit: str) -> dict:
        asked.append(channel)
        return {"value": 0.02, "unit": unit, "status": "OK"}

    driver = _driver(channels={"gen2.temperature": "K"}, read_channel=gen2_reader)
    driver._out_sock = FakeSocket()

    # No `requests` patching: the supplied reader never reaches the network.
    driver._poll()

    assert asked == ["gen2.temperature"]
    sent = json.loads(driver._out_sock.sent[0])
    assert sent["type"] == "CryostatReading"
    assert sent["payload"]["readings"]["gen2.temperature"] == {
        "value": 0.02,
        "unit": "K",
        "status": "OK",
    }


def test_normalize_channels_accepts_list_dict_or_none():
    assert normalize_channels(None) == {}
    assert normalize_channels(["mapper.bf.tmc"]) == {"mapper.bf.tmc": ""}
    assert normalize_channels({"mapper.bf.tmc": "K"}) == {"mapper.bf.tmc": "K"}


def _common_options() -> dict:
    return dict(
        qpi_addr="http://localhost:8090",
        token="t",
        ca_fingerprint="fp",
        ca_file_path="./bin/qpi.ca.pem",
        recv_timeout_ms=200,
    )


def _options(**raw: str) -> Options:
    """The raw ``-o`` strings this device is handed, as the CLI hands them over."""
    return Options(raw)


def test_build_from_options_reads_all_keys():
    driver = build_from_options(
        **_common_options(),
        options=_options(
            base_url="http://cryo:49099",
            channels="mapper.bf.tmc:K,mapper.bf.pmc:mbar",
            api_key="secret",
            poll_interval="2.5",
            timeout="7",
        ),
    )

    assert isinstance(driver, BlueforsGen1Driver)
    assert driver.bluefors_base_url == "http://cryo:49099"
    assert driver.channels == {"mapper.bf.tmc": "K", "mapper.bf.pmc": "mbar"}
    assert driver.api_key == "secret"
    assert driver.poll_interval == 2.5
    assert driver.timeout == 7.0


def test_defaults_every_key_but_channels():
    """Only the channels are unknowable in advance, so only they are required."""
    driver = build_from_options(
        **_common_options(), options=_options(channels="mapper.bf.tmc")
    )

    assert driver.bluefors_base_url == "http://127.0.0.1:49099"
    assert driver.api_key == ""
    assert driver.poll_interval == 5.0
    assert driver.timeout == 5.0


def test_channels_are_required():
    """It refuses to build without channels, naming the option."""
    with pytest.raises(ValueError, match="'channels'"):
        build_from_options(**_common_options(), options=_options(base_url="http://x"))


def test_an_option_this_monitor_does_not_read_is_left_unread():
    """What the device reads is its whole schema, so the CLI can flag the rest."""
    options = _options(channels="mapper.bf.tmc", pol_interval="2")
    build_from_options(**_common_options(), options=options)

    assert options.unread() == ("pol_interval",)


def test_parse_channels_handles_optional_units():
    parsed = parse_channels("mapper.bf.tmc:K, mapper.bf.pmc :mbar,mapper.bf.flow")

    assert parsed == {
        "mapper.bf.tmc": "K",
        "mapper.bf.pmc": "mbar",
        "mapper.bf.flow": "",
    }


def test_parse_channels_ignores_empty_segments():
    """A trailing comma or a stray space is not a channel named "".

    Worth tolerating: these strings are typed into unit files by hand.
    """
    assert parse_channels("mapper.bf.tmc:K, ,mapper.bf.pmc:mbar,") == {
        "mapper.bf.tmc": "K",
        "mapper.bf.pmc": "mbar",
    }
    assert parse_channels("") == {}
