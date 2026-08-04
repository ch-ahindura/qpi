package api

import (
	"fmt"
	"io/fs"
	"net/http"
	"testing"
	"testing/fstest"

	"github.com/pocketbase/dbx"
	"github.com/pocketbase/pocketbase/core"
	"github.com/pocketbase/pocketbase/tests"
	"github.com/pocketbase/pocketbase/tools/hook"

	"qpi/internal/config"
	"qpi/internal/db"
	"qpi/internal/drivers"
)

func TestThemeAPI_Defaults(t *testing.T) {
	scenario := tests.ApiScenario{
		Name:   "GET /api/theme/defaults returns compiled-in defaults",
		Method: http.MethodGet,
		URL:    "/api/theme/defaults",
		BeforeTestFunc: func(t testing.TB, app *tests.TestApp, e *core.ServeEvent) {
			cfg := testConfig()
			config.SaveConfigOnApp(app, cfg)
			_ = db.EnsureSchema(app)
			theme, _ := db.GetActiveThemeSchema(app)
			cfg.UpdateActiveTheme(theme)
			RegisterRoutes(e, testFS())
		},
		ExpectedStatus: http.StatusOK,
		ExpectedContent: []string{
			`"site_name":"QPI Interface"`,
			`"tagline":"Control Hub"`,
			`"colors"`,
		},
		AfterTestFunc: func(t testing.TB, app *tests.TestApp, res *http.Response) {
			if h := res.Header.Get("Cache-Control"); h != "public, max-age=300" {
				t.Errorf("expected Cache-Control 'public, max-age=300', got %q", h)
			}
		},
	}
	scenario.Test(t)
}

func TestThemeAPI_ActiveDefault(t *testing.T) {
	scenario := tests.ApiScenario{
		Name:   "GET /api/theme/active returns default theme when no active theme in DB",
		Method: http.MethodGet,
		URL:    "/api/theme/active",
		BeforeTestFunc: func(t testing.TB, app *tests.TestApp, e *core.ServeEvent) {
			cfg := testConfig()
			config.SaveConfigOnApp(app, cfg)
			_ = db.EnsureSchema(app)
			theme, _ := db.GetActiveThemeSchema(app)
			cfg.UpdateActiveTheme(theme)
			RegisterRoutes(e, testFS())
		},
		ExpectedStatus: http.StatusOK,
		ExpectedContent: []string{
			`"id":"default"`,
			`"name":"Default"`,
			`"site_name":"QPI Interface"`,
			`"tagline":"Control Hub"`,
		},
		AfterTestFunc: func(t testing.TB, app *tests.TestApp, res *http.Response) {
			if h := res.Header.Get("Cache-Control"); h != "" {
				t.Errorf("expected no Cache-Control header, got %q", h)
			}
		},
	}
	scenario.Test(t)
}

func TestThemeAPI_ActiveCustomAndCSSJS(t *testing.T) {
	app, err := tests.NewTestApp()
	if err != nil {
		t.Fatalf("failed to create test app: %v", err)
	}
	defer app.Cleanup()

	cfg := testConfig()
	config.SaveConfigOnApp(app, cfg)
	if err := db.EnsureSchema(app); err != nil {
		t.Fatalf("failed to ensure schema: %v", err)
	}
	theme, err := db.GetActiveThemeSchema(app)
	if err != nil {
		t.Fatalf("failed to get active theme schema: %v", err)
	}
	cfg.UpdateActiveTheme(theme)

	// 1. Initial CSS and JS endpoints return 204 No Content for default theme
	scenarioNoCSS := tests.ApiScenario{
		Name:   "GET /api/theme/css returns 204 when no custom CSS",
		Method: http.MethodGet,
		URL:    "/api/theme/css",
		BeforeTestFunc: func(t testing.TB, app *tests.TestApp, e *core.ServeEvent) {
			config.SaveConfigOnApp(app, cfg)
			_ = db.EnsureSchema(app)
			theme, _ := db.GetActiveThemeSchema(app)
			cfg.UpdateActiveTheme(theme)
			RegisterRoutes(e, testFS())
		},
		ExpectedStatus: http.StatusNoContent,
	}
	scenarioNoCSS.Test(t)

	scenarioNoJS := tests.ApiScenario{
		Name:   "GET /api/theme/js returns 204 when no custom JS",
		Method: http.MethodGet,
		URL:    "/api/theme/js",
		BeforeTestFunc: func(t testing.TB, app *tests.TestApp, e *core.ServeEvent) {
			config.SaveConfigOnApp(app, cfg)
			_ = db.EnsureSchema(app)
			theme, _ := db.GetActiveThemeSchema(app)
			cfg.UpdateActiveTheme(theme)
			RegisterRoutes(e, testFS())
		},
		ExpectedStatus: http.StatusNoContent,
	}
	scenarioNoJS.Test(t)

	setupCustomTheme := func(app *tests.TestApp) {
		app.OnRecordCreate().Bind(&hook.Handler[*core.RecordEvent]{
			Func: db.RegisterCollectionHooks(app, db.CollectionHookMap{
				config.DefaultThemesCollection: db.OnThemeUpsert,
			}),
		})
		app.OnRecordUpdate().Bind(&hook.Handler[*core.RecordEvent]{
			Func: db.RegisterCollectionHooks(app, db.CollectionHookMap{
				config.DefaultThemesCollection: db.OnThemeUpsert,
			}),
		})

		themeRec := &db.Theme{
			ThemeSchema: config.ThemeSchema{
				Name:      "Neon Quantum",
				IsActive:  true,
				SiteName:  "Neon Lab",
				Tagline:   "Future is Quantum",
				CustomCSS: ":root { --qpi-color-primary: #00ffaa; }",
				CustomJS:  "console.log('Neon JS');",
			},
		}
		rec, _ := themeRec.ToRecord(app)
		_ = app.Save(rec)
	}

	// Create custom active theme in the outer app (to test db logic directly)
	setupCustomTheme(app)

	// Verify cached active theme is now Neon Quantum
	cached, err := config.GetActiveThemeFromApp(app)
	if err != nil {
		t.Fatalf("failed to get cached active theme: %v", err)
	}

	if cached == nil || cached.Name != "Neon Quantum" {
		t.Fatalf("expected cached theme to be Neon Quantum, got %v", cached)
	}

	// 3. GET /api/theme/active with custom active theme
	scenarioActive := tests.ApiScenario{
		Name:   "GET /api/theme/active returns custom active theme",
		Method: http.MethodGet,
		URL:    "/api/theme/active",
		BeforeTestFunc: func(t testing.TB, app *tests.TestApp, e *core.ServeEvent) {
			config.SaveConfigOnApp(app, cfg)
			_ = db.EnsureSchema(app)
			setupCustomTheme(app)
			theme, _ := db.GetActiveThemeSchema(app)
			cfg.UpdateActiveTheme(theme)
			RegisterRoutes(e, testFS())
		},
		ExpectedStatus: http.StatusOK,
		ExpectedContent: []string{
			`"name":"Neon Quantum"`,
			`"site_name":"Neon Lab"`,
			`"tagline":"Future is Quantum"`,
		},
		AfterTestFunc: func(t testing.TB, app *tests.TestApp, res *http.Response) {
			if h := res.Header.Get("Cache-Control"); h != "" {
				t.Errorf("expected no Cache-Control header, got %q", h)
			}
		},
	}
	scenarioActive.Test(t)

	// 4. GET /api/theme/css returns custom CSS content
	scenarioCSS := tests.ApiScenario{
		Name:   "GET /api/theme/css returns custom CSS",
		Method: http.MethodGet,
		URL:    "/api/theme/css",
		BeforeTestFunc: func(t testing.TB, app *tests.TestApp, e *core.ServeEvent) {
			config.SaveConfigOnApp(app, cfg)
			_ = db.EnsureSchema(app)
			setupCustomTheme(app)
			theme, _ := db.GetActiveThemeSchema(app)
			cfg.UpdateActiveTheme(theme)
			RegisterRoutes(e, testFS())
		},
		ExpectedStatus:  http.StatusOK,
		ExpectedContent: []string{":root { --qpi-color-primary: #00ffaa; }"},
		AfterTestFunc: func(t testing.TB, app *tests.TestApp, res *http.Response) {
			if h := res.Header.Get("Cache-Control"); h != "public, max-age=300" {
				t.Errorf("expected Cache-Control 'public, max-age=300', got %q", h)
			}
			if h := res.Header.Get("Content-Type"); h != "text/css" {
				t.Errorf("expected Content-Type 'text/css', got %q", h)
			}
		},
	}
	scenarioCSS.Test(t)

	// 5. GET /api/theme/js returns custom JS content
	scenarioJS := tests.ApiScenario{
		Name:   "GET /api/theme/js returns custom JS",
		Method: http.MethodGet,
		URL:    "/api/theme/js",
		BeforeTestFunc: func(t testing.TB, app *tests.TestApp, e *core.ServeEvent) {
			config.SaveConfigOnApp(app, cfg)
			_ = db.EnsureSchema(app)
			setupCustomTheme(app)
			theme, _ := db.GetActiveThemeSchema(app)
			cfg.UpdateActiveTheme(theme)
			RegisterRoutes(e, testFS())
		},
		ExpectedStatus:  http.StatusOK,
		ExpectedContent: []string{"console.log('Neon JS');"},
		AfterTestFunc: func(t testing.TB, app *tests.TestApp, res *http.Response) {
			if h := res.Header.Get("Cache-Control"); h != "public, max-age=300" {
				t.Errorf("expected Cache-Control 'public, max-age=300', got %q", h)
			}
			if h := res.Header.Get("Content-Type"); h != "text/javascript" {
				t.Errorf("expected Content-Type 'text/javascript', got %q", h)
			}
		},
	}
	scenarioJS.Test(t)
}

func TestThemeAPI_ThemeDeactivationAndDeletionRevertsToDefault(t *testing.T) {
	app, err := tests.NewTestApp()
	if err != nil {
		t.Fatalf("failed to create test app: %v", err)
	}
	defer app.Cleanup()

	cfg := testConfig()
	config.SaveConfigOnApp(app, cfg)
	if err := db.EnsureSchema(app); err != nil {
		t.Fatalf("failed to ensure schema: %v", err)
	}

	// Create custom theme record
	themeRec := &db.Theme{
		ThemeSchema: config.ThemeSchema{
			Name:     "Temporary Theme",
			IsActive: true,
			SiteName: "Temp Site",
		},
	}
	rec, err := themeRec.ToRecord(app)
	if err != nil {
		t.Fatalf("failed to build record: %v", err)
	}
	themeRec.ID = rec.Id

	app.OnRecordDelete().Bind(&hook.Handler[*core.RecordEvent]{
		Func: db.RegisterCollectionHooks(app, db.CollectionHookMap{
			config.DefaultThemesCollection: db.OnThemeDelete,
		}),
	})

	// Save initial active theme to DB and cache
	if err := app.Save(rec); err != nil {
		t.Fatalf("failed to save record: %v", err)
	}
	if err := themeRec.RefreshFromRecord(rec); err != nil {
		t.Fatalf("failed to refresh theme from record: %v", err)
	}
	cfg.UpdateActiveTheme(&themeRec.ThemeSchema)
	appTheme, err := config.GetActiveThemeFromApp(app)
	if err != nil {
		t.Fatalf("failed to get active theme from app: %v", err)
	}
	if appTheme.Name != "Temporary Theme" {
		t.Fatalf("expected active theme in cache")
	}

	// Deleting the active theme should trigger OnThemeDelete hook and revert cache to default theme
	if err := app.Delete(rec); err != nil {
		t.Fatalf("failed to delete theme record: %v", err)
	}

	updatedAppTheme, err := config.GetActiveThemeFromApp(app)
	if err != nil {
		t.Fatalf("failed to get active theme from app: %v", err)
	}

	if updatedAppTheme.ID != "default" || updatedAppTheme.Name != "Default" {
		t.Fatalf("expected cache to revert to default theme after deletion, got %v", updatedAppTheme)
	}
}

func testFS() fs.FS {
	return fstest.MapFS{
		"internal/dashboard/dist/index.html": &fstest.MapFile{
			Data: []byte("<html></html>"),
		},
	}
}

// seedForExclusivity returns an app with one QPU and a driver factory bound to it.
func seedForExclusivity(t *testing.T) (*tests.TestApp, *config.AppConfig, func(name, kind, status string) *db.Driver) {
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

	return app, cfg, func(name, kind, status string) *db.Driver {
		t.Helper()
		driver := &db.Driver{
			Name:     name,
			QPU:      qpu.ID,
			Kind:     kind,
			Language: "python",
			Token:    db.HashToken("tok-" + name),
			Status:   status,
			Enabled:  true,
		}
		if err := saveToDb(app, driver); err != nil {
			t.Fatalf("failed to create driver %s: %v", name, err)
		}
		return driver
	}
}

// dispatching registers driverID as served by this process, as a connect would,
// and unregisters it when the test ends.
func dispatching(t *testing.T, driverID string) {
	t.Helper()
	activeDriversMu.Lock()
	activeDrivers[driverID] = func() {}
	activeDriversMu.Unlock()
	t.Cleanup(func() { StopDriverDistribution(driverID) })
}

func TestConnectedPeer_RefusesASecondTunerOnOneQPU(t *testing.T) {
	app, cfg, newDriver := seedForExclusivity(t)

	live := newDriver("tuner-1", string(drivers.QuantifyTuner), "online")
	dispatching(t, live.ID)
	second := newDriver("tuner-2", string(drivers.QuantifyTuner), "offline")

	peer, operation := connectedPeer(app, cfg, second)
	if peer != "tuner-1" {
		t.Errorf("expected tuner-1 to block the connection, got %q", peer)
	}
	if operation != drivers.Calibrate {
		t.Errorf("expected the shared operation to be calibrate, got %q", operation)
	}
}

// Both tuners calibrate the same chip, so the rule is one per operation, not one
// per kind.
func TestConnectedPeer_RefusesTheOtherSchedulersTuner(t *testing.T) {
	app, cfg, newDriver := seedForExclusivity(t)

	live := newDriver("quantify-tuner", string(drivers.QuantifyTuner), "online")
	dispatching(t, live.ID)
	second := newDriver("qblox-tuner", string(drivers.QbloxTuner), "offline")

	if peer, _ := connectedPeer(app, cfg, second); peer != "quantify-tuner" {
		t.Errorf("expected the running tuner to block the other scheduler's, got %q", peer)
	}
}

func TestConnectedPeer_RefusesASecondQPUDriver(t *testing.T) {
	app, cfg, newDriver := seedForExclusivity(t)

	live := newDriver("qpu-1", string(drivers.Quantify), "online")
	dispatching(t, live.ID)
	second := newDriver("qpu-2", string(drivers.Qblox), "offline")

	peer, operation := connectedPeer(app, cfg, second)
	if peer != "qpu-1" {
		t.Errorf("expected qpu-1 to block the connection, got %q", peer)
	}
	if operation != drivers.Process {
		t.Errorf("expected the shared operation to be process, got %q", operation)
	}
}

// A tuner and a QPU driver are the arrangement RFC 0004 §8 describes, not a clash.
func TestConnectedPeer_AllowsADifferentOperation(t *testing.T) {
	app, cfg, newDriver := seedForExclusivity(t)

	live := newDriver("qpu-1", string(drivers.Quantify), "online")
	dispatching(t, live.ID)
	tuner := newDriver("tuner-1", string(drivers.QuantifyTuner), "offline")

	if peer, _ := connectedPeer(app, cfg, tuner); peer != "" {
		t.Errorf("a tuner beside a QPU driver is the intended setup, got blocked by %q", peer)
	}
}

// A standby may be registered; only connecting it while the first is live is refused.
func TestConnectedPeer_AllowsAnOfflinePeer(t *testing.T) {
	app, cfg, newDriver := seedForExclusivity(t)

	newDriver("tuner-1", string(drivers.QuantifyTuner), "offline")
	second := newDriver("tuner-2", string(drivers.QuantifyTuner), "offline")

	if peer, _ := connectedPeer(app, cfg, second); peer != "" {
		t.Errorf("an offline peer must not block a connection, got %q", peer)
	}
}

// The crash case: the process died so the socket detached, but the record's own
// goroutines are still registered. A replacement must be able to take over.
func TestConnectedPeer_AllowsAPeerWhoseSocketDetached(t *testing.T) {
	app, cfg, newDriver := seedForExclusivity(t)

	dead := newDriver("tuner-1", string(drivers.QuantifyTuner), "offline")
	dispatching(t, dead.ID)
	second := newDriver("tuner-2", string(drivers.QuantifyTuner), "offline")

	if peer, _ := connectedPeer(app, cfg, second); peer != "" {
		t.Errorf("a detached peer must not block a replacement, got %q", peer)
	}
}

// The restart case: the database still says online from before the restart, but
// this process dispatches to nothing. A stale row must not wedge the QPU.
func TestConnectedPeer_AllowsAStaleOnlinePeerAfterARestart(t *testing.T) {
	app, cfg, newDriver := seedForExclusivity(t)

	newDriver("tuner-1", string(drivers.QuantifyTuner), "online")
	second := newDriver("tuner-2", string(drivers.QuantifyTuner), "offline")

	if peer, _ := connectedPeer(app, cfg, second); peer != "" {
		t.Errorf("a stale online row must not block a connection, got %q", peer)
	}
}

func TestConnectedPeer_AllowsADriverReconnectingAsItself(t *testing.T) {
	app, cfg, newDriver := seedForExclusivity(t)

	live := newDriver("tuner-1", string(drivers.QuantifyTuner), "online")
	dispatching(t, live.ID)

	if peer, _ := connectedPeer(app, cfg, live); peer != "" {
		t.Errorf("a driver must not block its own reconnect, got %q", peer)
	}
}

// A custom driver's operation is whatever its author wrote, so the server has no
// grounds to call two of them the same role.
func TestConnectedPeer_DoesNotPoliceCustomDrivers(t *testing.T) {
	app, cfg, newDriver := seedForExclusivity(t)

	live := newDriver("custom-1", string(drivers.Custom), "online")
	dispatching(t, live.ID)
	second := newDriver("custom-2", string(drivers.Custom), "offline")

	if peer, _ := connectedPeer(app, cfg, second); peer != "" {
		t.Errorf("custom drivers are not held to one per QPU, got %q", peer)
	}
}

func TestConnectedPeer_IgnoresAnotherQPUsDriver(t *testing.T) {
	app, cfg, newDriver := seedForExclusivity(t)

	otherQPU := db.QPU{Name: "qpu_2", Status: "online", Enabled: true}
	if err := saveToDb(app, &otherQPU); err != nil {
		t.Fatalf("failed to create the second qpu: %v", err)
	}
	elsewhere := &db.Driver{
		Name: "tuner-elsewhere", QPU: otherQPU.ID,
		Kind: string(drivers.QuantifyTuner), Language: "python",
		Token: db.HashToken("tok-elsewhere"), Status: "online", Enabled: true,
	}
	if err := saveToDb(app, elsewhere); err != nil {
		t.Fatalf("failed to create the second tuner: %v", err)
	}
	dispatching(t, elsewhere.ID)

	mine := newDriver("tuner-1", string(drivers.QuantifyTuner), "offline")

	if peer, _ := connectedPeer(app, cfg, mine); peer != "" {
		t.Errorf("another QPU's tuner is irrelevant, got blocked by %q", peer)
	}
}

// TestQPUAvailabilityAPI_NamesTheReason proves the reason reaches a caller from the
// server, rather than being rebuilt from `status` by each of them.
func TestQPUAvailabilityAPI_NamesTheReason(t *testing.T) {
	scenario := tests.ApiScenario{
		Name:   "GET /api/qpus/{name}/availability names the reason",
		Method: http.MethodGet,
		URL:    "/api/qpus/qpu_shut/availability",
		BeforeTestFunc: func(t testing.TB, app *tests.TestApp, e *core.ServeEvent) {
			cfg := testConfig()
			config.SaveConfigOnApp(app, cfg)
			_ = db.EnsureSchema(app)
			_ = saveToDb(app, &db.QPU{Name: "qpu_shut", Status: "maintenance", Enabled: true})
			RegisterRoutes(e, testFS())
		},
		ExpectedStatus: http.StatusOK,
		ExpectedContent: []string{
			`"available":false`,
			`"reason":"this QPU is under maintenance"`,
		},
	}
	scenario.Test(t)
}

func TestQPUAvailabilityAPI_SaysAvailableWhenItIs(t *testing.T) {
	scenario := tests.ApiScenario{
		Name:   "GET /api/qpus/{name}/availability on a QPU taking jobs",
		Method: http.MethodGet,
		URL:    "/api/qpus/qpu_open/availability",
		BeforeTestFunc: func(t testing.TB, app *tests.TestApp, e *core.ServeEvent) {
			cfg := testConfig()
			config.SaveConfigOnApp(app, cfg)
			_ = db.EnsureSchema(app)
			_ = saveToDb(app, &db.QPU{Name: "qpu_open", Status: "online", Enabled: true})
			RegisterRoutes(e, testFS())
		},
		ExpectedStatus:     http.StatusOK,
		ExpectedContent:    []string{`"available":true`},
		NotExpectedContent: []string{`"reason"`},
	}
	scenario.Test(t)
}

func TestQPUAvailabilityAPI_IsNotFoundForAnUnknownQPU(t *testing.T) {
	scenario := tests.ApiScenario{
		Name:   "GET /api/qpus/{name}/availability for a QPU that does not exist",
		Method: http.MethodGet,
		URL:    "/api/qpus/no_such_qpu/availability",
		BeforeTestFunc: func(t testing.TB, app *tests.TestApp, e *core.ServeEvent) {
			cfg := testConfig()
			config.SaveConfigOnApp(app, cfg)
			_ = db.EnsureSchema(app)
			RegisterRoutes(e, testFS())
		},
		ExpectedStatus:  http.StatusNotFound,
		ExpectedContent: []string{"QPU not found"},
	}
	scenario.Test(t)
}

// TestQPUAvailabilityAPI_ListDoesNotCollideWithTheNameRoute is the reason this
// route can exist at all: /api/qpus/availability sits under the same prefix as
// /api/qpus/{name}, so the router has to prefer the literal.
func TestQPUAvailabilityAPI_ListDoesNotCollideWithTheNameRoute(t *testing.T) {
	seed := func(t testing.TB, app *tests.TestApp, e *core.ServeEvent) {
		cfg := testConfig()
		config.SaveConfigOnApp(app, cfg)
		_ = db.EnsureSchema(app)
		_ = saveToDb(app, &db.QPU{Name: "qpu_open", Status: "online", Enabled: true})
		_ = saveToDb(app, &db.QPU{Name: "qpu_shut", Status: "maintenance", Enabled: true})
		RegisterRoutes(e, testFS())
	}

	list := tests.ApiScenario{
		Name:            "GET /api/qpus/availability returns every QPU",
		Method:          http.MethodGet,
		URL:             "/api/qpus/availability",
		BeforeTestFunc:  seed,
		ExpectedStatus:  http.StatusOK,
		ExpectedContent: []string{`"name":"qpu_open"`, `"name":"qpu_shut"`, `"reason":"this QPU is under maintenance"`},
	}
	list.Test(t)

	// And the wildcard route still answers for a real name.
	one := tests.ApiScenario{
		Name:            "GET /api/qpus/{name} still resolves",
		Method:          http.MethodGet,
		URL:             "/api/qpus/qpu_shut/availability",
		BeforeTestFunc:  seed,
		ExpectedStatus:  http.StatusOK,
		ExpectedContent: []string{`"name":"qpu_shut"`},
	}
	one.Test(t)
}

// testConfig builds an AppConfig with the default collection names and the
// driver framework enabled, mirroring handleDriverCreate/Connect's expectations.
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
		PortRangeStart:                6100,
		PortRangeEnd:                  6200,
	}
}

// TestDriverRegisterAndConnect proves the core Phase 1 flow end to end at the
// data layer: a driver is created against a QPU with a hashed token, is found
// by that hash the way handleDriverConnect looks it up, and gets free,
// non-colliding NNG ports allocated the same way handleQPUConnect does for
// QPUs (RFC 0001 §7, §8).
func TestDriverRegisterAndConnect(t *testing.T) {
	app, err := tests.NewTestApp()
	if err != nil {
		t.Fatalf("failed to create test app: %v", err)
	}
	defer app.Cleanup()

	cfg := testConfig()
	config.SaveConfigOnApp(app, cfg)
	if err := db.EnsureSchema(app); err != nil {
		t.Fatalf("failed to ensure schema: %v", err)
	}

	qpu := db.QPU{
		Name:    "qpu_1",
		Status:  "offline",
		Enabled: true,
	}
	if err := saveToDb(app, &qpu); err != nil {
		t.Fatalf("failed to create qpu: %v", err)
	}

	// A driver registers: it belongs to the QPU above and its token is
	// stored hashed, exactly as OnDriverCreate would leave it.
	rawToken := "raw_driver_token"
	driver := db.Driver{
		Name:     "driver_1",
		QPU:      qpu.ID,
		Kind:     "mock",
		Language: "python",
		Events:   []string{"JobDispatch", "JobResult"},
		Token:    db.HashToken(rawToken),
		Status:   "offline",
		Enabled:  true,
	}
	if err := saveToDb(app, &driver); err != nil {
		t.Fatalf("failed to create driver: %v", err)
	}
	if driver.QPU != qpu.ID {
		t.Errorf("driver.QPU = %q, want %q", driver.QPU, qpu.ID)
	}

	// The driver connects: handleDriverConnect hashes the presented token and
	// looks the driver up by that hash.
	hashedToken := db.HashToken(rawToken)
	var found db.Driver
	if err := db.FindOneByFilter(app, cfg.CollectionDrivers, &found, "token = {:token}", dbx.Params{"token": hashedToken}); err != nil {
		t.Fatalf("expected to find driver by hashed token: %v", err)
	}
	if found.ID != driver.ID {
		t.Errorf("found driver %q, want %q", found.ID, driver.ID)
	}

	ports, err := findFreePorts(app, 2)
	if err != nil {
		t.Fatalf("findFreePorts: %v", err)
	}
	if len(ports) != 2 {
		t.Fatalf("expected 2 ports, got %v", ports)
	}
	if ports[0] == ports[1] {
		t.Errorf("expected distinct ports, got %v", ports)
	}
	for _, p := range ports {
		if p < cfg.PortRangeStart || p >= cfg.PortRangeEnd {
			t.Errorf("port %d outside configured range [%d, %d)", p, cfg.PortRangeStart, cfg.PortRangeEnd)
		}
	}

	found.NNGInPort = ports[0]
	found.NNGOutPort = ports[1]
	if err := saveToDb(app, &found); err != nil {
		t.Fatalf("failed to save allocated ports: %v", err)
	}

	// A second allocation must not collide with the ports just claimed.
	morePorts, err := findFreePorts(app, 2)
	if err != nil {
		t.Fatalf("findFreePorts (second call): %v", err)
	}
	for _, p := range morePorts {
		if p == ports[0] || p == ports[1] {
			t.Errorf("second allocation %v reused an already-claimed port from %v", morePorts, ports)
		}
	}
}

// TestDriverCreate_DisabledDriverCannotConnect proves a disabled driver's
// token still hashes and stores, but is treated as unusable — mirroring
// handleDriverConnect's Enabled check (RFC 0001 §8).
func TestDriverCreate_DisabledDriverCannotConnect(t *testing.T) {
	app, err := tests.NewTestApp()
	if err != nil {
		t.Fatalf("failed to create test app: %v", err)
	}
	defer app.Cleanup()

	cfg := testConfig()
	config.SaveConfigOnApp(app, cfg)
	if err := db.EnsureSchema(app); err != nil {
		t.Fatalf("failed to ensure schema: %v", err)
	}

	qpu := db.QPU{Name: "qpu_2", Status: "offline", Enabled: true}
	if err := saveToDb(app, &qpu); err != nil {
		t.Fatalf("failed to create qpu: %v", err)
	}

	rawToken := "raw_disabled_token"
	driver := db.Driver{
		Name:     "driver_disabled",
		QPU:      qpu.ID,
		Kind:     "custom",
		Language: "go",
		Events:   []string{"JobDispatch"},
		Token:    db.HashToken(rawToken),
		Status:   "offline",
		Enabled:  false,
	}
	if err := saveToDb(app, &driver); err != nil {
		t.Fatalf("failed to create driver: %v", err)
	}

	var found db.Driver
	if err := db.FindOneByFilter(app, cfg.CollectionDrivers, &found, "token = {:token}", dbx.Params{"token": db.HashToken(rawToken)}); err != nil {
		t.Fatalf("expected to find the disabled driver by hashed token: %v", err)
	}
	if found.Enabled {
		t.Errorf("expected driver to be disabled")
	}
}

// TestDriverCreate_EveryCatalogKindSaves: handleDriverCreate gates on
// `drivers.Default`, and only the insert gates on the `kind` column.
func TestDriverCreate_EveryCatalogKindSaves(t *testing.T) {
	app, err := tests.NewTestApp()
	if err != nil {
		t.Fatalf("failed to create test app: %v", err)
	}
	defer app.Cleanup()

	config.SaveConfigOnApp(app, testConfig())
	if err := db.EnsureSchema(app); err != nil {
		t.Fatalf("failed to ensure schema: %v", err)
	}

	qpu := db.QPU{Name: "qpu_catalog", Status: "offline", Enabled: true}
	if err := saveToDb(app, &qpu); err != nil {
		t.Fatalf("failed to create qpu: %v", err)
	}

	for _, kind := range append(drivers.Default.Kinds(), drivers.Custom) {
		for _, language := range []drivers.Language{drivers.Python, drivers.TypeScript, drivers.Go} {
			if !drivers.Default.ShipsIn(kind, language) {
				continue
			}
			name := fmt.Sprintf("driver_%s_%s", kind, language)
			driver := db.Driver{
				Name:     name,
				QPU:      qpu.ID,
				Kind:     string(kind),
				Language: string(language),
				Events:   drivers.Default.Events(kind),
				Token:    db.HashToken(name),
				Status:   "offline",
				Enabled:  true,
			}
			if err := saveToDb(app, &driver); err != nil {
				t.Errorf("kind=%s language=%s: %v", kind, language, err)
			}
		}
	}
}
