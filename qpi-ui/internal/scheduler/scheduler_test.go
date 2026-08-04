package scheduler

import (
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

func TestQPUUnavailable_SaysWhyForEachReason(t *testing.T) {
	app, cfg, qpuID, driverID := seedChip(t)

	if reason := QPUUnavailable(app, qpuID); reason != "" {
		t.Fatalf("an online, enabled QPU with no calibration is available, got %q", reason)
	}

	setQPU(t, app, cfg, qpuID, map[string]any{"status": "maintenance"})
	if reason := QPUUnavailable(app, qpuID); reason != "this QPU is under maintenance" {
		t.Errorf("expected the maintenance reason, got %q", reason)
	}

	// Switched off outranks the rest: it is the reason nothing else matters.
	setQPU(t, app, cfg, qpuID, map[string]any{"status": "online", "enabled": false})
	if reason := QPUUnavailable(app, qpuID); reason != "this QPU is switched off" {
		t.Errorf("expected the switched-off reason, got %q", reason)
	}

	setQPU(t, app, cfg, qpuID, map[string]any{"enabled": true})
	saveModel(t, app, &db.CalibrationRequest{
		Driver: driverID, QPU: qpuID, Mode: "full", Status: "running",
	})
	if reason := QPUUnavailable(app, qpuID); reason != "this QPU is being calibrated" {
		t.Errorf("expected the calibration reason, got %q", reason)
	}
}

// An unknown QPU is not "unavailable": a job naming one fails on its relation, and
// a reason here would gate every job whose target had been deleted.
func TestQPUUnavailable_IsSilentForAnUnknownQPU(t *testing.T) {
	app, _, _, _ := seedChip(t)

	if reason := QPUUnavailable(app, "no-such-qpu"); reason != "" {
		t.Errorf("expected no reason for an unknown QPU, got %q", reason)
	}
}

// QPUServiceState is what drivers are told; QPUUnavailable is what stops jobs. A
// running calibration belongs only to the second, or the tuner doing the
// calibrating would be told to stop.
func TestQPUServiceState_IsNarrowerThanUnavailable(t *testing.T) {
	app, cfg, qpuID, driverID := seedChip(t)

	if got := QPUServiceState(app, qpuID); got != "online" {
		t.Errorf("expected online, got %q", got)
	}

	setQPU(t, app, cfg, qpuID, map[string]any{"status": "maintenance"})
	if got := QPUServiceState(app, qpuID); got != "maintenance" {
		t.Errorf("expected maintenance, got %q", got)
	}

	setQPU(t, app, cfg, qpuID, map[string]any{"status": "online", "enabled": false})
	if got := QPUServiceState(app, qpuID); got != "disabled" {
		t.Errorf("expected disabled, got %q", got)
	}

	setQPU(t, app, cfg, qpuID, map[string]any{"enabled": true})
	saveModel(t, app, &db.CalibrationRequest{
		Driver: driverID, QPU: qpuID, Mode: "full", Status: "running",
	})
	if got := QPUServiceState(app, qpuID); got != "online" {
		t.Errorf("a calibration must not become a service state, got %q", got)
	}
	if reason := QPUUnavailable(app, qpuID); reason == "" {
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
