package api

import (
	"context"
	"encoding/json"
	"strings"
	"testing"

	"github.com/pocketbase/dbx"

	"qpi/internal/db"
	"qpi/internal/scheduler"
)

// TestHandleCalibrationResult_PersistsTheReport proves a report reaches the
// calibration_results collection with its fields intact, attributed to both the
// driver that sent it and the QPU that driver belongs to (RFC 0004 §6.8).
func TestHandleCalibrationResult_PersistsTheReport(t *testing.T) {
	app, cfg, driverRec, qpuRec := seedDriverForEvents(t)

	event, err := NewEvent(driverRec.Id, EventCalibrationResult, CalibrationResultPayload{
		Timestamp: "2026-07-30T12:00:00.000Z",
		DurationS: 42.5,
		Mode:      "full",
		Backend:   "quantify",
		Status:    "success",
		RoutineResults: []RoutineResult{
			{RoutineName: "rabi", Target: "q0", DurationS: 1.5},
		},
		Benchmarks: []BenchmarkResult{
			{Protocol: "rb", Target: "q0", Fidelity: floatPtr(0.9994)},
		},
	})
	if err != nil {
		t.Fatalf("build event: %v", err)
	}

	ctx := context.WithValue(context.Background(), driverIDContextKey{}, driverRec.Id)
	if err := handleCalibrationResult(ctx, app, qpuRec.Id, event); err != nil {
		t.Fatalf("handleCalibrationResult: %v", err)
	}

	var rows []db.CalibrationResult
	err = db.FindMany(app, cfg.CollectionCalibrationResults, &rows, "mode = {:mode}", "", 10, 0,
		dbx.Params{"mode": "full"})
	if err != nil {
		t.Fatalf("find calibration results: %v", err)
	}
	if len(rows) != 1 {
		t.Fatalf("expected 1 calibration result, got %d", len(rows))
	}
	if rows[0].Driver != driverRec.Id {
		t.Errorf("expected driver %s, got %s", driverRec.Id, rows[0].Driver)
	}
	if rows[0].QPU != qpuRec.Id {
		t.Errorf("expected qpu %s, got %s — a report that cannot be attributed to a chip is not a record", qpuRec.Id, rows[0].QPU)
	}
	if rows[0].Status != "success" || rows[0].DurationS != 42.5 {
		t.Errorf("expected the report's own fields, got status=%q duration=%v", rows[0].Status, rows[0].DurationS)
	}
	if rows[0].Backend != "quantify" {
		t.Errorf("expected backend quantify, got %q", rows[0].Backend)
	}
}

// TestHandleCalibrationResult_RejectsABlankReport proves an empty payload is
// refused rather than stored.
//
// This is the shape a nested payload arrives as: `{"job_id":…,"results":{…}}`
// unmarshals into this struct without error and leaves every field zero. Saving
// it would put a blank row in front of whoever is trying to work out what the
// chip is doing, for a calibration that really ran.
func TestHandleCalibrationResult_RejectsABlankReport(t *testing.T) {
	app, _, driverRec, qpuRec := seedDriverForEvents(t)

	event, err := NewEvent(driverRec.Id, EventCalibrationResult, map[string]any{
		"job_id":  "cal-1",
		"results": map[string]any{"mode": "full", "status": "success"},
	})
	if err != nil {
		t.Fatalf("build event: %v", err)
	}

	ctx := context.WithValue(context.Background(), driverIDContextKey{}, driverRec.Id)
	if err := handleCalibrationResult(ctx, app, qpuRec.Id, event); err == nil {
		t.Fatal("expected a payload with no mode or status to be rejected")
	}
}

// TestHandleCalibrationResult_RejectsAVocabularyTheColumnLacks: the payload fills
// two select columns, and an insert failure in the listener is only an error.
func TestHandleCalibrationResult_RejectsAVocabularyTheColumnLacks(t *testing.T) {
	app, _, driverRec, qpuRec := seedDriverForEvents(t)

	event, err := NewEvent(driverRec.Id, EventCalibrationResult, CalibrationResultPayload{
		Timestamp: "2026-07-30T12:00:00.000Z",
		Mode:      "full",
		Status:    "aborted",
	})
	if err != nil {
		t.Fatalf("build event: %v", err)
	}

	ctx := context.WithValue(context.Background(), driverIDContextKey{}, driverRec.Id)
	err = handleCalibrationResult(ctx, app, qpuRec.Id, event)
	if err == nil {
		t.Fatal("expected an unknown status to be rejected")
	}
	// Both, or it says no more than the insert would have.
	for _, want := range []string{"aborted", "partial_failure"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("error %q does not mention %q", err, want)
		}
	}
}

// TestHandleCalibrationResult_ClosesOutItsRequest proves a report releases the
// queued request it answers, so the dispatcher can offer the next one.
func TestHandleCalibrationResult_ClosesOutItsRequest(t *testing.T) {
	app, cfg, driverRec, qpuRec := seedDriverForEvents(t)

	request := &db.CalibrationRequest{
		Driver: driverRec.Id, Mode: "full", Status: "running",
	}
	if err := saveToDb(app, request); err != nil {
		t.Fatalf("seed request: %v", err)
	}

	event, err := NewEvent(driverRec.Id, EventCalibrationResult, CalibrationResultPayload{
		JobID: request.ID, Timestamp: "2026-07-30T12:00:00.000Z",
		Mode: "full", Status: "success",
	})
	if err != nil {
		t.Fatalf("build event: %v", err)
	}

	ctx := context.WithValue(context.Background(), driverIDContextKey{}, driverRec.Id)
	if err := handleCalibrationResult(ctx, app, qpuRec.Id, event); err != nil {
		t.Fatalf("handleCalibrationResult: %v", err)
	}

	var stored db.CalibrationRequest
	if err := db.FindOne(app, cfg.CollectionCalibrationRequests, request.ID, &stored); err != nil {
		t.Fatalf("reload request: %v", err)
	}
	if stored.Status != "done" {
		t.Errorf("expected the request to be done, got %q", stored.Status)
	}
}

// TestFetchNextCalibration_ReturnsTheOldestPending proves the queue is FIFO per
// driver.
func TestFetchNextCalibration_ReturnsTheOldestPending(t *testing.T) {
	app, _, driverRec, _ := seedDriverForEvents(t)

	for _, mode := range []string{"full", "fidelity_check"} {
		if err := saveToDb(app, &db.CalibrationRequest{
			Driver: driverRec.Id, Mode: mode, Status: "pending",
		}); err != nil {
			t.Fatalf("seed request: %v", err)
		}
	}

	got := scheduler.FetchNextCalibration(app, driverRec.Id)
	if got == nil {
		t.Fatal("expected a pending calibration")
	}
	if got.Mode != "full" {
		t.Errorf("expected the oldest pending request, got mode %q", got.Mode)
	}
}

// TestFetchNextCalibration_WaitsWhileOneIsRunning proves a second calibration is
// not offered while the first is in flight.
//
// The tuner's worker is single-threaded and a full DAG walk is hours, so a
// request dispatched behind another would produce a report nobody could connect
// to a request (RFC 0004 §6.8).
func TestFetchNextCalibration_WaitsWhileOneIsRunning(t *testing.T) {
	app, _, driverRec, _ := seedDriverForEvents(t)

	for _, status := range []string{"running", "pending"} {
		if err := saveToDb(app, &db.CalibrationRequest{
			Driver: driverRec.Id, Mode: "full", Status: status,
		}); err != nil {
			t.Fatalf("seed request: %v", err)
		}
	}

	if got := scheduler.FetchNextCalibration(app, driverRec.Id); got != nil {
		t.Errorf("expected nothing while a calibration is running, got %s", got.ID)
	}
}

// TestFetchNextCalibration_IgnoresOtherDrivers proves the queue is per driver.
func TestFetchNextCalibration_IgnoresOtherDrivers(t *testing.T) {
	app, _, driverRec, _ := seedDriverForEvents(t)

	if err := saveToDb(app, &db.CalibrationRequest{
		Driver: driverRec.Id, Mode: "full", Status: "pending",
	}); err != nil {
		t.Fatalf("seed request: %v", err)
	}

	if got := scheduler.FetchNextCalibration(app, "some-other-driver"); got != nil {
		t.Errorf("expected nothing for another driver, got %s", got.ID)
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

// TestToStringSlice_NarrowsWhateverTheQueueStored proves the stored JSON becomes
// the string list the dispatch payload declares, whichever form it comes back in.
func TestToStringSlice_NarrowsWhateverTheQueueStored(t *testing.T) {
	cases := []struct {
		name  string
		given any
		want  int
	}{
		{"already strings", []string{"q0", "q1"}, 2},
		{"decoded json", []any{"q0", "q1"}, 2},
		{"mixed junk", []any{"q0", 7}, 1},
		{"nil", nil, 0},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := toStringSlice(tc.given); len(got) != tc.want {
				t.Errorf("expected %d strings, got %v", tc.want, got)
			}
		})
	}
}
