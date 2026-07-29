"""Cryostat monitor for Bluefors Control Software Gen. 1 (RFC 0001 §7, Phase 3).

Note the *Gen. 1*: Bluefors also ships a Gen. 2 Control Software with its own
API, which is out of scope here and would need its own driver if supported.

A driver that only reports upward: it never handles ``JobDispatch`` (it is
not a QPU), it just polls the Bluefors Remote Access Control API Gen. 1's HTTP
``values`` endpoint on a timer and emits a ``CryostatReading`` event with
whatever it read (Bluefors Remote Access Control API Gen. 1 Technical
Reference, "Program structure" and Appendix I: ``values``).

The value tree path for a channel (e.g. ``mapper.bf.tmc``) is exactly what the
Bluefors API endpoint path is, with dots replaced by slashes, e.g.
``GET {base_url}/values/mapper/bf/tmc``. Which channels exist, and their
names, depend on how a given system's mappers are configured, so they are
supplied as configuration rather than hard-coded.

Ships as an officially maintained driver — install with
``qpi-driver[cli,bluefors_gen1]`` and run with ``qpi-driver start --operation
monitor --device bluefors_gen1`` (see ``qpi_driver.cli``), the same tier as the
qblox/quantify executors.
"""

import logging
from typing import Any

import requests

from qpi_driver.builtins.qpu import _normalize_qpi_addr
from qpi_driver.builtins.registry import DeviceSpec, Operation
from qpi_driver.events import Event, EventType
from qpi_driver.options import Options
from qpi_driver.sdk import DEFAULT_RECV_TIMEOUT_MS, QpiDriver

log = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = 5.0
DEFAULT_TIMEOUT = 5.0


class BlueforsGen1Driver(QpiDriver):
    """Polls Bluefors Gen. 1 Control API channels and emits readings on a timer.

    Attributes:
        bluefors_base_url: Base URL of the Bluefors Control API, e.g.
            ``http://localhost:49099``.
        channels: Maps a value-tree channel path (e.g. ``"mapper.bf.tmc"``) to
            a display unit (e.g. ``"K"``), which the Bluefors API's basic read
            response does not itself report. An empty unit is fine.
        api_key: Optional Bluefors API access key, sent as the ``key`` query
            parameter (Bluefors reference §3.5.1).
        poll_interval: Seconds between polls.
        timeout: HTTP timeout per channel read, in seconds.
    """

    def __init__(
        self,
        qpi_addr: str = "http://127.0.0.1:8090",
        token: str = "",
        bluefors_base_url: str = "http://127.0.0.1:49099",
        channels: dict[str, str] | list[str] | None = None,
        api_key: str = "",
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        timeout: float = DEFAULT_TIMEOUT,
        ca_fingerprint: str = "",
        ca_file_path: str = "./bin/qpi.ca.pem",
        recv_timeout_ms: int = DEFAULT_RECV_TIMEOUT_MS,
    ) -> None:
        super().__init__(
            qpi_addr=_normalize_qpi_addr(qpi_addr),
            token=token,
            ca_fingerprint=ca_fingerprint,
            ca_file_path=ca_file_path,
            recv_timeout_ms=recv_timeout_ms,
        )
        self.bluefors_base_url = bluefors_base_url.rstrip("/")
        self.channels = normalize_channels(channels)
        self.api_key = api_key
        self.poll_interval = poll_interval
        self.timeout = timeout

        self.every(self.poll_interval, self._poll)

    def handle_event(self, event: Event) -> None:
        """Ignore every inbound event — the monitor only reports upward.

        It never handles ``JobDispatch``; it is a separate driver from the
        QPU, not part of it (RFC 0001 §4).
        """
        log.warning(
            "dropping event %s: bluefors_gen1 driver does not handle %s",
            event.id,
            event.type.value,
        )

    def _poll(self) -> None:
        """Read every configured channel and emit whatever succeeded.

        A channel that fails to read (timeout, HTTP error, unexpected shape)
        is recorded with a ``None`` value and an ``ERROR`` status rather than
        raising, so one bad channel does not lose the rest of the tick. If
        every channel fails, nothing is emitted for this tick.
        """
        readings: dict[str, dict[str, Any]] = {}
        for channel, unit in self.channels.items():
            readings[channel] = self._read_channel(channel, unit)

        if not any(r["status"] != "ERROR" for r in readings.values()):
            log.warning(
                "all %d channel(s) failed this tick; skipping emit", len(readings)
            )
            return

        self.emit(
            Event(
                type=EventType.CRYOSTAT_READING,
                driver=self.name,
                payload={"readings": readings},
            )
        )

    def _read_channel(self, channel: str, unit: str) -> dict[str, Any]:
        """Read a single value-tree channel from the Bluefors Control API.

        Mirrors the "values" endpoint example in the Bluefors reference: GET
        the endpoint path (channel with dots replaced by slashes) and read
        ``data.content.latest_valid_value``, falling back to
        ``latest_value`` if there is no recent valid sample.
        """
        url = f"{self.bluefors_base_url}/values/{channel.replace('.', '/')}"
        params = {"key": self.api_key} if self.api_key else {}

        try:
            resp = requests.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            content = resp.json()["data"]["content"]
            sample = (
                content.get("latest_valid_value") or content.get("latest_value") or {}
            )
            raw_value = sample.get("value")
            value = float(raw_value) if raw_value not in (None, "") else None
            status = sample.get("status", "UNKNOWN")
        except Exception:
            log.exception("failed to read channel %s", channel)
            return {"value": None, "unit": unit, "status": "ERROR"}

        return {"value": value, "unit": unit, "status": status}


def normalize_channels(channels: dict[str, str] | list[str] | None) -> dict[str, str]:
    """Turn a channel list/dict/None into the ``{path: unit}`` shape the driver uses."""
    if channels is None:
        return {}
    if isinstance(channels, dict):
        return dict(channels)
    return {channel: "" for channel in channels}


def parse_channels(raw: str) -> dict[str, str]:
    """Parse ``"path[:unit],path[:unit],..."`` into a channel->unit dict.

    Used by the CLI, which only has a single string option to work with.
    """
    channels: dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        path, _, unit = part.partition(":")
        channels[path.strip()] = unit.strip()
    return channels


def build_from_options(
    *,
    qpi_addr: str,
    token: str,
    ca_fingerprint: str,
    ca_file_path: str,
    recv_timeout_ms: int,
    options: Options,
) -> BlueforsGen1Driver:
    """Build an unstarted driver from its ``-o`` options.

    The options this monitor reads are the ones read here. Only ``channels`` is
    unknowable in advance — which channels a system exposes depends on how its
    mappers are configured (see the module docstring) — so it is the one with no
    default and the one whose absence is an error.
    """
    return BlueforsGen1Driver(
        qpi_addr=qpi_addr,
        token=token,
        bluefors_base_url=options.get_str("base_url", "http://127.0.0.1:49099"),
        channels=parse_channels(
            options.require("channels", "mapper.bf.tmc:K,mapper.bf.pmc:mbar")
        ),
        api_key=options.get_str("api_key"),
        poll_interval=options.get_float("poll_interval", DEFAULT_POLL_INTERVAL),
        timeout=options.get_float("timeout", DEFAULT_TIMEOUT),
        ca_fingerprint=ca_fingerprint,
        ca_file_path=ca_file_path,
        recv_timeout_ms=recv_timeout_ms,
    )


DEVICE_SPEC = DeviceSpec(
    name="bluefors_gen1",
    operation=Operation.MONITOR,
    build=build_from_options,
)
