package devices_test

import (
	"reflect"
	"strings"
	"testing"
	"time"

	qpidriver "github.com/sopherapps/qpi/qpi-driver/go"
	"github.com/sopherapps/qpi/qpi-driver/go/devices"
)

func fieldNames(value any) []string {
	t := reflect.TypeOf(value)
	names := make([]string, 0, t.NumField())
	for i := 0; i < t.NumField(); i++ {
		names = append(names, t.Field(i).Name)
	}
	return names
}

// fakeDriver stands in for a real device's driver. Embedding Base is not optional:
// [qpidriver.Driver] is sealed by an unexported method, so a builder can only
// return something the SDK can reach the transport of.
type fakeDriver struct {
	qpidriver.Base
	probes int
	label  string
	fast   bool
}

func (d *fakeDriver) HandleEvent(qpidriver.Event) {}

// fakeSpec is what a downstream module writes: a name, an operation and a builder
// that reads the options it understands and returns a driver rather than running it.
func fakeSpec(name string, operation devices.Operation) devices.DeviceSpec {
	return devices.DeviceSpec{
		Name:      name,
		Operation: operation,
		Build: func(_ qpidriver.Config, opts *devices.Options) (qpidriver.Driver, error) {
			driver := &fakeDriver{
				probes: opts.Int("probes", 1),
				label:  opts.String("label", ""),
				fast:   opts.Bool("fast", false),
			}
			if err := opts.Err(); err != nil {
				return nil, err
			}
			return driver, nil
		},
	}
}

func TestOperationsAreAClosedPair(t *testing.T) {
	// Devices are the extensible half; operations are not, so asserting the whole
	// set is the point here rather than a brittleness (RFC 0003 §13.1).
	names := devices.OperationNames()
	if len(names) != 2 || names[0] != "process" || names[1] != "monitor" {
		t.Fatalf("expected exactly process and monitor, got %v", names)
	}
	if devices.KnownOperation(devices.Operation("telemetry")) {
		t.Error("expected an invented operation not to be known")
	}
}

func TestOperationsCannotBeMutatedThroughTheAccessor(t *testing.T) {
	// Operations() hands out a copy, so a caller cannot quietly rename one.
	first := devices.Operations()
	first[0] = devices.Operation("tampered")
	if devices.Operations()[0] == devices.Operation("tampered") {
		t.Error("expected Operations() to return a copy")
	}
}

// TestADownstreamModuleAddsADevice is the extension story of the Go SDK, which has
// no runtime import by name: a build of your own registers a spec and gets the same
// resolution the built-in devices get (RFC 0003 §6, §8).
func TestADownstreamModuleAddsADevice(t *testing.T) {
	registry := devices.NewRegistry(fakeSpec("mylab_qpu", devices.Process))

	spec, err := registry.Resolve(devices.Process, "mylab_qpu")
	if err != nil {
		t.Fatalf("expected the registered device to resolve, got %v", err)
	}

	opts := devices.NewOptions(map[string]string{"probes": "4", "label": "cryo"})
	driver, err := spec.Build(qpidriver.Config{Token: "tok"}, opts)
	if err != nil {
		t.Fatalf("expected the builder to succeed, got %v", err)
	}

	built, ok := driver.(*fakeDriver)
	if !ok {
		t.Fatalf("expected the device's own driver, got %T", driver)
	}
	if built.probes != 4 || built.label != "cryo" {
		t.Errorf("expected the options to reach the builder, got %+v", built)
	}
	// A fallback the builder stated itself, rather than a schema's declared default.
	if built.fast {
		t.Error("expected fast to fall back to false")
	}
	if len(opts.Unread()) != 0 {
		t.Errorf("expected every given option read, got %v", opts.Unread())
	}
}

func TestADeviceSpecIsANameAnOperationAndABuilder(t *testing.T) {
	// A device that also described itself — a summary, a declared option schema, an
	// install target — would be a second catalog beside QPI-UI's, kept in step by
	// hand (RFC 0003 §9). Three fields is the whole of it.
	if got := fieldNames(devices.DeviceSpec{}); len(got) != 3 {
		t.Errorf("expected exactly Name, Operation and Build, got %v", got)
	}
}

func TestRegisterRejectsADuplicateName(t *testing.T) {
	// Two devices of one name would make the winner depend on registration order.
	registry := devices.NewRegistry(fakeSpec("mine", devices.Monitor))

	if err := registry.Register(fakeSpec("mine", devices.Monitor)); err == nil {
		t.Fatal("expected a duplicate name to be refused")
	} else if !strings.Contains(err.Error(), "already registered") {
		t.Errorf("expected the reason given, got %q", err)
	}
}

func TestRegisterScopesNamesPerOperation(t *testing.T) {
	// --device is always read in the context of an operation, so nothing needs to
	// stop a process and a monitor device sharing a name.
	registry := devices.NewRegistry(
		fakeSpec("shared", devices.Process),
		fakeSpec("shared", devices.Monitor),
	)

	for _, operation := range []devices.Operation{devices.Process, devices.Monitor} {
		spec, err := registry.Resolve(operation, "shared")
		if err != nil {
			t.Fatalf("expected %s/shared to resolve, got %v", operation, err)
		}
		if spec.Operation != operation {
			t.Errorf("expected the %s spec, got %s", operation, spec.Operation)
		}
	}
}

func TestRegisterRefusesAnIncompleteOrUnknownSpec(t *testing.T) {
	registry := devices.NewRegistry()

	if err := registry.Register(devices.DeviceSpec{Operation: devices.Monitor}); err == nil {
		t.Error("expected a spec with no name or builder to be refused")
	}
	if err := registry.Register(devices.DeviceSpec{
		Name:      "x",
		Operation: devices.Operation("telemetry"),
		Build: func(qpidriver.Config, *devices.Options) (qpidriver.Driver, error) {
			return nil, nil
		},
	}); err == nil {
		t.Error("expected an invented operation to be refused")
	}
}

func TestDevicesAreSortedByName(t *testing.T) {
	// Sorted rather than registration-ordered, so `devices` output is stable.
	registry := devices.NewRegistry(
		fakeSpec("zeta", devices.Monitor),
		fakeSpec("alpha", devices.Monitor),
		fakeSpec("mu", devices.Monitor),
	)

	got := devices.DeviceNames(registry.Devices(devices.Monitor))
	want := []string{"alpha", "mu", "zeta"}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("expected %v, got %v", want, got)
		}
	}
}

func TestResolveReportsTheKnownDevices(t *testing.T) {
	registry := devices.NewRegistry(
		fakeSpec("mu", devices.Monitor),
		fakeSpec("alpha", devices.Monitor),
	)

	_, err := registry.Resolve(devices.Monitor, "mockk")
	if err == nil {
		t.Fatal("expected an unknown device to fail")
	}
	if !strings.Contains(err.Error(), `unknown monitor device "mockk"`) {
		t.Errorf("expected the device named, got %q", err)
	}
	if !strings.Contains(err.Error(), "known devices: alpha, mu") {
		t.Errorf("expected the alternatives listed and sorted, got %q", err)
	}
}

func TestResolveSaysWhenAnOperationHasNoDevices(t *testing.T) {
	// Not "unknown device"; there is no device to be unknown. The message has to
	// send the operator somewhere useful (RFC 0003 §8).
	registry := devices.NewRegistry(fakeSpec("mine", devices.Monitor))

	_, err := registry.Resolve(devices.Process, "mock")
	if err == nil {
		t.Fatal("expected an empty operation to fail")
	}
	if !strings.Contains(err.Error(), "ships no process devices") {
		t.Errorf("expected an honest message, got %q", err)
	}
	if strings.Contains(err.Error(), "known devices") {
		t.Errorf("expected no empty device list, got %q", err)
	}
}

func TestResolveRefusesAnUnknownOperation(t *testing.T) {
	if _, err := devices.NewRegistry().Resolve(devices.Operation("telemetry"), "x"); err == nil {
		t.Error("expected an invented operation to be refused")
	}
}

func TestOptionsFallBackWhenAKeyIsAbsent(t *testing.T) {
	// Every accessor takes the fallback, so a device states its default once, in the
	// one piece of code that acts on it.
	opts := devices.NewOptions(nil)

	if opts.String("base_url", "http://localhost") != "http://localhost" ||
		opts.Int("probes", 3) != 3 || opts.Float("interval", 2.5) != 2.5 ||
		!opts.Bool("fast", true) || opts.Seconds("timeout", 5*time.Second) != 5*time.Second {
		t.Error("expected each accessor to return its fallback")
	}
	if err := opts.Err(); err != nil {
		t.Errorf("expected no error from absent options, got %v", err)
	}
}

func TestOptionsReadAValueAsTheAccessorSays(t *testing.T) {
	opts := devices.NewOptions(map[string]string{
		"probes": "9", "interval": "0.5", "fast": "on", "timeout": "2.5",
	})

	if opts.Int("probes", 3) != 9 || opts.Float("interval", 5) != 0.5 ||
		!opts.Bool("fast", false) || opts.Seconds("timeout", 0) != 2500*time.Millisecond {
		t.Error("expected each accessor to read the given value")
	}
}

func TestOptionsBooleansMatchThePythonSpellings(t *testing.T) {
	for raw, want := range map[string]bool{
		"1": true, "true": true, "TRUE": true, " yes ": true, "On": true,
		"0": false, "false": false, "no": false, "off": false, "": false, "maybe": false,
	} {
		got := devices.NewOptions(map[string]string{"k": raw}).Bool("k", true)
		if got != want {
			t.Errorf("expected Bool(%q) == %v, got %v", raw, want, got)
		}
	}
}

func TestOptionsRecordTheFirstBadValueAndNameIt(t *testing.T) {
	// A builder reads its options as a flat run of statements and checks Err once,
	// so the error has to carry which option it was about.
	opts := devices.NewOptions(map[string]string{"probes": "many", "interval": "soon"})

	opts.Int("probes", 1)
	opts.Float("interval", 1)

	err := opts.Err()
	if err == nil {
		t.Fatal("expected a bad value to be recorded")
	}
	if !strings.Contains(err.Error(), "bad value for -o probes") {
		t.Errorf("expected the first failure, named, got %q", err)
	}
}

func TestOptionsRequireReportsTheLineThatWasMissing(t *testing.T) {
	opts := devices.NewOptions(nil)

	opts.Require("channels", "a:K")

	err := opts.Err()
	if err == nil || !strings.Contains(err.Error(), `missing required option "channels", e.g. -o channels=a:K`) {
		t.Errorf("expected the -o pair that should have been typed, got %v", err)
	}
}

func TestOptionsUnreadReportsWhatNothingLookedAt(t *testing.T) {
	// The device's own code is the schema, so a key it never read is a typo.
	opts := devices.NewOptions(map[string]string{
		"probes": "9", "probez": "9", "elephant": "1",
	})
	opts.Int("probes", 1)

	got := opts.Unread()
	if len(got) != 2 || got[0] != "elephant" || got[1] != "probez" {
		t.Errorf("expected the unread keys, sorted, got %v", got)
	}
}

func TestOptionsReadingAnAbsentKeyStillCountsAsAReading(t *testing.T) {
	// Asking about a key is what says the device understands it, given or not.
	opts := devices.NewOptions(map[string]string{"probes": "9"})
	opts.String("label", "")
	opts.Int("probes", 1)

	if len(opts.Unread()) != 0 {
		t.Errorf("expected nothing unread, got %v", opts.Unread())
	}
}

func TestOptionsRemainingHandsOverEverythingLeft(t *testing.T) {
	opts := devices.NewOptions(map[string]string{"probes": "9", "qubits": "5"})
	opts.Int("probes", 1)

	rest := opts.Remaining()
	if len(rest) != 1 || rest["qubits"] != "5" {
		t.Errorf("expected the unread options as typed, got %v", rest)
	}
	if len(opts.Unread()) != 0 {
		t.Errorf("expected Remaining to mark them read, got %v", opts.Unread())
	}
}

func TestOptionsCopyWhatTheyAreGiven(t *testing.T) {
	// A caller mutating its own map afterwards must not change what a driver reads.
	raw := map[string]string{"probes": "9"}
	opts := devices.NewOptions(raw)
	raw["probes"] = "1"

	if opts.Int("probes", 0) != 9 {
		t.Error("expected NewOptions to copy its input")
	}
}

// The package-level functions delegate to [devices.Default]. They are what a
// downstream main calls, so they get their own coverage rather than only the method
// form — and the shipped CLI's registry is that global, so this is also the only
// place it is exercised directly.
func TestThePackageLevelFunctionsUseTheDefaultRegistry(t *testing.T) {
	spec := fakeSpec("package_level_probe", devices.Monitor)

	if err := devices.Register(spec); err != nil {
		t.Fatalf("expected the device to register into Default, got %v", err)
	}
	t.Cleanup(func() {
		// Default is process-global; leaving a test device in it would change what
		// every later test sees.
		*devices.Default = *devices.NewRegistry()
	})

	found := false
	for _, each := range devices.Devices(devices.Monitor) {
		if each.Name == spec.Name {
			found = true
		}
	}
	if !found {
		t.Error("expected Devices() to list what Register() added")
	}
	if _, err := devices.Resolve(devices.Monitor, spec.Name); err != nil {
		t.Errorf("expected Resolve() to find it, got %v", err)
	}
	if err := devices.Register(spec); err == nil {
		t.Error("expected a duplicate to be refused through the package function too")
	}
}

func TestNewRegistryPanicsOnADuplicate(t *testing.T) {
	// NewRegistry is for assembly, where a duplicate can only be a programming
	// error; Register is the recoverable form.
	defer func() {
		if recover() == nil {
			t.Error("expected NewRegistry to panic on a duplicate")
		}
	}()
	devices.NewRegistry(
		fakeSpec("same", devices.Monitor),
		fakeSpec("same", devices.Monitor),
	)
}
