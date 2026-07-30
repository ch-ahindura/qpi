// Package qpidriver is the Go SDK for building QPI drivers (RFC 0001 §4).
//
// A driver is an external process that exchanges typed events with QPI-UI: it
// handles the events QPI-UI sends it and emits events of its own. Embed
// [Base] in a struct, implement [Handler.HandleEvent] to act on each inbound
// event (switching on its type), and call [Base.Emit] to send an event upward.
// [Base.Every] runs a callback on a timer, for drivers that report on their
// own schedule rather than in reply to a dispatch. [Run] performs the
// handshake, opens the transport, and blocks until the process is signalled to
// stop.
//
// It mirrors the Python SDK (qpi-driver/py/qpi_driver) and the TypeScript SDK
// (qpi-driver/js): the same envelope, the same drivers/connect handshake, and
// TLS with the pinned root CA.
package qpidriver

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"time"
)

// EventType is one of the fixed event types a QPI-UI version understands.
// Maintainers grow the framework by adding new types over releases.
type EventType string

const (
	// JobDispatch is sent by QPI-UI to a QPU driver: run this job.
	JobDispatch EventType = "JobDispatch"
	// JobResult is emitted by a QPU driver once a job finishes.
	JobResult EventType = "JobResult"
	// CryostatReading is emitted by a monitor driver on its own schedule.
	CryostatReading EventType = "CryostatReading"
	// CalibrateDispatch is sent by QPI-UI to a calibrate driver: tune this chip.
	CalibrateDispatch EventType = "CalibrateDispatch"
	// CalibrationResult is emitted by a calibrate driver once a run finishes.
	CalibrationResult EventType = "CalibrationResult"
)

// Event is a single typed message exchanged with QPI-UI in either direction.
// The Payload shape depends on Type and is validated by whoever handles the
// event. The wire form is the shared envelope {id, driver, type, ts, payload}
// (RFC 0001 §6).
type Event struct {
	// ID uniquely identifies this envelope; generated when empty.
	ID string `json:"id"`
	// Driver identifies the driver this event belongs to.
	Driver string `json:"driver"`
	// Type determines the payload shape.
	Type EventType `json:"type"`
	// Ts is the creation time, ISO-8601 UTC with millisecond precision;
	// generated when empty.
	Ts string `json:"ts"`
	// Payload is the type-specific body.
	Payload map[string]any `json:"payload"`
}

// NewEvent builds an event of the given type for a driver, filling in a fresh
// ID and timestamp. It is the idiomatic way to construct an event to Emit.
func NewEvent(eventType EventType, driver string, payload map[string]any) Event {
	if payload == nil {
		payload = map[string]any{}
	}
	return Event{
		ID:      newEventID(),
		Driver:  driver,
		Type:    eventType,
		Ts:      nowTimestamp(),
		Payload: payload,
	}
}

// MarshalJSON serialises the envelope, filling in ID, Ts, and a non-nil
// Payload so a zero-valued Event still produces a well-formed envelope.
func (e Event) MarshalJSON() ([]byte, error) {
	if e.ID == "" {
		e.ID = newEventID()
	}
	if e.Ts == "" {
		e.Ts = nowTimestamp()
	}
	if e.Payload == nil {
		e.Payload = map[string]any{}
	}
	type wire Event
	return json.Marshal(wire(e))
}

// decodeEvent parses a received wire message into an Event.
func decodeEvent(raw []byte) (Event, error) {
	var e Event
	if err := json.Unmarshal(raw, &e); err != nil {
		return Event{}, err
	}
	if e.Payload == nil {
		e.Payload = map[string]any{}
	}
	return e, nil
}

func newEventID() string {
	buf := make([]byte, 12)
	_, _ = rand.Read(buf)
	return "evt_" + hex.EncodeToString(buf)
}

func nowTimestamp() string {
	return time.Now().UTC().Format("2006-01-02T15:04:05.000") + "Z"
}
