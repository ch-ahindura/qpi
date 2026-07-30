package db

import (
	"fmt"
	"strings"
	"testing"

	"qpi/internal/config"

	"github.com/pocketbase/pocketbase/core"
	"github.com/pocketbase/pocketbase/tests"
)

// testConfig builds an AppConfig with the default collection names, mirroring
// the setup every other db/api test uses.
func testConfig() *config.AppConfig {
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
	}
}

// TestEnsureSchema_DriversCollection proves that EnsureSchema creates the drivers collection.
func TestEnsureSchema_DriversCollection(t *testing.T) {
	app, err := tests.NewTestApp()
	if err != nil {
		t.Fatalf("failed to create test app: %v", err)
	}
	defer app.Cleanup()

	cfg := testConfig()
	config.SaveConfigOnApp(app, cfg)
	if err := EnsureSchema(app); err != nil {
		t.Fatalf("failed to ensure schema: %v", err)
	}

	if _, err := app.FindCollectionByNameOrId(config.DefaultDriversCollection); err != nil {
		t.Fatalf("expected drivers collection to exist: %v", err)
	}
}

// TestEnsureSchema_ThemesCollection proves that EnsureSchema creates the themes collection with public read and superuser-only CUD rules.
func TestEnsureSchema_ThemesCollection(t *testing.T) {
	app, err := tests.NewTestApp()
	if err != nil {
		t.Fatalf("failed to create test app: %v", err)
	}
	defer app.Cleanup()

	cfg := testConfig()
	config.SaveConfigOnApp(app, cfg)
	if err := EnsureSchema(app); err != nil {
		t.Fatalf("failed to ensure schema: %v", err)
	}

	col, err := app.FindCollectionByNameOrId(config.DefaultThemesCollection)
	if err != nil {
		t.Fatalf("expected themes collection to exist: %v", err)
	}

	wantRules := "list= view= create=nil update=nil delete=nil"
	if got := collectionRuleSnapshot(col); got != wantRules {
		t.Errorf("themes collection rules = %q, want %q", got, wantRules)
	}

	wantFields := []string{"name", "is_active", "site_name", "tagline", "logo", "favicon", "tokens", "custom_css", "custom_js", "created", "updated"}
	for _, name := range wantFields {
		found := false
		for _, f := range col.Fields {
			if f.GetName() == name {
				found = true
				break
			}
		}
		if !found {
			t.Errorf("expected themes collection to have field %q", name)
		}
	}
}

// TestEnsureSchema_EventsCollection proves that EnsureSchema creates the events collection.
func TestEnsureSchema_EventsCollection(t *testing.T) {
	app, err := tests.NewTestApp()
	if err != nil {
		t.Fatalf("failed to create test app: %v", err)
	}
	defer app.Cleanup()

	cfg := testConfig()
	config.SaveConfigOnApp(app, cfg)
	if err := EnsureSchema(app); err != nil {
		t.Fatalf("failed to ensure schema: %v", err)
	}

	col, err := app.FindCollectionByNameOrId(config.DefaultEventsCollection)
	if err != nil {
		t.Fatalf("expected events collection to exist: %v", err)
	}

	wantFields := []string{"source", "driver", "qpu", "type", "payload", "ts", "created"}
	for _, name := range wantFields {
		found := false
		for _, f := range col.Fields {
			if f.GetName() == name {
				found = true
				break
			}
		}
		if !found {
			t.Errorf("expected events collection to have field %q", name)
		}
	}

	wantIndex := fmt.Sprintf("idx_%s_type_ts", config.DefaultEventsCollection)
	if !hasIndex(col, wantIndex) {
		t.Errorf("expected events collection to have index %q, got %v", wantIndex, col.Indexes)
	}
}

// TestEnsureSchema_EventsIndexIdempotent proves running the migration twice
// does not duplicate the (type, ts) index.
func TestEnsureSchema_EventsIndexIdempotent(t *testing.T) {
	app, err := tests.NewTestApp()
	if err != nil {
		t.Fatalf("failed to create test app: %v", err)
	}
	defer app.Cleanup()

	cfg := testConfig()
	config.SaveConfigOnApp(app, cfg)
	if err := EnsureSchema(app); err != nil {
		t.Fatalf("failed to ensure schema (first pass): %v", err)
	}
	if err := EnsureSchema(app); err != nil {
		t.Fatalf("failed to ensure schema (second pass): %v", err)
	}

	col, err := app.FindCollectionByNameOrId(config.DefaultEventsCollection)
	if err != nil {
		t.Fatalf("events collection not found: %v", err)
	}

	wantIndex := fmt.Sprintf("idx_%s_type_ts", config.DefaultEventsCollection)
	count := 0
	for _, idx := range col.Indexes {
		if strings.Contains(idx, wantIndex) {
			count++
		}
	}
	if count != 1 {
		t.Errorf("expected exactly one %q index, got %d: %v", wantIndex, count, col.Indexes)
	}
}

// collectionRuleSnapshot renders a collection's API rules into a comparable
// string, treating a nil rule as the literal "nil".
func collectionRuleSnapshot(col *core.Collection) string {
	rule := func(r *string) string {
		if r == nil {
			return "nil"
		}
		return *r
	}
	return fmt.Sprintf(
		"list=%s view=%s create=%s update=%s delete=%s",
		rule(col.ListRule), rule(col.ViewRule), rule(col.CreateRule), rule(col.UpdateRule), rule(col.DeleteRule),
	)
}
