package scheduler

import (
	"encoding/json"
	"testing"
	"time"

	"qpi/internal/config"
	"qpi/internal/db"

	"github.com/pocketbase/pocketbase/core"
	"github.com/pocketbase/pocketbase/tests"
)

// retentionConfig builds an AppConfig with the driver framework on and a short
// retention window, mirroring the collection names every other test uses.
func retentionConfig(retention time.Duration) *config.AppConfig {
	return &config.AppConfig{
		CollectionQPUs:                config.DefaultQpusCollection,
		CollectionTimeSlots:           config.DefaultTimeSlotsCollection,
		CollectionQuantumJobs:         config.DefaultQuantumJobsCollection,
		CollectionAPITokens:           config.DefaultAPITokensCollection,
		CollectionNotifications:       config.DefaultNotificationsCollection,
		CollectionQPUTimeRequests:     config.DefaultQPUTimeRequestsCollection,
		CollectionDrivers:             config.DefaultDriversCollection,
		CollectionEvents:              config.DefaultEventsCollection,
		CollectionThemes:              config.DefaultThemesCollection,
		CollectionCalibrationResults:  config.DefaultCalibrationResultsCollection,
		CollectionCalibrationRequests: config.DefaultCalibrationRequestsCollection,
		EventsRetention:               retention,
		EventsPruneInterval:           time.Hour,
	}
}

// seedEvent inserts one events row with the given timestamp offset from now.
func seedEvent(t *testing.T, app core.App, cfg *config.AppConfig, age time.Duration) {
	t.Helper()
	col, err := app.FindCollectionByNameOrId(cfg.CollectionEvents)
	if err != nil {
		t.Fatalf("events collection not found: %v", err)
	}
	record := core.NewRecord(col)
	record.Set("source", "drv_test")
	record.Set("type", "CryostatReading")
	record.Set("ts", time.Now().UTC().Add(-age).Format("2006-01-02T15:04:05.000Z"))
	if err := app.Save(record); err != nil {
		t.Fatalf("failed to seed event: %v", err)
	}
}

func countEvents(t *testing.T, app core.App, cfg *config.AppConfig) int {
	t.Helper()
	records, err := app.FindRecordsByFilter(cfg.CollectionEvents, "id != ''", "+ts", 0, 0)
	if err != nil {
		t.Fatalf("failed to count events: %v", err)
	}
	return len(records)
}

// TestPruneEvents_RemovesExpiredKeepsFresh proves the retention prune deletes
// events older than the window and leaves recent ones untouched (RFC 0001 §11,
// Phase 5).
func TestPruneEvents_RemovesExpiredKeepsFresh(t *testing.T) {
	app, err := tests.NewTestApp()
	if err != nil {
		t.Fatalf("failed to create test app: %v", err)
	}
	defer app.Cleanup()

	cfg := retentionConfig(time.Hour)
	config.SaveConfigOnApp(app, cfg)
	if err := db.EnsureSchema(app); err != nil {
		t.Fatalf("failed to ensure schema: %v", err)
	}

	// Two entries older than the 1h window, two within it.
	seedEvent(t, app, cfg, 2*time.Hour)
	seedEvent(t, app, cfg, 90*time.Minute)
	seedEvent(t, app, cfg, 30*time.Minute)
	seedEvent(t, app, cfg, 1*time.Minute)

	pruned, err := PruneEvents(app)
	if err != nil {
		t.Fatalf("PruneEvents error: %v", err)
	}
	if pruned != 2 {
		t.Errorf("expected 2 events pruned, got %d", pruned)
	}
	if remaining := countEvents(t, app, cfg); remaining != 2 {
		t.Errorf("expected 2 events remaining, got %d", remaining)
	}
}

// TestPruneEvents_NoopWhenRetentionDisabled proves a zero window disables
// pruning so nothing is deleted.
func TestPruneEvents_NoopWhenRetentionDisabled(t *testing.T) {
	app, err := tests.NewTestApp()
	if err != nil {
		t.Fatalf("failed to create test app: %v", err)
	}
	defer app.Cleanup()

	cfg := retentionConfig(0)
	config.SaveConfigOnApp(app, cfg)
	if err := db.EnsureSchema(app); err != nil {
		t.Fatalf("failed to ensure schema: %v", err)
	}

	seedEvent(t, app, cfg, 1000*time.Hour)

	pruned, err := PruneEvents(app)
	if err != nil {
		t.Fatalf("PruneEvents error: %v", err)
	}
	if pruned != 0 {
		t.Errorf("expected 0 pruned with retention disabled, got %d", pruned)
	}
	if remaining := countEvents(t, app, cfg); remaining != 1 {
		t.Errorf("expected the event to survive, got %d remaining", remaining)
	}
}

// --- what a QPU's state does to dispatch -------------------------------------
//
// These live here rather than in internal/api because the functions do: the
// dispatcher and the submission hook both call them, and neither is their home.

// schedulerConfig is retentionConfig without a retention window to care about.
func schedulerConfig() *config.AppConfig {
	return retentionConfig(0)
}

// saveModel writes one model and returns its record id, as api's saveToDb does.
func saveModel(t *testing.T, app core.App, model db.DbModel) string {
	t.Helper()
	record, err := model.ToRecord(app)
	if err != nil {
		t.Fatalf("failed to build record: %v", err)
	}
	if err := app.Save(record); err != nil {
		t.Fatalf("failed to save record: %v", err)
	}
	return record.Id
}

// seedChip returns an app with one online QPU and one tuner registered to it.
func seedChip(t *testing.T) (*tests.TestApp, *config.AppConfig, string, string) {
	t.Helper()

	app, err := tests.NewTestApp()
	if err != nil {
		t.Fatalf("failed to create test app: %v", err)
	}
	t.Cleanup(app.Cleanup)

	cfg := schedulerConfig()
	config.SaveConfigOnApp(app, cfg)
	if err := db.EnsureSchema(app); err != nil {
		t.Fatalf("failed to ensure schema: %v", err)
	}

	qpuID := saveModel(t, app, &db.QPU{Name: "qpu_1", Status: "online", Enabled: true})
	driverID := saveModel(t, app, &db.Driver{
		Name: "tuner-1", QPU: qpuID, Kind: "quantify_tuner", Language: "python",
		Token: "hashed", Status: "online", Enabled: true,
	})
	return app, cfg, qpuID, driverID
}

func setQPU(t *testing.T, app core.App, cfg *config.AppConfig, qpuID string, updates map[string]any) {
	t.Helper()
	record, err := app.FindRecordById(cfg.CollectionQPUs, qpuID)
	if err != nil {
		t.Fatalf("failed to find qpu: %v", err)
	}
	for key, value := range updates {
		record.Set(key, value)
	}
	if err := app.Save(record); err != nil {
		t.Fatalf("failed to save qpu: %v", err)
	}
}

func TestUnavailableReason_SaysWhyForEachReason(t *testing.T) {
	app, cfg, qpuID, driverID := seedChip(t)

	if reason := UnavailableReason(app, qpuID); reason != "" {
		t.Fatalf("an online, enabled QPU with no calibration is available, got %q", reason)
	}

	setQPU(t, app, cfg, qpuID, map[string]any{"status": "maintenance"})
	if reason := UnavailableReason(app, qpuID); reason != "this QPU is under maintenance" {
		t.Errorf("expected the maintenance reason, got %q", reason)
	}

	// Switched off outranks the rest: it is the reason nothing else matters.
	setQPU(t, app, cfg, qpuID, map[string]any{"status": "online", "enabled": false})
	if reason := UnavailableReason(app, qpuID); reason != "this QPU is switched off" {
		t.Errorf("expected the switched-off reason, got %q", reason)
	}

	setQPU(t, app, cfg, qpuID, map[string]any{"enabled": true})
	saveModel(t, app, &db.CalibrationRequest{
		Driver: driverID, QPU: qpuID, Mode: "full", Status: "running",
	})
	if reason := UnavailableReason(app, qpuID); reason != "this QPU is being calibrated" {
		t.Errorf("expected the calibration reason, got %q", reason)
	}
}

// An unknown QPU is not "unavailable": a job naming one fails on its relation, and
// a reason here would gate every job whose target had been deleted.
func TestUnavailableReason_IsSilentForAnUnknownQPU(t *testing.T) {
	app, _, _, _ := seedChip(t)

	if reason := UnavailableReason(app, "no-such-qpu"); reason != "" {
		t.Errorf("expected no reason for an unknown QPU, got %q", reason)
	}
}

// A running calibration stops jobs but is not a service state, or the tuner doing
// the calibrating would be told to stop.
func TestServiceStateOf_IsNarrowerThanUnavailableReason(t *testing.T) {
	app, cfg, qpuID, driverID := seedChip(t)

	if got := ServiceStateOf(app, qpuID); got != "online" {
		t.Errorf("expected online, got %q", got)
	}

	setQPU(t, app, cfg, qpuID, map[string]any{"status": "maintenance"})
	if got := ServiceStateOf(app, qpuID); got != "maintenance" {
		t.Errorf("expected maintenance, got %q", got)
	}

	setQPU(t, app, cfg, qpuID, map[string]any{"status": "online", "enabled": false})
	if got := ServiceStateOf(app, qpuID); got != "disabled" {
		t.Errorf("expected disabled, got %q", got)
	}

	setQPU(t, app, cfg, qpuID, map[string]any{"enabled": true})
	saveModel(t, app, &db.CalibrationRequest{
		Driver: driverID, QPU: qpuID, Mode: "full", Status: "running",
	})
	if got := ServiceStateOf(app, qpuID); got != "online" {
		t.Errorf("a calibration must not become a service state, got %q", got)
	}
	if reason := UnavailableReason(app, qpuID); reason == "" {
		t.Error("it should still stop jobs, though")
	}
}

func TestFetchNextJob_HoldsBackWhileTheQPUIsUnavailable(t *testing.T) {
	for _, tc := range []struct {
		name    string
		prepare func(*testing.T, *tests.TestApp, *config.AppConfig, string, string)
	}{
		{"switched off", func(t *testing.T, app *tests.TestApp, cfg *config.AppConfig, qpuID, _ string) {
			setQPU(t, app, cfg, qpuID, map[string]any{"enabled": false})
		}},
		{"maintenance", func(t *testing.T, app *tests.TestApp, cfg *config.AppConfig, qpuID, _ string) {
			setQPU(t, app, cfg, qpuID, map[string]any{"status": "maintenance"})
		}},
		{"calibrating", func(t *testing.T, app *tests.TestApp, _ *config.AppConfig, qpuID, driverID string) {
			saveModel(t, app, &db.CalibrationRequest{
				Driver: driverID, QPU: qpuID, Mode: "full", Status: "running",
			})
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			app, cfg, qpuID, driverID := seedChip(t)
			saveModel(t, app, &db.QuantumJob{QPUTarget: qpuID, Status: "pending"})

			if FetchNextJob(app, qpuID) == nil {
				t.Fatal("the job should be dispatchable before the QPU is made unavailable")
			}

			tc.prepare(t, app, cfg, qpuID, driverID)

			if job := FetchNextJob(app, qpuID); job != nil {
				t.Errorf("expected no dispatch, got job %s", job.ID)
			}
		})
	}
}

// The queue waits rather than being failed, so a short maintenance costs nothing.
func TestFetchNextJob_ResumesWhenTheQPUComesBack(t *testing.T) {
	app, cfg, qpuID, _ := seedChip(t)
	saveModel(t, app, &db.QuantumJob{QPUTarget: qpuID, Status: "pending"})

	setQPU(t, app, cfg, qpuID, map[string]any{"status": "maintenance"})
	if job := FetchNextJob(app, qpuID); job != nil {
		t.Fatalf("expected no dispatch under maintenance, got %s", job.ID)
	}

	setQPU(t, app, cfg, qpuID, map[string]any{"status": "online"})
	if FetchNextJob(app, qpuID) == nil {
		t.Error("the job should be dispatched once maintenance is over, not failed")
	}
}

// --- the calibration queue ---------------------------------------------------

func TestFetchNextCalibration_ReturnsTheOldestPending(t *testing.T) {
	app, _, qpuID, driverID := seedChip(t)

	for _, mode := range []string{"full", "fidelity_check"} {
		saveModel(t, app, &db.CalibrationRequest{
			Driver: driverID, QPU: qpuID, Mode: mode, Status: "pending",
		})
	}

	got := FetchNextCalibration(app, driverID)
	if got == nil {
		t.Fatal("expected a pending calibration")
	}
	if got.Mode != "full" {
		t.Errorf("expected the oldest pending request, got mode %q", got.Mode)
	}
}

// Queued, not refused (RFC 0004 §6.8).
func TestFetchNextCalibration_WaitsWhileOneIsRunning(t *testing.T) {
	app, _, qpuID, driverID := seedChip(t)

	for _, status := range []string{"running", "pending"} {
		saveModel(t, app, &db.CalibrationRequest{
			Driver: driverID, QPU: qpuID, Mode: "full", Status: status,
		})
	}

	if got := FetchNextCalibration(app, driverID); got != nil {
		t.Errorf("expected nothing while a calibration is running, got %s", got.ID)
	}
}

// Two tuners on one QPU cannot calibrate it at once: nothing stops both being
// registered, and each has its own dispatcher, so waiting per driver would let
// both sweep the same qubits and both write the device YAML.
func TestFetchNextCalibration_WaitsWhileAnotherTunerHasTheChip(t *testing.T) {
	app, _, qpuID, driverID := seedChip(t)

	other := saveModel(t, app, &db.Driver{
		Name: "tuner-2", QPU: qpuID, Kind: "quantify_tuner", Language: "python",
		Token: "hashed-2", Status: "online", Enabled: true,
	})
	saveModel(t, app, &db.CalibrationRequest{
		Driver: other, QPU: qpuID, Mode: "full", Status: "running",
	})
	saveModel(t, app, &db.CalibrationRequest{
		Driver: driverID, QPU: qpuID, Mode: "full", Status: "pending",
	})

	if got := FetchNextCalibration(app, driverID); got != nil {
		t.Errorf("expected nothing while another tuner has the chip, got %s", got.ID)
	}
}

// And the wait is scoped to one chip rather than stalling every tuner in the rack.
func TestFetchNextCalibration_IgnoresAnotherQPUsTuner(t *testing.T) {
	app, _, qpuID, driverID := seedChip(t)

	otherQPU := saveModel(t, app, &db.QPU{Name: "qpu_2", Status: "online", Enabled: true})
	other := saveModel(t, app, &db.Driver{
		Name: "tuner-elsewhere", QPU: otherQPU, Kind: "quantify_tuner", Language: "python",
		Token: "hashed-3", Status: "online", Enabled: true,
	})
	saveModel(t, app, &db.CalibrationRequest{
		Driver: other, QPU: otherQPU, Mode: "full", Status: "running",
	})
	saveModel(t, app, &db.CalibrationRequest{
		Driver: driverID, QPU: qpuID, Mode: "full", Status: "pending",
	})

	if got := FetchNextCalibration(app, driverID); got == nil {
		t.Error("expected the pending calibration; another QPU's tuner is irrelevant")
	}
}

func TestFetchNextCalibration_IgnoresOtherDrivers(t *testing.T) {
	app, _, qpuID, driverID := seedChip(t)

	saveModel(t, app, &db.CalibrationRequest{
		Driver: driverID, QPU: qpuID, Mode: "full", Status: "pending",
	})

	if got := FetchNextCalibration(app, "some-other-driver"); got != nil {
		t.Errorf("expected nothing for another driver, got %s", got.ID)
	}
}

// --- the calibration path's retention (RFC 0006 §9) --------------------------

// calibrationRetentionConfig turns each of the three policies on independently, so a
// test says which one it is about.
func calibrationRetentionConfig(requests, results, fits time.Duration) *config.AppConfig {
	cfg := retentionConfig(0)
	cfg.CalibrationRequestRetention = requests
	cfg.CalibrationResultRetention = results
	cfg.CalibrationFitRetention = fits
	return cfg
}

// ageRow backdates a row's `created`. It cannot go in through the model: `created` is
// an autodate PocketBase sets itself on insert.
func ageRow(t *testing.T, app core.App, collection, id string, age time.Duration) {
	t.Helper()
	stamp := time.Now().UTC().Add(-age).Format("2006-01-02 15:04:05.000Z")
	_, err := app.DB().NewQuery(
		"UPDATE {{" + collection + "}} SET created = {:created} WHERE id = {:id}",
	).Bind(map[string]any{"created": stamp, "id": id}).Execute()
	if err != nil {
		t.Fatalf("failed to backdate %s %s: %v", collection, id, err)
	}
}

func countRows(t *testing.T, app core.App, collection string) int {
	t.Helper()
	records, err := app.FindRecordsByFilter(collection, "id != ''", "+created", 0, 0)
	if err != nil {
		t.Fatalf("failed to count %s: %v", collection, err)
	}
	return len(records)
}

// seedCalibrated returns an app with one QPU, one tuner, and *cfg* saved on it.
func seedCalibrated(t *testing.T, cfg *config.AppConfig) (*tests.TestApp, string, string) {
	t.Helper()
	app, err := tests.NewTestApp()
	if err != nil {
		t.Fatalf("failed to create test app: %v", err)
	}
	t.Cleanup(app.Cleanup)

	config.SaveConfigOnApp(app, cfg)
	if err := db.EnsureSchema(app); err != nil {
		t.Fatalf("failed to ensure schema: %v", err)
	}
	qpuID := saveModel(t, app, &db.QPU{Name: "qpu_1", Status: "online", Enabled: true})
	driverID := saveModel(t, app, &db.Driver{
		Name: "tuner-1", QPU: qpuID, Kind: "quantify_tuner", Language: "python",
		Token: "hashed", Status: "online", Enabled: true,
	})
	return app, qpuID, driverID
}

// TestPruneCalibrations_FinishedRequestsAreBookkeeping proves a done or failed row is
// pruned once past its window, and that a `running` one never is however old: a hung
// calibration would otherwise lose its record while still running.
func TestPruneCalibrations_FinishedRequestsAreBookkeeping(t *testing.T) {
	cfg := calibrationRetentionConfig(time.Hour, 0, 0)
	app, qpuID, driverID := seedCalibrated(t, cfg)

	for _, seed := range []struct {
		status string
		age    time.Duration
	}{
		{"done", 2 * time.Hour},
		{"failed", 2 * time.Hour},
		{"done", 10 * time.Minute},
		{"running", 1000 * time.Hour},
		{"pending", 1000 * time.Hour},
	} {
		id := saveModel(t, app, &db.CalibrationRequest{
			Driver: driverID, QPU: qpuID, Mode: "full", Status: seed.status,
		})
		ageRow(t, app, cfg.CollectionCalibrationRequests, id, seed.age)
	}

	requests, _, _, err := PruneCalibrations(app)
	if err != nil {
		t.Fatalf("PruneCalibrations error: %v", err)
	}
	if requests != 2 {
		t.Errorf("expected the 2 aged finished rows pruned, got %d", requests)
	}
	if remaining := countRows(t, app, cfg.CollectionCalibrationRequests); remaining != 3 {
		t.Errorf("expected the fresh, running and pending rows to survive, got %d", remaining)
	}
}

// TestPruneCalibrations_ReportsSurviveByDefault is the policy: a report is what the
// chip *was*, and nobody loses that history by accident (RFC 0004 §9).
func TestPruneCalibrations_ReportsSurviveByDefault(t *testing.T) {
	cfg := calibrationRetentionConfig(time.Hour, 0, 0)
	app, qpuID, driverID := seedCalibrated(t, cfg)

	id := saveModel(t, app, &db.CalibrationResult{
		Driver: driverID, QPU: qpuID, Mode: "full", Status: "success",
		Timestamp: "2020-01-01T00:00:00.000Z",
	})
	ageRow(t, app, cfg.CollectionCalibrationResults, id, 10000*time.Hour)

	_, results, _, err := PruneCalibrations(app)
	if err != nil {
		t.Fatalf("PruneCalibrations error: %v", err)
	}
	if results != 0 {
		t.Errorf("expected no reports pruned with the default of 0, got %d", results)
	}
	if remaining := countRows(t, app, cfg.CollectionCalibrationResults); remaining != 1 {
		t.Errorf("expected the report to survive, got %d remaining", remaining)
	}
}

// TestPruneCalibrations_ReportsGoWhenAnOperatorAsks proves the lever works when set.
func TestPruneCalibrations_ReportsGoWhenAnOperatorAsks(t *testing.T) {
	cfg := calibrationRetentionConfig(0, time.Hour, 0)
	app, qpuID, driverID := seedCalibrated(t, cfg)

	old := saveModel(t, app, &db.CalibrationResult{
		Driver: driverID, QPU: qpuID, Mode: "full", Status: "success", Timestamp: "2026-08-06T12:00:00.000Z",
	})
	ageRow(t, app, cfg.CollectionCalibrationResults, old, 2*time.Hour)
	saveModel(t, app, &db.CalibrationResult{
		Driver: driverID, QPU: qpuID, Mode: "full", Status: "success", Timestamp: "2026-08-06T12:00:00.000Z",
	})

	_, results, _, err := PruneCalibrations(app)
	if err != nil {
		t.Fatalf("PruneCalibrations error: %v", err)
	}
	if results != 1 {
		t.Errorf("expected the aged report pruned, got %d", results)
	}
	if remaining := countRows(t, app, cfg.CollectionCalibrationResults); remaining != 1 {
		t.Errorf("expected the fresh report to survive, got %d remaining", remaining)
	}
}

// TestPruneCalibrations_FitsGoSeparatelyFromTheReport is the whole point of the third
// policy: the traces are ~90% of the row and the least durable part of its value, and
// the fitted numbers they came with are the record of what the chip was.
func TestPruneCalibrations_FitsGoSeparatelyFromTheReport(t *testing.T) {
	cfg := calibrationRetentionConfig(0, 0, time.Hour)
	app, qpuID, driverID := seedCalibrated(t, cfg)

	id := saveModel(t, app, &db.CalibrationResult{
		Driver: driverID, QPU: qpuID, Mode: "full", Status: "success", Timestamp: "2026-08-06T12:00:00.000Z",
		RoutineResults: []map[string]any{{
			"routine_name": "rabi",
			"target":       "q0",
			"parameters":   map[string]any{"rxy.amp180": 0.2031},
			"fit":          map[string]any{"x": []float64{1, 2}, "measured": []float64{1, 0.5}},
		}},
		Benchmarks: []map[string]any{{
			"protocol": "rb", "target": "q0", "fidelity": 0.9993,
			"raw_data": map[string]any{"depths": []int{1, 2, 4}},
		}},
	})
	ageRow(t, app, cfg.CollectionCalibrationResults, id, 2*time.Hour)

	_, results, stripped, err := PruneCalibrations(app)
	if err != nil {
		t.Fatalf("PruneCalibrations error: %v", err)
	}
	if results != 0 || stripped != 1 {
		t.Fatalf("expected 1 report stripped and none pruned, got %d/%d", stripped, results)
	}

	record, err := app.FindRecordById(cfg.CollectionCalibrationResults, id)
	if err != nil {
		t.Fatalf("reload report: %v", err)
	}
	var routines []map[string]any
	if err := json.Unmarshal([]byte(record.GetString("routine_results")), &routines); err != nil {
		t.Fatalf("routine_results is not json: %v", err)
	}
	if _, ok := routines[0]["fit"]; ok {
		t.Error("expected the trace stripped")
	}
	// Everything the report is actually for is untouched.
	if routines[0]["routine_name"] != "rabi" {
		t.Errorf("expected the result itself intact, got %v", routines[0])
	}
	params, _ := routines[0]["parameters"].(map[string]any)
	if params["rxy.amp180"] != 0.2031 {
		t.Errorf("expected the fitted parameter intact, got %v", params)
	}

	var benchmarks []map[string]any
	if err := json.Unmarshal([]byte(record.GetString("benchmarks")), &benchmarks); err != nil {
		t.Fatalf("benchmarks is not json: %v", err)
	}
	if _, ok := benchmarks[0]["raw_data"]; ok {
		t.Error("expected the benchmark's raw data stripped")
	}
	if benchmarks[0]["fidelity"] != 0.9993 {
		t.Errorf("expected the fidelity intact, got %v", benchmarks[0])
	}
}

// TestPruneCalibrations_StrippingIsIdempotent proves a second pass reports no work.
// Without it the engine would re-save every aged report on every tick, forever.
func TestPruneCalibrations_StrippingIsIdempotent(t *testing.T) {
	cfg := calibrationRetentionConfig(0, 0, time.Hour)
	app, qpuID, driverID := seedCalibrated(t, cfg)

	id := saveModel(t, app, &db.CalibrationResult{
		Driver: driverID, QPU: qpuID, Mode: "full", Status: "success", Timestamp: "2026-08-06T12:00:00.000Z",
		RoutineResults: []map[string]any{{"routine_name": "rabi", "fit": map[string]any{"x": []float64{1}}}},
		Benchmarks:     []map[string]any{},
	})
	ageRow(t, app, cfg.CollectionCalibrationResults, id, 2*time.Hour)

	if _, _, stripped, _ := PruneCalibrations(app); stripped != 1 {
		t.Fatalf("expected 1 stripped on the first pass, got %d", stripped)
	}
	if _, _, stripped, _ := PruneCalibrations(app); stripped != 0 {
		t.Errorf("expected nothing left to strip, got %d", stripped)
	}
}

// TestPruneCalibrations_NoopWhenEveryPolicyIsOff keeps the whole feature switchable.
func TestPruneCalibrations_NoopWhenEveryPolicyIsOff(t *testing.T) {
	cfg := calibrationRetentionConfig(0, 0, 0)
	app, qpuID, driverID := seedCalibrated(t, cfg)

	id := saveModel(t, app, &db.CalibrationRequest{
		Driver: driverID, QPU: qpuID, Mode: "full", Status: "done",
	})
	ageRow(t, app, cfg.CollectionCalibrationRequests, id, 10000*time.Hour)

	requests, results, stripped, err := PruneCalibrations(app)
	if err != nil {
		t.Fatalf("PruneCalibrations error: %v", err)
	}
	if requests+results+stripped != 0 {
		t.Errorf("expected nothing touched, got %d/%d/%d", requests, results, stripped)
	}
	if remaining := countRows(t, app, cfg.CollectionCalibrationRequests); remaining != 1 {
		t.Errorf("expected the row to survive, got %d remaining", remaining)
	}
}
