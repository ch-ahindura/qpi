package api

import (
	"testing"

	"github.com/pocketbase/pocketbase/tests"

	"qpi/internal/config"
	"qpi/internal/db"
	"qpi/internal/drivers"
)

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
