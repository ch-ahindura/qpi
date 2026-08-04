package api

import (
	"testing"

	"github.com/pocketbase/pocketbase/tests"

	"qpi/internal/config"
	"qpi/internal/db"
	"qpi/internal/drivers"
)

// A driver holds a lease — two NNG ports and two goroutines — from the moment it
// connects until it disconnects, is disabled, or is deleted. These tests pin the
// release paths; before them, only disabling released anything.

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
