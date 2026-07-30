package api

import (
	"strings"
	"testing"

	"qpi/internal/config"
	"qpi/internal/db"
	"qpi/internal/drivers"

	"github.com/pocketbase/dbx"
	"github.com/pocketbase/pocketbase/tests"
)

// testConfig builds an AppConfig with the default collection names and the
// driver framework enabled, mirroring handleDriverCreate/Connect's expectations.
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
		PortRangeStart:               6100,
		PortRangeEnd:                 6200,
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

// TestJoinKindsNamesWhatIsAvailable covers the sentence handleDriverCreate hands an
// operator whose kind×language pairing does not exist. "it ships" followed by
// nothing would be the worst version of this message, so the empty case says so.
func TestJoinKindsNamesWhatIsAvailable(t *testing.T) {
	if got := joinKinds(nil); got != "no built-in devices" {
		t.Errorf("joinKinds(nil) = %q, want a sentence about there being none", got)
	}
	if got := joinKinds(drivers.Default.KindsIn(drivers.Go)); got != `"bluefors_gen1"` {
		t.Errorf("joinKinds(KindsIn(go)) = %q, want just the quoted monitor", got)
	}
	got := joinKinds(drivers.Default.KindsIn(drivers.Python))
	for _, want := range []string{`"mock"`, `"qblox"`, `"bluefors_gen1"`} {
		if !strings.Contains(got, want) {
			t.Errorf("joinKinds(KindsIn(python)) = %q, want it to contain %s", got, want)
		}
	}
}
