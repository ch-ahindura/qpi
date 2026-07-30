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
		CollectionQPUs:               config.DefaultQpusCollection,
		CollectionTimeSlots:          config.DefaultTimeSlotsCollection,
		CollectionQuantumJobs:        config.DefaultQuantumJobsCollection,
		CollectionAPITokens:          config.DefaultAPITokensCollection,
		CollectionNotifications:      config.DefaultNotificationsCollection,
		CollectionQPUTimeRequests:    config.DefaultQPUTimeRequestsCollection,
		CollectionDrivers:            config.DefaultDriversCollection,
		CollectionEvents:             config.DefaultEventsCollection,
		CollectionThemes:             config.DefaultThemesCollection,
		CollectionCalibrationResults: config.DefaultCalibrationResultsCollection,
		EventsRetention:              retention,
		EventsPruneInterval:          time.Hour,
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
