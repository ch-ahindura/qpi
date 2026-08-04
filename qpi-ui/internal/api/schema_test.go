package api

import (
	"encoding/json"
	"testing"

	"qpi/internal/drivers"
)

// TestCatalogEventsAreKnownTypes guards the one seam between the driver catalog
// and the wire protocol: the catalog names the events each kind participates in
// as plain strings (it is a leaf package and cannot import the api event
// types), so this asserts every one of those strings is an EventType QPI-UI
// actually has a handler for. It fails loudly if the two ever drift.
func TestCatalogEventsAreKnownTypes(t *testing.T) {
	for _, kind := range drivers.Default.Kinds() {
		for _, event := range drivers.Default.Events(kind) {
			if !isKnownEventType(EventType(event)) {
				t.Errorf("catalog kind %q lists unknown event type %q", kind, event)
			}
		}
	}
}

// TestCalibrateDispatchPayload_RoundTripsAsTheDriverReadsIt proves the wire
// shape the Python driver parses.
func TestCalibrateDispatchPayload_RoundTripsAsTheDriverReadsIt(t *testing.T) {
	payload := CalibrateDispatchPayload{
		JobID: "req-1", Mode: "partial", TargetQubits: []string{"q0", "q1"},
	}
	raw, err := json.Marshal(payload)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}

	var decoded map[string]any
	if err := json.Unmarshal(raw, &decoded); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	for _, key := range []string{"job_id", "mode", "target_qubits"} {
		if _, ok := decoded[key]; !ok {
			t.Errorf("expected %q in the dispatch payload, got %v", key, decoded)
		}
	}
}
