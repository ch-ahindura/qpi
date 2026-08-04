package api

import (
	"testing"

	"github.com/pocketbase/pocketbase/tests"

	"qpi/internal/config"
	"qpi/internal/db"
	"qpi/internal/drivers"
	"qpi/internal/scheduler"
)

// A QPU takes jobs only when it is switched on, not under maintenance, and not
// being calibrated. Before these, nothing in the dispatch path read the QPU at all.

func seedForGate(t *testing.T) (*tests.TestApp, *config.AppConfig, *db.QPU, *db.Driver) {
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

	qpu := &db.QPU{Name: "qpu_1", Status: "online", Enabled: true}
	if err := saveToDb(app, qpu); err != nil {
		t.Fatalf("failed to create qpu: %v", err)
	}

	tuner := &db.Driver{
		Name: "tuner-1", QPU: qpu.ID,
		Kind: string(drivers.QuantifyTuner), Language: "python",
		Token: db.HashToken("tok"), Status: "online", Enabled: true,
	}
	if err := saveToDb(app, tuner); err != nil {
		t.Fatalf("failed to create driver: %v", err)
	}
	return app, cfg, qpu, tuner
}

func queueJob(t *testing.T, app *tests.TestApp, cfg *config.AppConfig, qpuID string) {
	t.Helper()
	job := &db.QuantumJob{QPUTarget: qpuID, Status: "pending"}
	if err := saveToDb(app, job); err != nil {
		t.Fatalf("failed to queue job: %v", err)
	}
}

func TestQPUUnavailable_SaysWhyForEachReason(t *testing.T) {
	app, _, qpu, tuner := seedForGate(t)

	if reason := scheduler.QPUUnavailable(app, qpu.ID); reason != "" {
		t.Fatalf("an online, enabled QPU with no calibration is available, got %q", reason)
	}

	qpu.Status = "maintenance"
	if err := saveToDb(app, qpu); err != nil {
		t.Fatalf("save: %v", err)
	}
	if reason := scheduler.QPUUnavailable(app, qpu.ID); reason != "this QPU is under maintenance" {
		t.Errorf("expected the maintenance reason, got %q", reason)
	}

	qpu.Status = "online"
	qpu.Enabled = false
	if err := saveToDb(app, qpu); err != nil {
		t.Fatalf("save: %v", err)
	}
	// Switched off outranks the rest: it is the reason nothing else matters.
	if reason := scheduler.QPUUnavailable(app, qpu.ID); reason != "this QPU is switched off" {
		t.Errorf("expected the switched-off reason, got %q", reason)
	}

	qpu.Enabled = true
	if err := saveToDb(app, qpu); err != nil {
		t.Fatalf("save: %v", err)
	}
	if err := saveToDb(app, &db.CalibrationRequest{
		Driver: tuner.ID, QPU: qpu.ID, Mode: "full", Status: "running",
	}); err != nil {
		t.Fatalf("seed calibration: %v", err)
	}
	if reason := scheduler.QPUUnavailable(app, qpu.ID); reason != "this QPU is being calibrated" {
		t.Errorf("expected the calibration reason, got %q", reason)
	}
}

func TestFetchNextJob_HoldsBackWhileTheQPUIsUnavailable(t *testing.T) {
	for _, tc := range []struct {
		name    string
		prepare func(*tests.TestApp, *config.AppConfig, *db.QPU, *db.Driver)
	}{
		{"switched off", func(app *tests.TestApp, _ *config.AppConfig, qpu *db.QPU, _ *db.Driver) {
			qpu.Enabled = false
			_ = saveToDb(app, qpu)
		}},
		{"maintenance", func(app *tests.TestApp, _ *config.AppConfig, qpu *db.QPU, _ *db.Driver) {
			qpu.Status = "maintenance"
			_ = saveToDb(app, qpu)
		}},
		{"calibrating", func(app *tests.TestApp, _ *config.AppConfig, qpu *db.QPU, d *db.Driver) {
			_ = saveToDb(app, &db.CalibrationRequest{
				Driver: d.ID, QPU: qpu.ID, Mode: "full", Status: "running",
			})
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			app, cfg, qpu, tuner := seedForGate(t)
			queueJob(t, app, cfg, qpu.ID)

			if scheduler.FetchNextJob(app, qpu.ID) == nil {
				t.Fatal("the job should be dispatchable before the QPU is made unavailable")
			}

			tc.prepare(app, cfg, qpu, tuner)

			if job := scheduler.FetchNextJob(app, qpu.ID); job != nil {
				t.Errorf("expected no dispatch, got job %s", job.ID)
			}
		})
	}
}

// The queue waits rather than being failed, so a short maintenance costs nothing.
func TestFetchNextJob_ResumesWhenTheQPUComesBack(t *testing.T) {
	app, cfg, qpu, _ := seedForGate(t)
	queueJob(t, app, cfg, qpu.ID)

	qpu.Status = "maintenance"
	if err := saveToDb(app, qpu); err != nil {
		t.Fatalf("save: %v", err)
	}
	if job := scheduler.FetchNextJob(app, qpu.ID); job != nil {
		t.Fatalf("expected no dispatch under maintenance, got %s", job.ID)
	}

	qpu.Status = "online"
	if err := saveToDb(app, qpu); err != nil {
		t.Fatalf("save: %v", err)
	}
	if scheduler.FetchNextJob(app, qpu.ID) == nil {
		t.Error("the job should be dispatched once maintenance is over, not failed")
	}
}

// An unknown QPU is not "unavailable" — a job naming one fails on its relation, and
// reporting a reason here would gate every job whose target had been deleted.
func TestQPUUnavailable_IsSilentForAnUnknownQPU(t *testing.T) {
	app, _, _, _ := seedForGate(t)

	if reason := scheduler.QPUUnavailable(app, "no-such-qpu"); reason != "" {
		t.Errorf("expected no reason for an unknown QPU, got %q", reason)
	}
}
