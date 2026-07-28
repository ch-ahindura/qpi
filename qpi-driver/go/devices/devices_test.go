package devices_test

import (
	"encoding/json"
	"strings"
	"testing"
	"time"

	qpidriver "github.com/sopherapps/qpi/qpi-driver/go"
	"github.com/sopherapps/qpi/qpi-driver/go/devices"
)

// fakeDriver stands in for a real device's driver. Embedding Base is not optional:
// [qpidriver.Driver] is sealed by an unexported method, so a builder can only
// return something the SDK can reach the transport of.
type fakeDriver struct {
	qpidriver.Base
	opts devices.Options
}

func (d *fakeDriver) HandleEvent(qpidriver.Event) {}

// fakeSpec is what a downstream module writes: a spec describing its own device,
// with its own options, and a builder returning a driver rather than running one.
func fakeSpec(name string, operation devices.Operation) devices.DeviceSpec {
	return devices.DeviceSpec{
		Name:      name,
		Operation: operation,
		Summary:   "A device from outside the SDK.",
		Options: []devices.OptionSpec{
			{Key: "probes", Help: "How many.", Type: "int", Parse: devices.AsInt, Default: "1"},
			{Key: "label", Help: "What to call it.", Type: "str"},
			{Key: "fast", Help: "Whether to hurry.", Type: "bool", Parse: devices.AsBool, Default: "no"},
		},
		Build: func(_ qpidriver.Config, opts devices.Options) (qpidriver.Driver, error) {
			return &fakeDriver{opts: opts}, nil
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
	for _, spec := range devices.Operations() {
		if spec.Summary == "" || len(spec.Events) == 0 {
			t.Errorf("expected %s to describe itself fully, got %+v", spec.Name, spec)
		}
	}
}

func TestOperationsCannotBeMutatedThroughTheAccessor(t *testing.T) {
	// Operations() hands out a copy, so a caller cannot quietly add one.
	first := devices.Operations()
	first[0].Summary = "tampered"
	if devices.Operations()[0].Summary == "tampered" {
		t.Error("expected Operations() to return a copy")
	}
}

// TestADownstreamModuleAddsADevice is the extension story of the Go SDK, which has
// no runtime import by name: a build of your own registers a spec and gets the same
// resolution, help and validation the built-in devices get (RFC 0003 §6, §8).
func TestADownstreamModuleAddsADevice(t *testing.T) {
	registry := devices.NewRegistry(fakeSpec("mylab_qpu", devices.Process))

	spec, err := registry.Resolve(devices.Process, "mylab_qpu")
	if err != nil {
		t.Fatalf("expected the registered device to resolve, got %v", err)
	}

	opts, err := spec.ParseOptions(map[string]string{"probes": "4", "label": "cryo"})
	if err != nil {
		t.Fatalf("expected the options to parse, got %v", err)
	}
	driver, err := spec.Build(qpidriver.Config{Token: "tok"}, opts)
	if err != nil {
		t.Fatalf("expected the builder to succeed, got %v", err)
	}

	built, ok := driver.(*fakeDriver)
	if !ok {
		t.Fatalf("expected the device's own driver, got %T", driver)
	}
	if built.opts.Int("probes") != 4 || built.opts.String("label") != "cryo" {
		t.Errorf("expected the parsed options to reach the builder, got %+v", built.opts)
	}
	// A default it did not pass, converted by the schema rather than by the builder.
	if built.opts.Bool("fast") {
		t.Error("expected fast to default to false")
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
		Build: func(qpidriver.Config, devices.Options) (qpidriver.Driver, error) {
			return nil, nil
		},
	}); err == nil {
		t.Error("expected an invented operation to be refused")
	}
}

func TestDevicesAreSortedByName(t *testing.T) {
	// Sorted rather than registration-ordered, so generated output is stable.
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

func TestParseOptionsConvertsAndFillsDefaults(t *testing.T) {
	spec := fakeSpec("mine", devices.Monitor)

	opts, err := spec.ParseOptions(map[string]string{"probes": "9"})
	if err != nil {
		t.Fatalf("expected the options to parse, got %v", err)
	}
	if opts.Int("probes") != 9 {
		t.Errorf("expected probes converted to 9, got %v", opts.Int("probes"))
	}
	if opts.Bool("fast") {
		t.Error("expected fast to come from its default, false")
	}
	// `label` has neither a value nor a default, so it is absent rather than "" —
	// which lets a builder tell "not set" from "set to nothing".
	if opts.Has("label") {
		t.Error("expected an option with no value and no default to be absent")
	}
}

func TestParseOptionsRejectsAnUnknownKey(t *testing.T) {
	spec := fakeSpec("mine", devices.Monitor)

	_, err := spec.ParseOptions(map[string]string{"probez": "9"})
	if err == nil {
		t.Fatal("expected an unknown key to fail")
	}
	if !strings.Contains(err.Error(), `unknown option "probez"`) {
		t.Errorf("expected the bad key named, got %q", err)
	}
	if !strings.Contains(err.Error(), "valid options: fast, label, probes") {
		t.Errorf("expected the valid keys listed and sorted, got %q", err)
	}

	_, err = spec.ParseOptions(map[string]string{"y": "1", "x": "2"})
	if err == nil || !strings.Contains(err.Error(), `unknown options "x", "y"`) {
		t.Errorf("expected both bad keys in one error, got %v", err)
	}
}

func TestParseOptionsPassesAnythingThroughWhenAsked(t *testing.T) {
	// For a device with no schema to check against: undeclared options go through
	// as typed, since with no schema there is nothing to convert them to.
	spec := fakeSpec("mine", devices.Monitor)
	spec.AcceptsAnyOption = true

	opts, err := spec.ParseOptions(map[string]string{"whatever": "1"})
	if err != nil {
		t.Fatalf("expected the option to pass through, got %v", err)
	}
	if opts.String("whatever") != "1" {
		t.Errorf("expected the raw string, got %v", opts.String("whatever"))
	}
}

func TestParseOptionsReportsAMissingRequiredKey(t *testing.T) {
	spec := fakeSpec("mine", devices.Monitor)
	spec.Options = append(spec.Options, devices.OptionSpec{
		Key: "channels", Help: "What to poll.", Required: true, Example: "a:K",
	})

	_, err := spec.ParseOptions(map[string]string{})
	if err == nil {
		t.Fatal("expected a missing required option to fail")
	}
	// The error is the fix: it shows the -o line that should have been typed.
	if !strings.Contains(err.Error(), `needs a "channels" option, e.g. -o channels=a:K`) {
		t.Errorf("expected the example in the message, got %q", err)
	}
}

func TestParseOptionsNamesTheOptionABadValueBelongsTo(t *testing.T) {
	spec := fakeSpec("mine", devices.Monitor)

	_, err := spec.ParseOptions(map[string]string{"probes": "many"})
	if err == nil || !strings.Contains(err.Error(), "bad value for -o probes") {
		t.Fatalf("expected the option named, got %v", err)
	}
}

func TestParsers(t *testing.T) {
	for raw, want := range map[string]bool{
		"1": true, "true": true, "TRUE": true, " yes ": true, "On": true,
		"0": false, "false": false, "no": false, "off": false, "": false, "maybe": false,
	} {
		got, err := devices.AsBool(raw)
		if err != nil {
			t.Fatalf("expected %q to parse, got %v", raw, err)
		}
		if got != want {
			t.Errorf("expected AsBool(%q) == %v, got %v", raw, want, got)
		}
	}

	if value, err := devices.AsFloat(" 2.5 "); err != nil || value != 2.5 {
		t.Errorf("expected AsFloat to trim and parse, got %v, %v", value, err)
	}
	if _, err := devices.AsFloat("soon"); err == nil {
		t.Error("expected AsFloat to reject a non-number")
	}
	if value, err := devices.AsInt(" 7 "); err != nil || value != 7 {
		t.Errorf("expected AsInt to trim and parse, got %v, %v", value, err)
	}
	if _, err := devices.AsInt("7.5"); err == nil {
		t.Error("expected AsInt to reject a decimal")
	}
	if value, err := devices.AsString(" kept "); err != nil || value != " kept " {
		t.Errorf("expected AsString to keep the value as-is, got %q, %v", value, err)
	}
}

func TestOptionsAccessorsAreTotal(t *testing.T) {
	// A missing or wrongly-typed option reads as the zero value rather than
	// panicking: a builder asking for a key its own spec declares cannot be wrong,
	// and one asking for anything else should not crash a driver.
	opts := devices.NewOptions(nil)

	if opts.String("x") != "" || opts.Int("x") != 0 || opts.Float("x") != 0 ||
		opts.Bool("x") || opts.Channels("x") != nil || opts.Seconds("x") != 0 {
		t.Error("expected zero values for absent options")
	}
	if _, ok := opts.Raw("x"); ok {
		t.Error("expected Raw to report an absent option")
	}
}

func TestOptionsSecondsConvertsFromFloat(t *testing.T) {
	// Seconds on the command line, a Duration in the driver — declared as "float"
	// in the catalog so the Python SDK reports the same type for the same option.
	opts := devices.NewOptions(map[string]any{"poll_interval": 2.5})

	if got := opts.Seconds("poll_interval"); got != 2500*time.Millisecond {
		t.Errorf("expected 2.5s, got %v", got)
	}
}

func TestCatalogShapeIsTheDocumentedOne(t *testing.T) {
	// Three SDKs produce this document and qpi-ui reads it, so the shape is the
	// thing under test — a renamed key is a break, not a detail (RFC 0003 §9).
	registry := devices.NewRegistry(fakeSpec("mine", devices.Monitor))

	encoded, err := json.Marshal(devices.CatalogOf(registry))
	if err != nil {
		t.Fatalf("expected the catalog to marshal, got %v", err)
	}

	var document map[string]any
	if err := json.Unmarshal(encoded, &document); err != nil {
		t.Fatal(err)
	}
	if len(document) != 2 || document["schema_version"] != float64(devices.SchemaVersion) {
		t.Fatalf("expected exactly schema_version and operations, got %v", document)
	}

	operations := document["operations"].([]any)
	if len(operations) != 2 {
		t.Fatalf("expected both operations listed even when one is empty, got %d", len(operations))
	}
	for _, entry := range operations {
		operation := entry.(map[string]any)
		if len(operation) != 5 {
			t.Errorf("expected five operation keys, got %v", operation)
		}
		for _, key := range []string{"name", "summary", "default_device", "events", "devices"} {
			if _, ok := operation[key]; !ok {
				t.Errorf("expected operation key %q, got %v", key, operation)
			}
		}
	}
}

func TestCatalogReportsOptionTypesAndDefaults(t *testing.T) {
	registry := devices.NewRegistry(fakeSpec("mine", devices.Monitor))

	catalog := devices.CatalogOf(registry)
	var monitor devices.CatalogOperation
	for _, operation := range catalog.Operations {
		if operation.Name == "monitor" {
			monitor = operation
		}
	}
	if len(monitor.Devices) != 1 {
		t.Fatalf("expected the one registered device, got %+v", monitor.Devices)
	}

	options := map[string]devices.CatalogOption{}
	for _, option := range monitor.Devices[0].Options {
		options[option.Key] = option
	}
	if options["probes"].Type != "int" || *options["probes"].Default != "1" {
		t.Errorf("expected probes as int defaulting to 1, got %+v", options["probes"])
	}
	// No default marshals to null, as it does in the Python SDK — not to "".
	if options["label"].Default != nil {
		t.Errorf("expected label to have no default, got %v", *options["label"].Default)
	}
}

func TestCatalogListsAnEmptyOperationAsEmpty(t *testing.T) {
	// Not omitted, and not null: a consumer must be able to tell "no devices here"
	// from "this key was missing".
	encoded, err := json.Marshal(devices.CatalogOf(devices.NewRegistry()))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(encoded), `"devices":[]`) {
		t.Errorf("expected an empty device list, got %s", encoded)
	}
}

func TestRenderCatalogNarrowsToOneOperation(t *testing.T) {
	registry := devices.NewRegistry(fakeSpec("mine", devices.Monitor))

	everything := devices.RenderCatalog(registry, "")
	if !strings.Contains(everything, "--operation process") ||
		!strings.Contains(everything, "--operation monitor") {
		t.Errorf("expected both operations, got:\n%s", everything)
	}

	monitorOnly := devices.RenderCatalog(registry, devices.Monitor)
	if strings.Contains(monitorOnly, "--operation process") {
		t.Errorf("expected process left out, got:\n%s", monitorOnly)
	}
	if !strings.Contains(monitorOnly, "probes=<int> (default: 1)") {
		t.Errorf("expected the option lines, got:\n%s", monitorOnly)
	}
}

func TestDeviceLinesOnlyAdvertiseADefaultThisBuildHas(t *testing.T) {
	// Which devices a Go binary has is decided when it is compiled, so an
	// operation's DefaultDevice is not necessarily one this build registered.
	// Advertising `default bluefors_gen1` in a binary that never registered it
	// would send an operator after a device that cannot run there.
	own := devices.NewRegistry(fakeSpec("thermometer", devices.Monitor))
	if got := devices.DeviceLines(own, devices.Monitor)[0]; strings.Contains(got, "default") {
		t.Errorf("expected no default advertised, got %q", got)
	}

	withDefault := devices.NewRegistry(fakeSpec("bluefors_gen1", devices.Monitor))
	if got := devices.DeviceLines(withDefault, devices.Monitor)[0]; !strings.Contains(got, "default bluefors_gen1") {
		t.Errorf("expected the default advertised when present, got %q", got)
	}
}

func TestHasReportsWhatIsRegistered(t *testing.T) {
	registry := devices.NewRegistry(fakeSpec("mine", devices.Monitor))

	if !registry.Has(devices.Monitor, "mine") {
		t.Error("expected a registered device to be found")
	}
	if registry.Has(devices.Monitor, "other") || registry.Has(devices.Process, "mine") {
		t.Error("expected Has to be scoped to the operation and name")
	}
}

func TestDeviceLinesSayWhenAnOperationIsEmpty(t *testing.T) {
	// Someone reading --help must be able to tell "no devices here" from "devices
	// exist but are not listed".
	lines := devices.DeviceLines(devices.NewRegistry(), devices.Process)

	joined := strings.Join(lines, "\n")
	if !strings.Contains(joined, "ships no process devices") {
		t.Errorf("expected the empty case stated, got:\n%s", joined)
	}
	if strings.Contains(joined, "default ") {
		t.Errorf("expected no default device advertised when there is none, got:\n%s", joined)
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
		// every later test, and --help, sees.
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

func TestLookupOperation(t *testing.T) {
	spec, ok := devices.LookupOperation(devices.Monitor)
	if !ok || spec.DefaultDevice != "bluefors_gen1" {
		t.Errorf("expected the monitor spec, got %+v (ok=%v)", spec, ok)
	}
	if _, ok := devices.LookupOperation(devices.Operation("telemetry")); ok {
		t.Error("expected an invented operation not to be found")
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

func TestOptionLineDescribesEveryKindOfOption(t *testing.T) {
	// Four shapes an option can have, each of which reads differently in help.
	registry := devices.NewRegistry(devices.DeviceSpec{
		Name:      "shapes",
		Operation: devices.Monitor,
		Build: func(qpidriver.Config, devices.Options) (qpidriver.Driver, error) {
			return nil, nil
		},
		Options: []devices.OptionSpec{
			{Key: "needed", Help: "Required, with an example.", Required: true, Example: "x"},
			{Key: "bare", Help: "Required, with none.", Required: true},
			{Key: "suggested", Help: "Optional, with an example.", Example: "y"},
			{Key: "plain", Help: "Optional, with neither."},
		},
	})

	lines := strings.Join(devices.DeviceLines(registry, devices.Monitor), "\n")
	for _, want := range []string{
		"• needed=<str> (required, e.g. x)",
		"• bare=<str> (required)",
		"• suggested=<str> (optional, e.g. y)",
		"• plain=<str> (optional)",
	} {
		if !strings.Contains(lines, want) {
			t.Errorf("expected %q in:\n%s", want, lines)
		}
	}
}

func TestAnOptionWithNoDeclaredTypeReadsAsAString(t *testing.T) {
	// Type is documentation, not enforcement: omitting it must not produce
	// `key=<>` in help or `"type": ""` in the catalog.
	registry := devices.NewRegistry(devices.DeviceSpec{
		Name:      "untyped",
		Operation: devices.Monitor,
		Build: func(qpidriver.Config, devices.Options) (qpidriver.Driver, error) {
			return nil, nil
		},
		Options: []devices.OptionSpec{{Key: "thing", Help: "Whatever."}},
	})

	if !strings.Contains(
		strings.Join(devices.DeviceLines(registry, devices.Monitor), "\n"),
		"thing=<str>",
	) {
		t.Error("expected an untyped option to render as <str>")
	}
	for _, operation := range devices.CatalogOf(registry).Operations {
		for _, device := range operation.Devices {
			if device.Options[0].Type != "str" {
				t.Errorf("expected the catalog to report str, got %q", device.Options[0].Type)
			}
		}
	}
}

func TestParseOptionsReportsNoneWhenADeviceDeclaresNothing(t *testing.T) {
	spec := devices.DeviceSpec{
		Name:      "bare",
		Operation: devices.Monitor,
		Build: func(qpidriver.Config, devices.Options) (qpidriver.Driver, error) {
			return nil, nil
		},
	}

	_, err := spec.ParseOptions(map[string]string{"anything": "1"})
	if err == nil || !strings.Contains(err.Error(), "valid options: none") {
		t.Errorf(`expected "none" rather than an empty list, got %v`, err)
	}
}
