package api

import (
	"context"
	"encoding/json"
	"strings"
	"testing"

	"qpi/internal/config"
	"qpi/internal/db"
	"qpi/internal/drivers"

	"github.com/pocketbase/dbx"
	"github.com/pocketbase/pocketbase/core"
	"github.com/pocketbase/pocketbase/tests"
)

// seedJobForResult builds a test app with a user, a QPU, and one running job
// owned by that user, ready for a JobResult to be applied to it.
func seedJobForResult(t *testing.T) (*tests.TestApp, *config.AppConfig, *core.Record, *core.Record) {
	t.Helper()

	app, err := tests.NewTestApp()
	if err != nil {
		t.Fatalf("failed to create test app: %v", err)
	}
	t.Cleanup(app.Cleanup)

	cfg := testConfig()
	config.SaveConfigOnApp(app, cfg)
	if err := db.EnsureSchema(app); err != nil {
		t.Fatalf("failed to ensure schema: %v", err)
	}

	userRec := core.NewRecord(getCollectionByName(t, app, "users"))
	userRec.Set("email", "runner@example.com")
	userRec.Set("password", "runnerpassword1234")
	userRec.Set("qpu_seconds", 1000.0)
	if err := app.Save(userRec); err != nil {
		t.Fatalf("failed to create user: %v", err)
	}

	qpuRec := core.NewRecord(getCollectionByName(t, app, cfg.CollectionQPUs))
	qpuRec.Set("name", "qpu_result")
	qpuRec.Set("access_token", db.HashToken("tok"))
	qpuRec.Set("status", "online")
	qpuRec.Set("num_qubits", 2)
	qpuRec.Set("enabled", true)
	if err := app.Save(qpuRec); err != nil {
		t.Fatalf("failed to create qpu: %v", err)
	}

	jobRec := core.NewRecord(getCollectionByName(t, app, cfg.CollectionQuantumJobs))
	jobRec.Set("user_id", userRec.Id)
	jobRec.Set("qpu_target", qpuRec.Id)
	jobRec.Set("status", "running")
	jobRec.Set("payload", map[string]any{"circuits": []any{}})
	if err := app.Save(jobRec); err != nil {
		t.Fatalf("failed to create job: %v", err)
	}

	return app, cfg, userRec, jobRec
}

// TestApplyJobResult_CompletesAndDeducts proves the JobResult handler persists a
// successful outcome and deducts the user's QPU-seconds, mirroring the legacy
// result listener (RFC 0001 §8).
func TestApplyJobResult_CompletesAndDeducts(t *testing.T) {
	app, cfg, userRec, jobRec := seedJobForResult(t)

	result := ResultPayload{
		JobID:   jobRec.Id,
		Results: map[string]any{"counts": map[string]any{"0x0": 1024}},
	}
	if err := applyJobResult(app, "qpu_result", result); err != nil {
		t.Fatalf("applyJobResult: %v", err)
	}

	updated, err := app.FindRecordById(cfg.CollectionQuantumJobs, jobRec.Id)
	if err != nil {
		t.Fatalf("find updated job: %v", err)
	}
	if updated.GetString("status") != "completed" {
		t.Errorf("job status = %q, want completed", updated.GetString("status"))
	}

	updatedUser, err := app.FindRecordById("users", userRec.Id)
	if err != nil {
		t.Fatalf("find updated user: %v", err)
	}
	if updatedUser.GetFloat("qpu_seconds") > 1000.0 {
		t.Errorf("expected qpu_seconds deducted from 1000, got %v", updatedUser.GetFloat("qpu_seconds"))
	}
}

// TestApplyJobResult_MarksFailedOnError proves an error payload lands the job as
// failed rather than completed.
func TestApplyJobResult_MarksFailedOnError(t *testing.T) {
	app, cfg, _, jobRec := seedJobForResult(t)

	result := ResultPayload{
		JobID:   jobRec.Id,
		Results: map[string]any{"error": "execution blew up"},
	}
	if err := applyJobResult(app, "qpu_result", result); err != nil {
		t.Fatalf("applyJobResult: %v", err)
	}

	updated, err := app.FindRecordById(cfg.CollectionQuantumJobs, jobRec.Id)
	if err != nil {
		t.Fatalf("find updated job: %v", err)
	}
	if updated.GetString("status") != "failed" {
		t.Errorf("job status = %q, want failed", updated.GetString("status"))
	}
}

// TestHandleDriverJobResult_ParsesEnvelope proves the registered handler unwraps
// a JobResult envelope's payload and applies it (RFC 0001 §4, §6).
func TestHandleDriverJobResult_ParsesEnvelope(t *testing.T) {
	app, cfg, _, jobRec := seedJobForResult(t)

	event, err := NewEvent("drv_1", EventJobResult, ResultPayload{
		JobID:   jobRec.Id,
		Results: map[string]any{"counts": map[string]any{"0x3": 1024}},
	})
	if err != nil {
		t.Fatalf("build result event: %v", err)
	}

	if err := handleDriverJobResult(nil, app, "qpu_result", event); err != nil {
		t.Fatalf("handleDriverJobResult: %v", err)
	}

	updated, err := app.FindRecordById(cfg.CollectionQuantumJobs, jobRec.Id)
	if err != nil {
		t.Fatalf("find updated job: %v", err)
	}
	if updated.GetString("status") != "completed" {
		t.Errorf("job status = %q, want completed", updated.GetString("status"))
	}
}

// seedDriverForEvents builds a test app with a QPU and a bluefors_gen1
// driver belonging to it, ready for a CryostatReading event to be attributed
// to (RFC 0001 §7, Phase 3).
func seedDriverForEvents(t *testing.T) (*tests.TestApp, *config.AppConfig, *core.Record, *core.Record) {
	t.Helper()

	app, err := tests.NewTestApp()
	if err != nil {
		t.Fatalf("failed to create test app: %v", err)
	}
	t.Cleanup(app.Cleanup)

	cfg := testConfig()
	config.SaveConfigOnApp(app, cfg)
	if err := db.EnsureSchema(app); err != nil {
		t.Fatalf("failed to ensure schema: %v", err)
	}

	qpuRec := core.NewRecord(getCollectionByName(t, app, cfg.CollectionQPUs))
	qpuRec.Set("name", "qpu_monitor")
	qpuRec.Set("access_token", db.HashToken("tok"))
	qpuRec.Set("status", "online")
	qpuRec.Set("enabled", true)
	if err := app.Save(qpuRec); err != nil {
		t.Fatalf("failed to create qpu: %v", err)
	}

	driverRec := core.NewRecord(getCollectionByName(t, app, cfg.CollectionDrivers))
	driverRec.Set("name", "cryostat-1")
	driverRec.Set("qpu", qpuRec.Id)
	driverRec.Set("kind", string(drivers.BlueforsGen1))
	driverRec.Set("language", string(drivers.Python))
	driverRec.Set("events", []string{string(EventCryostatReading)})
	driverRec.Set("token", db.HashToken("drv-tok"))
	driverRec.Set("status", "online")
	driverRec.Set("enabled", true)
	if err := app.Save(driverRec); err != nil {
		t.Fatalf("failed to create driver: %v", err)
	}

	return app, cfg, driverRec, qpuRec
}

func floatPtr(v float64) *float64 { return &v }

// TestHandleCryostatReading_PersistsToEventsLog proves a valid reading is
// appended to the events log, attributed to the driver that sent it and its
// QPU (RFC 0001 §7, Phase 3).
func TestHandleCryostatReading_PersistsToEventsLog(t *testing.T) {
	app, cfg, driverRec, qpuRec := seedDriverForEvents(t)

	event, err := NewEvent(driverRec.Id, EventCryostatReading, CryostatReadingPayload{
		Readings: map[string]ChannelReading{
			"mapper.bf.tmc": {Value: floatPtr(0.0123), Unit: "K", Status: "SYNCHRONIZED"},
		},
	})
	if err != nil {
		t.Fatalf("build event: %v", err)
	}

	ctx := context.WithValue(context.Background(), driverIDContextKey{}, driverRec.Id)
	if err := handleCryostatReading(ctx, app, qpuRec.Id, event); err != nil {
		t.Fatalf("handleCryostatReading: %v", err)
	}

	var rows []db.Event
	err = db.FindMany(app, cfg.CollectionEvents, &rows, "type = {:type}", "", 10, 0, dbx.Params{"type": string(EventCryostatReading)})
	if err != nil {
		t.Fatalf("find events: %v", err)
	}
	if len(rows) != 1 {
		t.Fatalf("expected 1 event row, got %d", len(rows))
	}
	if rows[0].Driver != driverRec.Id {
		t.Errorf("event driver = %q, want %q", rows[0].Driver, driverRec.Id)
	}
	if rows[0].QPU != qpuRec.Id {
		t.Errorf("event qpu = %q, want %q", rows[0].QPU, qpuRec.Id)
	}
	if rows[0].Source != driverRec.Id {
		t.Errorf("event source = %q, want %q", rows[0].Source, driverRec.Id)
	}
}

// TestHandleCryostatReading_RejectsEmptyReadings proves a payload with no
// readings is rejected — the registry logs and drops it rather than the
// listener crashing (RFC 0001 §4).
func TestHandleCryostatReading_RejectsEmptyReadings(t *testing.T) {
	app, _, driverRec, qpuRec := seedDriverForEvents(t)

	event, err := NewEvent(driverRec.Id, EventCryostatReading, CryostatReadingPayload{
		Readings: map[string]ChannelReading{},
	})
	if err != nil {
		t.Fatalf("build event: %v", err)
	}

	if err := handleCryostatReading(context.Background(), app, qpuRec.Id, event); err == nil {
		t.Errorf("expected empty readings to be rejected")
	}
}

// TestDriverEventRegistry_DispatchesCryostatReading proves the production
// registry routes CryostatReading to its handler and persists it, exercising
// the same path runDriverListener uses (RFC 0001 §4, §7).
func TestDriverEventRegistry_DispatchesCryostatReading(t *testing.T) {
	app, cfg, driverRec, qpuRec := seedDriverForEvents(t)

	event, err := NewEvent(driverRec.Id, EventCryostatReading, CryostatReadingPayload{
		Readings: map[string]ChannelReading{"mapper.bf.pmc": {Value: floatPtr(1.2e-6), Unit: "mbar"}},
	})
	if err != nil {
		t.Fatalf("build event: %v", err)
	}

	ctx := context.WithValue(context.Background(), driverIDContextKey{}, driverRec.Id)
	if err := driverEventRegistry.Dispatch(ctx, app, qpuRec.Id, event); err != nil {
		t.Fatalf("dispatch: %v", err)
	}

	var rows []db.Event
	err = db.FindMany(app, cfg.CollectionEvents, &rows, "type = {:type}", "", 10, 0, dbx.Params{"type": string(EventCryostatReading)})
	if err != nil {
		t.Fatalf("find events: %v", err)
	}
	if len(rows) != 1 {
		t.Fatalf("expected 1 event row, got %d", len(rows))
	}
}

func seedForLifecycle(t *testing.T) (*tests.TestApp, *config.AppConfig, *db.Driver) {
	t.Helper()

	app, err := tests.NewTestApp()
	if err != nil {
		t.Fatalf("failed to create test app: %v", err)
	}
	t.Cleanup(app.Cleanup)

	cfg := testConfig()
	config.SaveConfigOnApp(app, cfg)
	if err := db.EnsureSchema(app); err != nil {
		t.Fatalf("failed to ensure schema: %v", err)
	}

	qpu := db.QPU{Name: "qpu_1", Status: "online", Enabled: true}
	if err := saveToDb(app, &qpu); err != nil {
		t.Fatalf("failed to create qpu: %v", err)
	}

	driver := &db.Driver{
		Name: "tuner-1", QPU: qpu.ID,
		Kind: string(drivers.QuantifyTuner), Language: "python",
		Token: db.HashToken("tok"), Status: "online", Enabled: true,
		NNGInPort: 6111, NNGOutPort: 6112,
	}
	if err := saveToDb(app, driver); err != nil {
		t.Fatalf("failed to create driver: %v", err)
	}
	return app, cfg, driver
}

// leaseHeld registers driverID as served, as StartDriverDistribution would.
func leaseHeld(t *testing.T, driverID string) {
	t.Helper()
	activeDriversMu.Lock()
	activeDrivers[driverID] = func() {}
	activeDriversMu.Unlock()
	t.Cleanup(func() { StopDriverDistribution(driverID) })
}

func TestStopDriverDistribution_ReleasesTheLease(t *testing.T) {
	_, _, driver := seedForLifecycle(t)
	leaseHeld(t, driver.ID)

	if !isDispatching(driver.ID) {
		t.Fatal("expected the lease to be held before release")
	}

	StopDriverDistribution(driver.ID)

	if isDispatching(driver.ID) {
		t.Error("expected the lease to be released")
	}
}

// A released lease keeps its ports, which is why no grace period is needed: the
// record still claims them and findFreePorts will not hand them to anyone else.
func TestStopDriverDistribution_LeavesThePortsOnTheRecord(t *testing.T) {
	app, cfg, driver := seedForLifecycle(t)
	leaseHeld(t, driver.ID)

	StopDriverDistribution(driver.ID)

	var reloaded db.Driver
	if err := db.FindOne(app, cfg.CollectionDrivers, driver.ID, &reloaded); err != nil {
		t.Fatalf("failed to reload driver: %v", err)
	}
	if reloaded.NNGInPort != 6111 || reloaded.NNGOutPort != 6112 {
		t.Errorf("ports moved on release: got %d/%d, want 6111/6112",
			reloaded.NNGInPort, reloaded.NNGOutPort)
	}

	ports, err := findFreePorts(app, 2)
	if err != nil {
		t.Fatalf("findFreePorts: %v", err)
	}
	for _, port := range ports {
		if port == 6111 || port == 6112 {
			t.Errorf("port %d was reallocated while its record still claims it", port)
		}
	}
}

func TestMarkEveryDriverOffline_ClearsAStaleOnlineClaim(t *testing.T) {
	app, cfg, driver := seedForLifecycle(t)

	if err := MarkEveryDriverOffline(app); err != nil {
		t.Fatalf("MarkEveryDriverOffline: %v", err)
	}

	var reloaded db.Driver
	if err := db.FindOne(app, cfg.CollectionDrivers, driver.ID, &reloaded); err != nil {
		t.Fatalf("failed to reload driver: %v", err)
	}
	if reloaded.Status != "offline" {
		t.Errorf("expected offline after startup reconciliation, got %q", reloaded.Status)
	}
}

// Startup reconciliation is what stops a row left `online` by a crashed server
// from blocking the QPU's next driver.
func TestMarkEveryDriverOffline_UnblocksTheOnePerRoleCheck(t *testing.T) {
	app, cfg, stale := seedForLifecycle(t)
	leaseHeld(t, stale.ID)

	second := &db.Driver{
		Name: "tuner-2", QPU: stale.QPU,
		Kind: string(drivers.QuantifyTuner), Language: "python",
		Token: db.HashToken("tok2"), Status: "offline", Enabled: true,
	}
	if err := saveToDb(app, second); err != nil {
		t.Fatalf("failed to create the second driver: %v", err)
	}

	if peer, _ := connectedPeer(app, cfg, second); peer == "" {
		t.Fatal("expected the stale online peer to block while its lease is held")
	}

	if err := MarkEveryDriverOffline(app); err != nil {
		t.Fatalf("MarkEveryDriverOffline: %v", err)
	}

	if peer, _ := connectedPeer(app, cfg, second); peer != "" {
		t.Errorf("expected no block after reconciliation, got %q", peer)
	}
}

func TestMarkEveryDriverOffline_LeavesAnAlreadyOfflineDriverAlone(t *testing.T) {
	app, cfg, driver := seedForLifecycle(t)
	driver.Status = "offline"
	if err := saveToDb(app, driver); err != nil {
		t.Fatalf("failed to save driver: %v", err)
	}

	if err := MarkEveryDriverOffline(app); err != nil {
		t.Fatalf("MarkEveryDriverOffline: %v", err)
	}

	var reloaded db.Driver
	if err := db.FindOne(app, cfg.CollectionDrivers, driver.ID, &reloaded); err != nil {
		t.Fatalf("failed to reload driver: %v", err)
	}
	if reloaded.Status != "offline" {
		t.Errorf("expected offline, got %q", reloaded.Status)
	}
}

// A driver record that goes away must take its lease with it, or its goroutines
// and its listener outlive every trace of it.
func TestDriverDelete_ReleasesTheLease(t *testing.T) {
	app, cfg, driver := seedForLifecycle(t)
	leaseHeld(t, driver.ID)

	record, err := app.FindRecordById(cfg.CollectionDrivers, driver.ID)
	if err != nil {
		t.Fatalf("failed to find driver record: %v", err)
	}
	if err := app.Delete(record); err != nil {
		t.Fatalf("failed to delete driver: %v", err)
	}
	ReleaseLeaseIfDriver(app, record)

	if isDispatching(driver.ID) {
		t.Error("expected a deleted driver's lease to be released")
	}
}

func TestReleaseLeaseIfDriver_IgnoresOtherCollections(t *testing.T) {
	app, cfg, driver := seedForLifecycle(t)
	leaseHeld(t, driver.ID)

	qpu, err := app.FindFirstRecordByFilter(cfg.CollectionQPUs, "name = 'qpu_1'")
	if err != nil {
		t.Fatalf("failed to find qpu: %v", err)
	}
	ReleaseLeaseIfDriver(app, qpu)

	if !isDispatching(driver.ID) {
		t.Error("deleting a QPU must not release a driver's lease")
	}
}

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

// TestHandleCalibrationProgress_WritesOntoTheRequest proves a walk's position
// reaches the row the dashboard is subscribed to, without touching its status: a
// calibration reporting progress is still running (RFC 0004 §6.8).
func TestHandleCalibrationProgress_WritesOntoTheRequest(t *testing.T) {
	app, cfg, driverRec, qpuRec := seedDriverForEvents(t)

	request := &db.CalibrationRequest{
		Driver: driverRec.Id, Mode: "full", Status: "running",
	}
	if err := saveToDb(app, request); err != nil {
		t.Fatalf("seed request: %v", err)
	}

	event, err := NewEvent(driverRec.Id, EventCalibrationProgress, CalibrationProgressPayload{
		JobID: request.ID, Mode: "full", Step: 7, Total: 33,
		Routine: "rabi", Target: "q2", Succeeded: 12, Failed: 1, ElapsedS: 812.4,
	})
	if err != nil {
		t.Fatalf("build event: %v", err)
	}

	ctx := context.WithValue(context.Background(), driverIDContextKey{}, driverRec.Id)
	if err := handleCalibrationProgress(ctx, app, qpuRec.Id, event); err != nil {
		t.Fatalf("handleCalibrationProgress: %v", err)
	}

	record, err := app.FindRecordById(cfg.CollectionCalibrationRequests, request.ID)
	if err != nil {
		t.Fatalf("reload request: %v", err)
	}
	if status := record.GetString("status"); status != "running" {
		t.Errorf("expected the request to still be running, got %q", status)
	}

	var stored map[string]any
	if err := json.Unmarshal([]byte(record.GetString("progress")), &stored); err != nil {
		t.Fatalf("progress is not stored as json: %v", err)
	}
	for field, want := range map[string]any{
		"step": 7.0, "total": 33.0, "routine": "rabi", "target": "q2",
		"succeeded": 12.0, "failed": 1.0,
	} {
		if stored[field] != want {
			t.Errorf("progress[%q] = %v, want %v", field, stored[field], want)
		}
	}
	if _, ok := stored["job_id"]; ok {
		t.Error("job_id names the record this was written to; storing it again is noise")
	}
}

// TestACalibrationRequestCarriesItsRequester proves the two fields the dispatch
// endpoint sets are ones the collection accepts.
//
// Both fail at the insert rather than at compile time — `trigger` is a select and
// `requested_by` is a relation whose target PocketBase checks — so a wrong value in
// either would refuse every dispatched calibration rather than lose its attribution.
func TestACalibrationRequestCarriesItsRequester(t *testing.T) {
	app, cfg, driverRec, qpuRec := seedDriverForEvents(t)

	// Written the way getCurrentUser writes it: `users` is an auth collection, and a
	// proxy record for an admin who signs in as a superuser has no password to set.
	usersCol, err := app.FindCollectionByNameOrId("users")
	if err != nil {
		t.Fatalf("users collection: %v", err)
	}
	admin := core.NewRecord(usersCol)
	admin.Set("email", "admin@example.com")
	admin.Set("username", "admin_proxy")
	if err := app.SaveNoValidate(admin); err != nil {
		t.Fatalf("seed the admin's proxy user: %v", err)
	}

	request := &db.CalibrationRequest{
		Driver: driverRec.Id, QPU: qpuRec.Id, Mode: "full", Status: "pending",
		RequestedBy: admin.Id, Trigger: "dispatched",
	}
	if err := saveToDb(app, request); err != nil {
		t.Fatalf("a dispatched calibration must save with its requester: %v", err)
	}

	record, err := app.FindRecordById(cfg.CollectionCalibrationRequests, request.ID)
	if err != nil {
		t.Fatalf("reload request: %v", err)
	}
	if got := record.GetString("requested_by"); got != admin.Id {
		t.Errorf("requested_by = %q, want %q", got, admin.Id)
	}
	if got := record.GetString("trigger"); got != "dispatched" {
		t.Errorf("trigger = %q, want dispatched", got)
	}
}

// TestACalibrationRequestRefusesARequesterThatIsNotAUser is why the endpoint resolves
// a superuser to its proxy `users` record instead of storing its own id: the caller
// authenticates against `_superusers`, and an id from there is not one this relation
// can hold.
func TestACalibrationRequestRefusesARequesterThatIsNotAUser(t *testing.T) {
	app, _, driverRec, qpuRec := seedDriverForEvents(t)

	request := &db.CalibrationRequest{
		Driver: driverRec.Id, QPU: qpuRec.Id, Mode: "full", Status: "pending",
		RequestedBy: "notarealuserid1", Trigger: "dispatched",
	}
	if err := saveToDb(app, request); err == nil {
		t.Error("expected a requester outside `users` to be refused")
	}
}

// TestHandleCalibrationQueued_CreatesTheRowNobodyDispatched proves a drift check
// gets a request row of its own, running and attributed to drift (RFC 0004 §6.5).
func TestHandleCalibrationQueued_CreatesTheRowNobodyDispatched(t *testing.T) {
	app, cfg, driverRec, qpuRec := seedDriverForEvents(t)

	event, err := NewEvent(driverRec.Id, EventCalibrationQueued, CalibrationQueuedPayload{
		JobID: "drift_check", Mode: "fidelity_check", Reason: "the drift timer",
	})
	if err != nil {
		t.Fatalf("build event: %v", err)
	}

	ctx := context.WithValue(context.Background(), driverIDContextKey{}, driverRec.Id)
	if err := handleCalibrationQueued(ctx, app, qpuRec.Id, event); err != nil {
		t.Fatalf("handleCalibrationQueued: %v", err)
	}

	record := findCalibrationRequest(app, cfg, "drift_check")
	if record == nil {
		t.Fatal("expected a row for the drift check the driver queued itself")
	}
	// Running, not pending: the driver has already started it. Pending would have
	// the dispatcher offer it a second time.
	if status := record.GetString("status"); status != "running" {
		t.Errorf("expected running, got %q", status)
	}
	if trigger := record.GetString("trigger"); trigger != "drift" {
		t.Errorf("expected trigger drift, got %q — nobody should hunt for who started it", trigger)
	}
	if record.GetString("driver") != driverRec.Id || record.GetString("qpu") != qpuRec.Id {
		t.Errorf("expected the row attributed to the driver and its QPU, got %q/%q",
			record.GetString("driver"), record.GetString("qpu"))
	}
	// `drift_check` is not a legal record id — fifteen lowercase alphanumerics — so
	// it has to live in a field of its own for progress to resolve against.
	if record.Id == "drift_check" {
		t.Error("expected the job id in job_id, not as the record id")
	}
}

// TestHandleCalibrationQueued_IsIdempotent proves a re-announced calibration does
// not leave two rows for one run, which a driver reconnecting mid-run would.
func TestHandleCalibrationQueued_IsIdempotent(t *testing.T) {
	app, cfg, driverRec, qpuRec := seedDriverForEvents(t)

	event, err := NewEvent(driverRec.Id, EventCalibrationQueued, CalibrationQueuedPayload{
		JobID: "drift_check", Mode: "fidelity_check",
	})
	if err != nil {
		t.Fatalf("build event: %v", err)
	}

	ctx := context.WithValue(context.Background(), driverIDContextKey{}, driverRec.Id)
	for range 2 {
		if err := handleCalibrationQueued(ctx, app, qpuRec.Id, event); err != nil {
			t.Fatalf("handleCalibrationQueued: %v", err)
		}
	}

	rows, err := app.FindRecordsByFilter(cfg.CollectionCalibrationRequests,
		"job_id = 'drift_check'", "+created", 0, 0)
	if err != nil {
		t.Fatalf("find rows: %v", err)
	}
	if len(rows) != 1 {
		t.Errorf("expected 1 row for one calibration, got %d", len(rows))
	}
}

// TestHandleCalibrationProgress_FindsASelfTriggeredRun proves progress reaches a row
// whose job id is not its record id, which is every calibration a driver starts itself.
func TestHandleCalibrationProgress_FindsASelfTriggeredRun(t *testing.T) {
	app, cfg, driverRec, qpuRec := seedDriverForEvents(t)

	request := &db.CalibrationRequest{
		Driver: driverRec.Id, Mode: "partial", Status: "running",
		JobID: "drift_check_recalibrate", Trigger: "drift",
	}
	if err := saveToDb(app, request); err != nil {
		t.Fatalf("seed request: %v", err)
	}

	event, err := NewEvent(driverRec.Id, EventCalibrationProgress, CalibrationProgressPayload{
		JobID: "drift_check_recalibrate", Mode: "partial", Step: 3, Total: 29,
		Routine: "rabi", Target: "q0",
	})
	if err != nil {
		t.Fatalf("build event: %v", err)
	}

	ctx := context.WithValue(context.Background(), driverIDContextKey{}, driverRec.Id)
	if err := handleCalibrationProgress(ctx, app, qpuRec.Id, event); err != nil {
		t.Fatalf("handleCalibrationProgress: %v", err)
	}

	record, err := app.FindRecordById(cfg.CollectionCalibrationRequests, request.ID)
	if err != nil {
		t.Fatalf("reload request: %v", err)
	}
	var stored map[string]any
	if err := json.Unmarshal([]byte(record.GetString("progress")), &stored); err != nil {
		t.Fatalf("progress is not stored as json: %v", err)
	}
	if stored["step"] != 3.0 || stored["routine"] != "rabi" {
		t.Errorf("expected the update to reach the row, got %v", stored)
	}
}

// TestHandleCalibrationProgress_IgnoresAnUnknownJob proves a driver's own drift
// check, which answers to no queued row, is dropped rather than reported as an error.
func TestHandleCalibrationProgress_IgnoresAnUnknownJob(t *testing.T) {
	app, _, driverRec, qpuRec := seedDriverForEvents(t)

	event, err := NewEvent(driverRec.Id, EventCalibrationProgress, CalibrationProgressPayload{
		JobID: "drift_check", Mode: "fidelity_check", Step: 1, Total: 2, Routine: "rb",
	})
	if err != nil {
		t.Fatalf("build event: %v", err)
	}

	ctx := context.WithValue(context.Background(), driverIDContextKey{}, driverRec.Id)
	if err := handleCalibrationProgress(ctx, app, qpuRec.Id, event); err != nil {
		t.Errorf("a drift check has no request to update; that is not an error: %v", err)
	}
}

// TestHandleCalibrationProgress_RejectsAPayloadWithNoTotal proves a progress
// update that cannot be rendered as a position is refused.
func TestHandleCalibrationProgress_RejectsAPayloadWithNoTotal(t *testing.T) {
	app, _, driverRec, qpuRec := seedDriverForEvents(t)

	event, err := NewEvent(driverRec.Id, EventCalibrationProgress, map[string]any{
		"job_id": "cal-1", "routine": "rabi",
	})
	if err != nil {
		t.Fatalf("build event: %v", err)
	}

	ctx := context.WithValue(context.Background(), driverIDContextKey{}, driverRec.Id)
	if err := handleCalibrationProgress(ctx, app, qpuRec.Id, event); err == nil {
		t.Error("expected a payload with no total to be refused")
	}
}

// aPlan is a plan over two routines, the second walked over three targets.
func aPlan() *CalibrationPlan {
	return &CalibrationPlan{Nodes: []CalibrationPlanNode{
		{Name: "resonator_spectroscopy", Targets: []string{"q0"}, Planned: true, HasCheck: true},
		{Name: "rabi", DependsOn: []string{"resonator_spectroscopy"},
			Targets: []string{"q0", "q1", "q2"}, Kind: "qubits", Planned: true,
			Updates: []string{"rxy.amp180"}},
	}}
}

// TestHandleCalibrationQueued_AttachesAPlanToARowItDidNotCreate proves the second
// announcement lands its plan on the row the first one made, rather than being
// dropped as a duplicate (RFC 0006 §5.1).
func TestHandleCalibrationQueued_AttachesAPlanToARowItDidNotCreate(t *testing.T) {
	app, cfg, driverRec, qpuRec := seedDriverForEvents(t)

	request := &db.CalibrationRequest{
		Driver: driverRec.Id, Mode: "full", Status: "running",
		JobID: "drift_check_recalibrate", Trigger: "drift",
	}
	if err := saveToDb(app, request); err != nil {
		t.Fatalf("seed request: %v", err)
	}

	event, err := NewEvent(driverRec.Id, EventCalibrationQueued, CalibrationQueuedPayload{
		JobID: "drift_check_recalibrate", Mode: "full",
		Reason: "the walk it is about to make", Plan: aPlan(),
	})
	if err != nil {
		t.Fatalf("build event: %v", err)
	}

	ctx := context.WithValue(context.Background(), driverIDContextKey{}, driverRec.Id)
	if err := handleCalibrationQueued(ctx, app, qpuRec.Id, event); err != nil {
		t.Fatalf("handleCalibrationQueued: %v", err)
	}

	record, err := app.FindRecordById(cfg.CollectionCalibrationRequests, request.ID)
	if err != nil {
		t.Fatalf("reload request: %v", err)
	}
	var stored CalibrationPlan
	if err := json.Unmarshal([]byte(record.GetString("plan")), &stored); err != nil {
		t.Fatalf("the plan is not stored as json: %v", err)
	}
	if len(stored.Nodes) != 2 || stored.Nodes[1].Name != "rabi" {
		t.Fatalf("expected the plan's two nodes in walk order, got %+v", stored.Nodes)
	}
	if got := stored.Nodes[1].Updates; len(got) != 1 || got[0] != "rxy.amp180" {
		t.Errorf("expected the node's device paths to survive the round trip, got %v", got)
	}
	// One run, one row: the second announcement carries a plan, not a duplicate.
	rows, err := app.FindRecordsByFilter(cfg.CollectionCalibrationRequests,
		"job_id = 'drift_check_recalibrate'", "+created", 0, 0)
	if err != nil {
		t.Fatalf("find rows: %v", err)
	}
	if len(rows) != 1 {
		t.Errorf("expected 1 row for one calibration, got %d", len(rows))
	}
}

// TestHandleCalibrationQueued_CreatesTheRowWithItsPlan proves a plan whose
// announcement was lost still gets a row, since it is the row's own second
// announcement that carries it.
func TestHandleCalibrationQueued_CreatesTheRowWithItsPlan(t *testing.T) {
	app, cfg, driverRec, qpuRec := seedDriverForEvents(t)

	event, err := NewEvent(driverRec.Id, EventCalibrationQueued, CalibrationQueuedPayload{
		JobID: "drift_check_recalibrate", Mode: "partial",
		TargetQubits: []string{"q0"}, Plan: aPlan(),
	})
	if err != nil {
		t.Fatalf("build event: %v", err)
	}

	ctx := context.WithValue(context.Background(), driverIDContextKey{}, driverRec.Id)
	if err := handleCalibrationQueued(ctx, app, qpuRec.Id, event); err != nil {
		t.Fatalf("handleCalibrationQueued: %v", err)
	}

	record := findCalibrationRequest(app, cfg, "drift_check_recalibrate")
	if record == nil {
		t.Fatal("expected a row for the calibration the plan describes")
	}
	var stored CalibrationPlan
	if err := json.Unmarshal([]byte(record.GetString("plan")), &stored); err != nil {
		t.Fatalf("the plan is not stored as json: %v", err)
	}
	if len(stored.Nodes) != 2 {
		t.Errorf("expected the plan on the created row, got %+v", stored.Nodes)
	}
}

// TestHandleCalibrationQueued_LeavesAStoredPlanAloneWhenNoneIsSent proves the
// announcement made at queue time cannot wipe the plan a later one landed — the two
// arrive in whatever order the sockets deliver them.
func TestHandleCalibrationQueued_LeavesAStoredPlanAloneWhenNoneIsSent(t *testing.T) {
	app, cfg, driverRec, qpuRec := seedDriverForEvents(t)
	ctx := context.WithValue(context.Background(), driverIDContextKey{}, driverRec.Id)

	withPlan, err := NewEvent(driverRec.Id, EventCalibrationQueued, CalibrationQueuedPayload{
		JobID: "drift_check_recalibrate", Mode: "full", Plan: aPlan(),
	})
	if err != nil {
		t.Fatalf("build event: %v", err)
	}
	withoutPlan, err := NewEvent(driverRec.Id, EventCalibrationQueued, CalibrationQueuedPayload{
		JobID: "drift_check_recalibrate", Mode: "full", Reason: "the drift timer",
	})
	if err != nil {
		t.Fatalf("build event: %v", err)
	}

	for _, event := range []*Event{withPlan, withoutPlan} {
		if err := handleCalibrationQueued(ctx, app, qpuRec.Id, event); err != nil {
			t.Fatalf("handleCalibrationQueued: %v", err)
		}
	}

	record := findCalibrationRequest(app, cfg, "drift_check_recalibrate")
	var stored CalibrationPlan
	if err := json.Unmarshal([]byte(record.GetString("plan")), &stored); err != nil {
		t.Fatalf("the plan is not stored as json: %v", err)
	}
	if len(stored.Nodes) != 2 {
		t.Errorf("expected the stored plan to survive an announcement without one, got %+v", stored.Nodes)
	}
}

// TestAdvanceNodes_AccumulatesAcrossEvents proves per-node state builds up over a
// walk rather than being replaced by the latest position (RFC 0006 §5.3).
//
// The events are the ones a walk actually emits: `succeeded` and `failed` are the
// walk's running totals, not the outcome of the target the event names, so whether
// that target failed is only readable as a difference from the event before.
func TestAdvanceNodes_AccumulatesAcrossEvents(t *testing.T) {
	plan := aPlan()
	walk := []CalibrationProgressPayload{
		{Step: 1, Total: 2, Routine: "resonator_spectroscopy", Target: "q0", Succeeded: 1},
		{Step: 2, Total: 2, Routine: "rabi", Target: "q0", Succeeded: 2},
		{Step: 2, Total: 2, Routine: "rabi", Target: "q1", Succeeded: 2, Failed: 1},
		{Step: 2, Total: 2, Routine: "rabi", Target: "q2", Succeeded: 3, Failed: 1},
	}

	prior := map[string]any{}
	var nodes map[string]CalibrationNodeState
	for i := range walk {
		nodes = advanceNodes(prior, &walk[i], plan)
		// Round-tripped through JSON, as the stored row does it: the reducer reads
		// its own previous output back out of a json field, not out of memory.
		stored := walk[i].ToMap()
		stored["nodes"] = nodes
		encoded, err := json.Marshal(stored)
		if err != nil {
			t.Fatalf("marshal progress: %v", err)
		}
		prior = map[string]any{}
		if err := json.Unmarshal(encoded, &prior); err != nil {
			t.Fatalf("unmarshal progress: %v", err)
		}
	}

	// Its one target passed, and the walk moved on: done.
	if got := nodes["resonator_spectroscopy"]; got.State != "done" || got.Done != 1 {
		t.Errorf("resonator_spectroscopy = %+v, want done 1/1", got)
	}
	// Three targets, one of them failed: partial, and the tally the drawing shows.
	rabi := nodes["rabi"]
	if rabi.State != "partial" || rabi.Done != 3 || rabi.Total != 3 || rabi.Failed != 1 {
		t.Errorf("rabi = %+v, want partial with 3/3 and 1 failed", rabi)
	}
}

// TestAdvanceNodes_MarksTheNamedNodeRunningUntilItsTargetsAreDone proves a node with
// targets left is `running`, since a progress event fires after a target finishes and
// the routine it names is still the one being walked.
func TestAdvanceNodes_MarksTheNamedNodeRunningUntilItsTargetsAreDone(t *testing.T) {
	event := CalibrationProgressPayload{Step: 2, Total: 2, Routine: "rabi", Target: "q0", Succeeded: 1}
	nodes := advanceNodes(nil, &event, aPlan())

	if got := nodes["rabi"]; got.State != "running" || got.Done != 1 || got.Total != 3 {
		t.Errorf("rabi = %+v, want running 1/3", got)
	}
}

// TestAdvanceNodes_EveryTargetFailingIsFailedNotPartial keeps the two apart: a node
// that produced nothing usable is not a node that produced some of it.
func TestAdvanceNodes_EveryTargetFailingIsFailedNotPartial(t *testing.T) {
	prior := map[string]any{}
	var nodes map[string]CalibrationNodeState
	for i, target := range []string{"q0", "q1", "q2"} {
		event := CalibrationProgressPayload{
			Step: 2, Total: 2, Routine: "rabi", Target: target, Failed: i + 1,
		}
		nodes = advanceNodes(prior, &event, aPlan())
		prior = map[string]any{"routine": "rabi", "failed": float64(i + 1), "nodes": jsonRoundTrip(t, nodes)}
	}

	if got := nodes["rabi"]; got.State != "failed" || got.Failed != 3 {
		t.Errorf("rabi = %+v, want failed with 3 failures", got)
	}
}

// TestAdvanceNodes_WithoutAPlanTheWalkMovingOnSettlesTheNode proves a row that never
// received a plan still gets usable state: the tally cannot say a node is finished
// with no total to compare against, but the walk naming a different routine can.
func TestAdvanceNodes_WithoutAPlanTheWalkMovingOnSettlesTheNode(t *testing.T) {
	first := CalibrationProgressPayload{Step: 1, Total: 2, Routine: "t1", Target: "q0", Succeeded: 1}
	nodes := advanceNodes(nil, &first, &CalibrationPlan{})
	if got := nodes["t1"].State; got != "running" {
		t.Errorf("t1 = %q, want running while nothing says how many targets it has", got)
	}

	second := CalibrationProgressPayload{Step: 2, Total: 2, Routine: "t2_echo", Target: "q0", Succeeded: 2}
	nodes = advanceNodes(
		map[string]any{"routine": "t1", "succeeded": 1.0, "nodes": jsonRoundTrip(t, nodes)},
		&second, &CalibrationPlan{},
	)
	if got := nodes["t1"].State; got != "done" {
		t.Errorf("t1 = %q, want done once the walk moved on", got)
	}
}

// jsonRoundTrip is how the node map reaches the reducer in production: out of a json
// field, so every number is a float64 and every state a bare string.
func jsonRoundTrip(t *testing.T, nodes map[string]CalibrationNodeState) map[string]any {
	t.Helper()
	encoded, err := json.Marshal(nodes)
	if err != nil {
		t.Fatalf("marshal nodes: %v", err)
	}
	var out map[string]any
	if err := json.Unmarshal(encoded, &out); err != nil {
		t.Fatalf("unmarshal nodes: %v", err)
	}
	return out
}

// TestHandleCalibrationProgress_LandsTheNodeMapOnTheRow proves the accumulation
// reaches the row the dashboard subscribes to, sized from the stored plan.
func TestHandleCalibrationProgress_LandsTheNodeMapOnTheRow(t *testing.T) {
	app, cfg, driverRec, qpuRec := seedDriverForEvents(t)

	request := &db.CalibrationRequest{
		Driver: driverRec.Id, Mode: "full", Status: "running", Plan: aPlan(),
	}
	if err := saveToDb(app, request); err != nil {
		t.Fatalf("seed request: %v", err)
	}

	event, err := NewEvent(driverRec.Id, EventCalibrationProgress, CalibrationProgressPayload{
		JobID: request.ID, Mode: "full", Step: 2, Total: 2,
		Routine: "rabi", Target: "q0", Succeeded: 1,
	})
	if err != nil {
		t.Fatalf("build event: %v", err)
	}

	ctx := context.WithValue(context.Background(), driverIDContextKey{}, driverRec.Id)
	if err := handleCalibrationProgress(ctx, app, qpuRec.Id, event); err != nil {
		t.Fatalf("handleCalibrationProgress: %v", err)
	}

	record, err := app.FindRecordById(cfg.CollectionCalibrationRequests, request.ID)
	if err != nil {
		t.Fatalf("reload request: %v", err)
	}
	var stored struct {
		Nodes map[string]CalibrationNodeState `json:"nodes"`
	}
	if err := json.Unmarshal([]byte(record.GetString("progress")), &stored); err != nil {
		t.Fatalf("progress is not stored as json: %v", err)
	}
	if got := stored.Nodes["rabi"]; got.State != "running" || got.Total != 3 {
		t.Errorf("nodes[rabi] = %+v, want running out of the plan's 3 targets", got)
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

// TestAdvanceNodes_MarksASingleTargetNodeRunningBeforeItFinishes is the regression
// test for RFC 0009 §7.1. A finish event alone puts a one-target node straight to
// `done`, so before the driver reported a start there was no walk in which the
// `running` style the graph draws could ever be reached.
func TestAdvanceNodes_MarksASingleTargetNodeRunningBeforeItFinishes(t *testing.T) {
	start := CalibrationProgressPayload{
		Step: 1, Total: 2, Routine: "resonator_spectroscopy", Running: []string{"q0"},
	}
	nodes := advanceNodes(nil, &start, aPlan())

	node := nodes["resonator_spectroscopy"]
	if node.State != "running" || node.Done != 0 {
		t.Fatalf("after start = %+v, want running with nothing done", node)
	}
	if len(node.Running) != 1 || node.Running[0] != "q0" {
		t.Errorf("running = %v, want [q0]", node.Running)
	}

	finish := CalibrationProgressPayload{
		Step: 1, Total: 2, Routine: "resonator_spectroscopy", Target: "q0", Succeeded: 1,
	}
	nodes = advanceNodes(storedAfter(t, &start, nodes), &finish, aPlan())

	if node := nodes["resonator_spectroscopy"]; node.State != "done" || node.Done != 1 {
		t.Errorf("after finish = %+v, want done 1/1", node)
	}
	if node := nodes["resonator_spectroscopy"]; len(node.Running) != 0 {
		t.Errorf("running = %v, want empty once the target is done", node.Running)
	}
}

// TestAdvanceNodes_ShrinksTheInFlightSetAsAGroupReportsBack covers a fused group,
// whose start names every target at once and whose finishes arrive one at a time
// (RFC 0009 §7.2).
func TestAdvanceNodes_ShrinksTheInFlightSetAsAGroupReportsBack(t *testing.T) {
	start := CalibrationProgressPayload{
		Step: 2, Total: 2, Routine: "rabi", Running: []string{"q0", "q1", "q2"},
	}
	nodes := advanceNodes(nil, &start, aPlan())
	if got := nodes["rabi"]; len(got.Running) != 3 || got.Total != 3 {
		t.Fatalf("after start = %+v, want 3 in flight out of 3", got)
	}

	prior := storedAfter(t, &start, nodes)
	finish := CalibrationProgressPayload{
		Step: 2, Total: 2, Routine: "rabi", Target: "q1", Succeeded: 1,
	}
	nodes = advanceNodes(prior, &finish, aPlan())

	got := nodes["rabi"]
	if got.State != "running" || got.Done != 1 {
		t.Fatalf("after one finish = %+v, want running 1/3", got)
	}
	if len(got.Running) != 2 || got.Running[0] != "q0" || got.Running[1] != "q2" {
		t.Errorf("running = %v, want the two targets still in flight", got.Running)
	}
}

// TestAdvanceNodes_SettlesANodeWhoseTargetsWereAllSkipped proves a node that never
// ran stops at `blocked` rather than sitting at `pending` for the rest of the walk.
// Skipped is neither done nor failed (RFC 0007 §11), and before RFC 0009 a blocked
// target reported nothing at all.
func TestAdvanceNodes_SettlesANodeWhoseTargetsWereAllSkipped(t *testing.T) {
	nodes := map[string]CalibrationNodeState{}
	prior := map[string]any{}
	for i, target := range []string{"q0", "q1", "q2"} {
		event := CalibrationProgressPayload{
			Step: 2, Total: 2, Routine: "rabi", Target: target, Skipped: i + 1,
		}
		nodes = advanceNodes(prior, &event, aPlan())
		prior = storedAfter(t, &event, nodes)
	}

	if got := nodes["rabi"]; got.State != "blocked" || got.Skipped != 3 {
		t.Errorf("rabi = %+v, want blocked with 3 skipped", got)
	}
}

// TestAdvanceNodes_PrefersFailureToASkip: a node with one of each has something to
// investigate, and reporting it as merely blocked would bury that.
func TestAdvanceNodes_PrefersFailureToASkip(t *testing.T) {
	nodes := map[string]CalibrationNodeState{}
	prior := map[string]any{}
	walk := []CalibrationProgressPayload{
		{Step: 2, Total: 2, Routine: "rabi", Target: "q0", Skipped: 1},
		{Step: 2, Total: 2, Routine: "rabi", Target: "q1", Skipped: 1, Failed: 1},
		{Step: 2, Total: 2, Routine: "rabi", Target: "q2", Skipped: 1, Failed: 1, Succeeded: 1},
	}
	for i := range walk {
		nodes = advanceNodes(prior, &walk[i], aPlan())
		prior = storedAfter(t, &walk[i], nodes)
	}

	if got := nodes["rabi"]; got.State != "partial" || got.Failed != 1 || got.Skipped != 1 {
		t.Errorf("rabi = %+v, want partial with one failure and one skip", got)
	}
}

// TestAdvanceNodes_IgnoresAStartFromAnOlderDriver: no `running` key means the payload
// came from a driver predating RFC 0009, which must reduce exactly as it used to.
func TestAdvanceNodes_IgnoresAStartFromAnOlderDriver(t *testing.T) {
	event := CalibrationProgressPayload{
		Step: 2, Total: 2, Routine: "rabi", Target: "q0", Succeeded: 1,
	}
	nodes := advanceNodes(nil, &event, aPlan())

	if got := nodes["rabi"]; got.State != "running" || got.Done != 1 || got.Total != 3 {
		t.Errorf("rabi = %+v, want running 1/3 as before", got)
	}
}

// storedAfter is the row's `progress` field as the handler writes it, round-tripped
// through JSON — the reducer reads its own previous output back out of a json field,
// not out of memory.
func storedAfter(
	t *testing.T, event *CalibrationProgressPayload, nodes map[string]CalibrationNodeState,
) map[string]any {
	t.Helper()
	stored := event.ToMap()
	stored["nodes"] = nodes
	encoded, err := json.Marshal(stored)
	if err != nil {
		t.Fatalf("marshal progress: %v", err)
	}
	prior := map[string]any{}
	if err := json.Unmarshal(encoded, &prior); err != nil {
		t.Fatalf("unmarshal progress: %v", err)
	}
	return prior
}
