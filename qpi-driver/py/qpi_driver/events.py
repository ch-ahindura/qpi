"""Event envelope shared with QPI-UI over NNG (RFC 0001 §4, §6).

A driver speaks the same typed events QPI-UI understands: it handles the events
QPI-UI sends it and emits events of its own. Every event travels in one envelope
whose payload shape depends on its type and is validated by whoever handles it.
"""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class EventType(str, Enum):
    """The fixed set of event types a QPI-UI version understands.

    Maintainers grow the framework by adding new types over releases.
    """

    JOB_DISPATCH = "JobDispatch"
    JOB_RESULT = "JobResult"
    CRYOSTAT_READING = "CryostatReading"
    CALIBRATE_DISPATCH = "CalibrateDispatch"
    CALIBRATION_RESULT = "CalibrationResult"
    CALIBRATION_PROGRESS = "CalibrationProgress"
    CALIBRATION_QUEUED = "CalibrationQueued"
    QPU_STATE = "QPUState"


@dataclass
class Event:
    """A single typed message exchanged with QPI-UI in either direction.

    Attributes:
        type: The event type, which determines the payload shape.
        payload: Type-specific body, validated by whoever handles the event.
        driver: Identifier of the driver this event belongs to.
        id: Unique identifier of this envelope.
        ts: Creation time as an ISO-8601 UTC timestamp with millisecond precision.
    """

    type: EventType
    payload: dict[str, Any] = field(default_factory=dict)
    driver: str = ""
    id: str = ""
    ts: str = ""

    def __post_init__(self) -> None:
        self.type = EventType(self.type)
        if not self.id:
            self.id = _new_event_id()
        if not self.ts:
            self.ts = _now_timestamp()

    def to_dict(self) -> dict[str, Any]:
        """Return the envelope as a plain dict matching the wire shape."""
        return {
            "id": self.id,
            "driver": self.driver,
            "type": self.type.value,
            "ts": self.ts,
            "payload": self.payload,
        }

    def to_json(self) -> str:
        """Serialise the envelope to a JSON string for sending over NNG."""
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Event":
        """Build an event from a decoded envelope dict."""
        return cls(
            type=EventType(data["type"]),
            payload=data.get("payload") or {},
            driver=data.get("driver", ""),
            id=data.get("id", ""),
            ts=data.get("ts", ""),
        )

    @classmethod
    def from_json(cls, raw: str | bytes) -> "Event":
        """Build an event from a JSON string or bytes received over NNG."""
        if isinstance(raw, bytes):
            raw = raw.decode()
        return cls.from_dict(json.loads(raw))


def _new_event_id() -> str:
    return "evt_" + os.urandom(12).hex()


def _now_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
