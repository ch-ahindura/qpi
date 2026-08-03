package db

import (
	"fmt"
	"slices"
	"strings"
	"testing"

	"qpi/internal/config"
	"qpi/internal/drivers"

	"github.com/pocketbase/pocketbase/core"
	"github.com/pocketbase/pocketbase/tests"
)

// testConfig builds an AppConfig with the default collection names, mirroring
// the setup every other db/api test uses.
func testConfig() *config.AppConfig {
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

// driverKindValues is what the drivers collection's `kind` select allows.
func driverKindValues(t *testing.T, app core.App) []string {
	t.Helper()
	col, err := app.FindCollectionByNameOrId(config.DefaultDriversCollection)
	if err != nil {
		t.Fatalf("drivers collection not found: %v", err)
	}
	field, ok := col.Fields.GetByName("kind").(*core.SelectField)
	if !ok {
		t.Fatalf("kind is not a select field: %#v", col.Fields.GetByName("kind"))
	}
	return field.Values
}

// TestEnsureSchema_DriverKindsCoverTheCatalog: the endpoint validates against the
// catalog, so a kind it knows and the column does not fails on the insert.
func TestEnsureSchema_DriverKindsCoverTheCatalog(t *testing.T) {
	app, err := tests.NewTestApp()
	if err != nil {
		t.Fatalf("failed to create test app: %v", err)
	}
	defer app.Cleanup()

	config.SaveConfigOnApp(app, testConfig())
	if err := EnsureSchema(app); err != nil {
		t.Fatalf("failed to ensure schema: %v", err)
	}

	allowed := make(map[string]bool)
	for _, v := range driverKindValues(t, app) {
		allowed[v] = true
	}
	for _, kind := range append(drivers.Default.Kinds(), drivers.Custom) {
		if !allowed[string(kind)] {
			t.Errorf("the catalog ships kind %q but the drivers collection rejects it", kind)
		}
	}
}

// TestEnsureSchema_DriverLanguagesCoverTheSDKs is the kind test's twin.
func TestEnsureSchema_DriverLanguagesCoverTheSDKs(t *testing.T) {
	app, err := tests.NewTestApp()
	if err != nil {
		t.Fatalf("failed to create test app: %v", err)
	}
	defer app.Cleanup()

	config.SaveConfigOnApp(app, testConfig())
	if err := EnsureSchema(app); err != nil {
		t.Fatalf("failed to ensure schema: %v", err)
	}

	allowed := AllowedValues(app, config.DefaultDriversCollection, "language")
	for _, language := range drivers.Languages {
		if !slices.Contains(allowed, string(language)) {
			t.Errorf("the SDKs include %q but the drivers collection rejects it", language)
		}
	}
}

// TestEnsureSchema_WidensAnOlderSelect: a collection created by an earlier
// release picks up values added since.
func TestEnsureSchema_WidensAnOlderSelect(t *testing.T) {
	app, err := tests.NewTestApp()
	if err != nil {
		t.Fatalf("failed to create test app: %v", err)
	}
	defer app.Cleanup()

	config.SaveConfigOnApp(app, testConfig())
	if err := EnsureSchema(app); err != nil {
		t.Fatalf("failed to ensure schema: %v", err)
	}

	// What 0.2.0 created, plus an operator's own addition that has to survive.
	col, err := app.FindCollectionByNameOrId(config.DefaultDriversCollection)
	if err != nil {
		t.Fatalf("drivers collection not found: %v", err)
	}
	field := col.Fields.GetByName("kind").(*core.SelectField)
	field.Values = []string{
		"mock", "qiskit_aer", "quantify", "qblox", "presto", "bluefors_gen1",
		"custom", "hand_rolled",
	}
	if err := app.Save(col); err != nil {
		t.Fatalf("failed to roll the kind field back: %v", err)
	}

	if err := EnsureSchema(app); err != nil {
		t.Fatalf("failed to re-ensure schema: %v", err)
	}

	allowed := make(map[string]bool)
	for _, v := range driverKindValues(t, app) {
		allowed[v] = true
	}
	for _, want := range []string{"quantify_tuner", "qblox_tuner", "hand_rolled", "mock"} {
		if !allowed[want] {
			t.Errorf("kind %q not allowed after migration; allowed: %v", want, driverKindValues(t, app))
		}
	}
}

// TestEnsureSchema_WidensEverySelect: the widening is not a drivers special case.
func TestEnsureSchema_WidensEverySelect(t *testing.T) {
	app, err := tests.NewTestApp()
	if err != nil {
		t.Fatalf("failed to create test app: %v", err)
	}
	defer app.Cleanup()

	config.SaveConfigOnApp(app, testConfig())
	if err := EnsureSchema(app); err != nil {
		t.Fatalf("failed to ensure schema: %v", err)
	}

	// Every model that declares a select, emptied of everything the code declares.
	narrowed := map[string]string{
		config.DefaultQpusCollection:                "status",
		config.DefaultQuantumJobsCollection:         "status",
		config.DefaultQPUTimeRequestsCollection:     "status",
		config.DefaultCalibrationResultsCollection:  "mode",
		config.DefaultCalibrationRequestsCollection: "status",
	}
	for name, fieldName := range narrowed {
		col, err := app.FindCollectionByNameOrId(name)
		if err != nil {
			t.Fatalf("%s not found: %v", name, err)
		}
		col.Fields.GetByName(fieldName).(*core.SelectField).Values = []string{"legacy"}
		if err := app.Save(col); err != nil {
			t.Fatalf("failed to narrow %s.%s: %v", name, fieldName, err)
		}
	}

	if err := EnsureSchema(app); err != nil {
		t.Fatalf("failed to re-ensure schema: %v", err)
	}

	for name, fieldName := range narrowed {
		got := AllowedValues(app, name, fieldName)
		if len(got) < 2 || !slices.Contains(got, "legacy") {
			t.Errorf("%s.%s = %v, want the declared values plus the legacy one", name, fieldName, got)
		}
	}
}
