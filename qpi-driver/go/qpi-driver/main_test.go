package main

import (
	"testing"

	"github.com/sopherapps/qpi/qpi-driver/go/devices"
)

// This command is two steps — register, hand off — so there are two things to
// check: that what it registers is registrable, and that the officially
// maintained device is among it. Everything else about the CLI belongs to the
// importable cli package and is tested there.

func TestBuiltinDevicesShipTheBlueforsMonitor(t *testing.T) {
	names := devices.DeviceNames(builtinDevices())
	if len(names) != 1 || names[0] != "bluefors_gen1" {
		t.Fatalf("expected the bluefors monitor to ship, got %v", names)
	}
}

func TestBuiltinDevicesAreRegistrable(t *testing.T) {
	// Registering is fallible — a spec with no builder, an invented operation, a
	// name already taken — and main treats a failure as fatal, so a broken spec
	// would mean a binary that cannot start rather than a test failure here.
	registry := devices.NewRegistry()
	for _, spec := range builtinDevices() {
		if err := registry.Register(spec); err != nil {
			t.Fatalf("expected %q to register, got %v", spec.Name, err)
		}
		if _, err := registry.Resolve(spec.Operation, spec.Name); err != nil {
			t.Errorf("expected %q to resolve after registering, got %v", spec.Name, err)
		}
	}
}
