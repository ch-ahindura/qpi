package db

import (
	"testing"

	"qpi/internal/config"

	"github.com/pocketbase/pocketbase/core"
	"github.com/pocketbase/pocketbase/tests"
	"github.com/pocketbase/pocketbase/tools/hook"
)

// TestOnDriverCreate_HashesTokenOnSave proves a driver's raw token gets
// hashed the same way OnQpuCreate hashes a QPU's access_token — via the
// real app.Save() hook chain, mirroring how main.go wires it (RFC 0001 §7, §9).
func TestOnDriverCreate_HashesTokenOnSave(t *testing.T) {
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

	app.OnRecordCreate().Bind(&hook.Handler[*core.RecordEvent]{
		Func: RegisterCollectionHooks(app, CollectionHookMap{
			config.DefaultDriversCollection: OnDriverCreate,
		}),
	})

	qpuCol, err := app.FindCollectionByNameOrId(config.DefaultQpusCollection)
	if err != nil {
		t.Fatalf("qpus collection not found: %v", err)
	}
	qpuRec := core.NewRecord(qpuCol)
	qpuRec.Set("name", "qpu_for_driver_test")
	qpuRec.Set("access_token", "qpu_raw_token")
	qpuRec.Set("status", "offline")
	qpuRec.Set("enabled", true)
	if err := app.Save(qpuRec); err != nil {
		t.Fatalf("failed to create qpu: %v", err)
	}

	driversCol, err := app.FindCollectionByNameOrId(config.DefaultDriversCollection)
	if err != nil {
		t.Fatalf("drivers collection not found: %v", err)
	}
	driverRec := core.NewRecord(driversCol)
	driverRec.Set("name", "driver_1")
	driverRec.Set("qpu", qpuRec.Id)
	driverRec.Set("kind", "mock")
	driverRec.Set("language", "python")
	driverRec.Set("events", []string{"JobDispatch", "JobResult"})
	driverRec.Set("status", "offline")
	driverRec.Set("enabled", true)
	driverRec.Set("token", "raw_driver_token")

	if err := app.Save(driverRec); err != nil {
		t.Fatalf("failed to create driver: %v", err)
	}

	got := driverRec.GetString("token")
	want := HashToken("raw_driver_token")
	if got != want {
		t.Errorf("stored token = %q, want hashed %q", got, want)
	}
	if got == "raw_driver_token" {
		t.Errorf("expected the raw token not to be stored as-is")
	}
}

// TestOnThemeUpsert_DeactivatesPreviousActiveTheme proves that when a new theme
// is saved with is_active=true, any previously active theme is automatically deactivated.
func TestOnThemeUpsert_DeactivatesPreviousActiveTheme(t *testing.T) {
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

	app.OnRecordCreate().Bind(&hook.Handler[*core.RecordEvent]{
		Func: RegisterCollectionHooks(app, CollectionHookMap{
			config.DefaultThemesCollection: OnThemeUpsert,
		}),
	})
	app.OnRecordUpdate().Bind(&hook.Handler[*core.RecordEvent]{
		Func: RegisterCollectionHooks(app, CollectionHookMap{
			config.DefaultThemesCollection: OnThemeUpsert,
		}),
	})

	themesCol, err := app.FindCollectionByNameOrId(config.DefaultThemesCollection)
	if err != nil {
		t.Fatalf("themes collection not found: %v", err)
	}

	theme1 := core.NewRecord(themesCol)
	theme1.Set("name", "Theme 1")
	theme1.Set("is_active", true)
	if err := app.Save(theme1); err != nil {
		t.Fatalf("failed to save theme1: %v", err)
	}

	if !theme1.GetBool("is_active") {
		t.Fatalf("expected theme1 to be active")
	}

	theme2 := core.NewRecord(themesCol)
	theme2.Set("name", "Theme 2")
	theme2.Set("is_active", true)
	if err := app.Save(theme2); err != nil {
		t.Fatalf("failed to save theme2: %v", err)
	}

	reloadedTheme1, err := app.FindRecordById(config.DefaultThemesCollection, theme1.Id)
	if err != nil {
		t.Fatalf("failed to reload theme1: %v", err)
	}

	if reloadedTheme1.GetBool("is_active") {
		t.Errorf("expected theme1 to be deactivated after theme2 became active")
	}

	if !theme2.GetBool("is_active") {
		t.Errorf("expected theme2 to remain active")
	}
}

// TestConnectedQpuStatus_DerivesFromThePortPair proves what clearing maintenance
// falls back to. Writing `online` there would claim a connection the QPU may not
// have, which is why the endpoint asks this rather than guessing.
func TestConnectedQpuStatus_DerivesFromThePortPair(t *testing.T) {
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

	col, err := app.FindCollectionByNameOrId(cfg.CollectionQPUs)
	if err != nil {
		t.Fatalf("qpus collection not found: %v", err)
	}
	record := core.NewRecord(col)
	record.Set("name", "qpu_1")
	record.Set("status", "online")
	record.Set("enabled", true)

	if got := ConnectedQpuStatus(record); got != "offline" {
		t.Errorf("with no ports allocated, expected offline, got %q", got)
	}

	record.Set("nng_command_port", 6201)
	record.Set("nng_result_port", 6202)
	if got := ConnectedQpuStatus(record); got != "online" {
		t.Errorf("with a port pair, expected online, got %q", got)
	}
}
