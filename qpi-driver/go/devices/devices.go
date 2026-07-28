// Package devices is the operation and device catalog for the Go driver SDK
// (RFC 0003 §5, §6, §8) — the Go counterpart of the Python SDK's
// qpi_driver.builtins.registry.
//
// Two ideas, deliberately asymmetric:
//
// An operation is a contract with QPI-UI — Process runs jobs pushed to it,
// Monitor reports upward on its own schedule. QPI-UI needs a server-side handler
// for each, so the set is closed: the constants below are the whole of it, and
// growing it is a coordinated change across the SDKs and the server.
//
// A device is one backend implementing an operation. The set is open, and this is
// where they land. Go has no runtime import by name, so a device from outside the
// SDK arrives at compile time: a downstream main imports this package, calls
// [Register], and hands off to the SDK's own CLI — see the README. That is the
// whole extension story, and it is why Register and Registry are exported while
// the Python SDK also has entry points and import paths.
//
// A device is described by data ([DeviceSpec]), never by running it, so `--help`,
// `catalog --json` and QPI-UI's own catalog are all generated from one source.
// Specs live beside the code they describe — the bluefors package owns its own —
// and are registered by whichever main assembles the CLI.
package devices

import (
	"fmt"
	"sort"
	"strconv"
	"strings"
	"time"

	qpidriver "github.com/sopherapps/qpi/qpi-driver/go"
)

// Operation is what a driver does, and the contract QPI-UI implements for it.
// Closed by design: every value needs a handler on the server side, so a new
// operation is a coordinated change across the SDKs and QPI-UI, never a
// third-party extension (RFC 0003 §13.1). Devices are the open half.
type Operation string

const (
	// Process runs quantum jobs pushed to the driver and reports their results.
	Process Operation = "process"
	// Monitor reports readings upward on the driver's own schedule.
	Monitor Operation = "monitor"
)

// Event-type names an operation takes part in. They mirror the wire event types
// in the SDK's events.go, the same way qpi-ui/internal/drivers does.
const (
	eventJobDispatch     = "JobDispatch"
	eventJobResult       = "JobResult"
	eventCryostatReading = "CryostatReading"
)

// DeviceBuilder builds — but does not run — the driver for one device, from the
// universal transport config and the device's own parsed options.
//
// Returning a driver rather than running one is what lets a device be built and
// asserted on in a test with no server, and keeps [qpidriver.Run] the only thing
// that runs anything (RFC 0003 §7). Driver is sealed by an unexported method, so a
// builder can only return something embedding [qpidriver.Base] — which is the
// point: Run can always reach the transport.
type DeviceBuilder func(cfg qpidriver.Config, opts Options) (qpidriver.Driver, error)

// Parser turns one raw `-o` string into the value a builder wants. The only place
// an option value is converted, so no builder hand-rolls strconv calls.
type Parser func(raw string) (any, error)

// OptionSpec is one `-o key=value` setting a device reads.
type OptionSpec struct {
	// Key is the option name as typed, e.g. "channels".
	Key string
	// Help is one line for generated help.
	Help string
	// Type names the kind of value expected — "str", "int", "float", "bool",
	// "channels". It is what `catalog --json` reports, and it must match what the
	// Python SDK reports for the same device, since a test compares the two
	// catalogs (RFC 0003 §9).
	Type string
	// Parse converts the raw string. Defaults to [AsString] when nil.
	Parse Parser
	// Default is the value used when the option is absent, written as it would be
	// typed on the command line. It goes through Parse like any other value.
	// Empty means there is no default.
	Default string
	// Required reports whether omitting the option is an error.
	Required bool
	// Example is a ready-to-paste value for generated help and snippets.
	Example string
}

// DeviceSpec is the data-only description of one device.
type DeviceSpec struct {
	// Name is how --device names it, e.g. "bluefors_gen1".
	Name string
	// Operation is which operation this device implements.
	Operation Operation
	// Build returns an unstarted driver; see [DeviceBuilder].
	Build DeviceBuilder
	// Options are the `-o` keys this device reads.
	Options []OptionSpec
	// Extra is the install target that ships it, e.g. "qpi-driver[cli,qblox]" —
	// meaningful for the Python SDK's devices, empty here where a device is
	// compiled in.
	Extra string
	// Summary is one line describing the device, for generated help.
	Summary string
	// AcceptsAnyOption passes an undeclared `-o` key through as a raw string
	// instead of rejecting it. For a device with no schema to check against; a
	// device in the catalog declares its options and leaves this alone, so a typo
	// stays an error.
	AcceptsAnyOption bool
}

// OperationSpec is the data-only description of one operation.
type OperationSpec struct {
	// Name is the operation itself.
	Name Operation
	// Summary is one line describing what drivers of this operation do.
	Summary string
	// DefaultDevice is the device used when --device is omitted. Empty when this
	// SDK registers none, in which case the CLI says so rather than offering a
	// device it cannot honour (RFC 0003 §8).
	DefaultDevice string
	// DefaultName is the driver name used when --name is omitted.
	DefaultName string
	// Events are the event-type names drivers of this operation take part in.
	Events []string
}

// operationSpecs is every operation, in the order help and `catalog --json` list
// them. Deliberately the same order and content as the Python SDK's OPERATIONS.
var operationSpecs = []OperationSpec{
	{
		Name:          Process,
		Summary:       "Run quantum jobs pushed by QPI-UI and report their results.",
		DefaultDevice: "",
		DefaultName:   "qpu_sim_01",
		Events:        []string{eventJobDispatch, eventJobResult},
	},
	{
		Name:          Monitor,
		Summary:       "Report readings upward on a timer.",
		DefaultDevice: "bluefors_gen1",
		DefaultName:   "qpi-monitor",
		Events:        []string{eventCryostatReading},
	},
}

// Registry is a set of devices, keyed by operation and then by name. Names are
// unique per operation rather than globally, since --device is always read in the
// context of an operation.
//
// Tests use their own registry to assemble a catalog in isolation; the CLI uses
// the package-level [Default].
type Registry struct {
	devices map[Operation]map[string]DeviceSpec
}

// NewRegistry builds a registry holding the given specs. It panics on a duplicate,
// which can only be a programming error at assembly time — see [Registry.Register]
// for the recoverable form.
func NewRegistry(specs ...DeviceSpec) *Registry {
	r := &Registry{devices: map[Operation]map[string]DeviceSpec{}}
	for _, spec := range specs {
		if err := r.Register(spec); err != nil {
			panic(err)
		}
	}
	return r
}

// Default is the registry the SDK's CLI reads. A downstream main registers into it
// and gets the same CLI, help and validation.
var Default = NewRegistry()

// Register adds spec to the registry.
//
// It fails rather than replacing a device of the same name, because silently
// replacing one would make the winner depend on the order of the calls.
func (r *Registry) Register(spec DeviceSpec) error {
	if spec.Name == "" || spec.Build == nil {
		return fmt.Errorf("a device spec needs a name and a builder, got %+v", spec)
	}
	if _, ok := operationOf(spec.Operation); !ok {
		return fmt.Errorf("unknown operation %q for device %q; valid operations: %s",
			spec.Operation, spec.Name, strings.Join(OperationNames(), ", "))
	}
	if r.devices[spec.Operation] == nil {
		r.devices[spec.Operation] = map[string]DeviceSpec{}
	}
	if _, exists := r.devices[spec.Operation][spec.Name]; exists {
		return fmt.Errorf("%s device %q is already registered", spec.Operation, spec.Name)
	}
	r.devices[spec.Operation][spec.Name] = spec
	return nil
}

// Devices returns every device registered for operation, sorted by name — sorted
// rather than in registration order so generated help and `catalog --json` are
// stable no matter what registered first.
func (r *Registry) Devices(operation Operation) []DeviceSpec {
	names := make([]string, 0, len(r.devices[operation]))
	for name := range r.devices[operation] {
		names = append(names, name)
	}
	sort.Strings(names)

	specs := make([]DeviceSpec, 0, len(names))
	for _, name := range names {
		specs = append(specs, r.devices[operation][name])
	}
	return specs
}

// Resolve looks device up within operation.
//
// An operation this build registers no devices for says so and where to find one,
// rather than reporting an unknown device with an empty list of known ones
// (RFC 0003 §8).
func (r *Registry) Resolve(operation Operation, device string) (DeviceSpec, error) {
	if _, ok := operationOf(operation); !ok {
		return DeviceSpec{}, fmt.Errorf("unknown operation %q; valid operations: %s",
			operation, strings.Join(OperationNames(), ", "))
	}
	if len(r.devices[operation]) == 0 {
		return DeviceSpec{}, fmt.Errorf(
			"this build ships no %s devices; run one from the Python SDK instead "+
				"(pip install %q) or register one in your own main", operation,
			"qpi-driver[cli]")
	}

	spec, ok := r.devices[operation][device]
	if !ok {
		return DeviceSpec{}, fmt.Errorf("unknown %s device %q; known devices: %s",
			operation, device, strings.Join(DeviceNames(r.Devices(operation)), ", "))
	}
	return spec, nil
}

// Has reports whether operation has a device of this name registered.
//
// Worth asking about an operation's own DefaultDevice: which devices a Go binary
// has is decided when it is compiled, so the default an operation names is not
// necessarily one this build registered, and advertising one it does not have would
// send an operator after a device that cannot run here.
func (r *Registry) Has(operation Operation, device string) bool {
	_, ok := r.devices[operation][device]
	return ok
}

// Register adds spec to [Default].
func Register(spec DeviceSpec) error { return Default.Register(spec) }

// Devices returns [Default]'s devices for operation, sorted by name.
func Devices(operation Operation) []DeviceSpec { return Default.Devices(operation) }

// Resolve looks device up in [Default] within operation.
func Resolve(operation Operation, device string) (DeviceSpec, error) {
	return Default.Resolve(operation, device)
}

// Operations returns every operation, in declaration order.
func Operations() []OperationSpec {
	specs := make([]OperationSpec, len(operationSpecs))
	copy(specs, operationSpecs)
	return specs
}

// OperationNames names the operations in declaration order, for help and errors.
func OperationNames() []string {
	names := make([]string, 0, len(operationSpecs))
	for _, spec := range operationSpecs {
		names = append(names, string(spec.Name))
	}
	return names
}

// DeviceNames names the given devices, in the order they come.
func DeviceNames(specs []DeviceSpec) []string {
	names := make([]string, 0, len(specs))
	for _, spec := range specs {
		names = append(names, spec.Name)
	}
	return names
}

// Operation looks an operation's spec up by name.
func operationOf(operation Operation) (OperationSpec, bool) {
	for _, spec := range operationSpecs {
		if spec.Name == operation {
			return spec, true
		}
	}
	return OperationSpec{}, false
}

// LookupOperation returns the spec for operation, and whether it exists.
func LookupOperation(operation Operation) (OperationSpec, bool) {
	return operationOf(operation)
}

// Options are a device's `-o` values, already checked and converted by its own
// schema. The typed accessors are lookups, not conversions: whatever a value is
// going to be, it became that in [DeviceSpec.ParseOptions].
type Options struct {
	values map[string]any
}

// NewOptions wraps already-converted values, for tests and for builders called
// directly.
func NewOptions(values map[string]any) Options {
	if values == nil {
		values = map[string]any{}
	}
	return Options{values: values}
}

// Has reports whether the option is present at all — which distinguishes "not
// set" from "set to the zero value".
func (o Options) Has(key string) bool {
	_, ok := o.values[key]
	return ok
}

// Raw returns the stored value, whatever its type, and whether it was present.
func (o Options) Raw(key string) (any, bool) {
	value, ok := o.values[key]
	return value, ok
}

// String returns a string option, or "" if it is absent or not a string.
func (o Options) String(key string) string {
	value, _ := o.values[key].(string)
	return value
}

// Int returns an int option, or 0 if it is absent or not an int.
func (o Options) Int(key string) int {
	value, _ := o.values[key].(int)
	return value
}

// Float returns a float option, or 0 if it is absent or not a float.
func (o Options) Float(key string) float64 {
	value, _ := o.values[key].(float64)
	return value
}

// Bool returns a bool option, or false if it is absent or not a bool.
func (o Options) Bool(key string) bool {
	value, _ := o.values[key].(bool)
	return value
}

// Seconds returns a float option as a duration, which is how every time-valued
// option in the catalog is declared — seconds on the command line, a
// [time.Duration] in the driver.
func (o Options) Seconds(key string) time.Duration {
	return time.Duration(o.Float(key) * float64(time.Second))
}

// Channels returns a channel-map option, or nil if it is absent or not one.
func (o Options) Channels(key string) map[string]string {
	value, _ := o.values[key].(map[string]string)
	return value
}

// AsString is the default parser: no conversion at all.
func AsString(raw string) (any, error) { return raw, nil }

// AsInt parses a whole number.
func AsInt(raw string) (any, error) {
	value, err := strconv.Atoi(strings.TrimSpace(raw))
	if err != nil {
		return nil, fmt.Errorf("%q is not a whole number", raw)
	}
	return value, nil
}

// AsFloat parses a decimal number, which is how seconds are given.
func AsFloat(raw string) (any, error) {
	value, err := strconv.ParseFloat(strings.TrimSpace(raw), 64)
	if err != nil {
		return nil, fmt.Errorf("%q is not a number", raw)
	}
	return value, nil
}

// AsBool parses a boolean-ish value: 1, true, yes and on are true, in any case;
// anything else, including the empty string, is false. Matches the Python SDK's
// as_bool, so the same `-o is_dummy=on` means the same thing in both.
func AsBool(raw string) (any, error) {
	switch strings.ToLower(strings.TrimSpace(raw)) {
	case "1", "true", "yes", "on":
		return true, nil
	}
	return false, nil
}

// ParseOptions checks raw `-o` values against this device's schema and converts
// them.
//
// The result is what [DeviceSpec.Build] is called with, and carries every option
// that has a value — the ones given, plus the parsed default of each one left out
// — so a builder reads its keys without repeating their defaults, and conversion
// happens here rather than in every builder. A key the device does not declare is
// an error unless [DeviceSpec.AcceptsAnyOption] says to pass it through.
func (s DeviceSpec) ParseOptions(raw map[string]string) (Options, error) {
	declared := make(map[string]OptionSpec, len(s.Options))
	valid := make([]string, 0, len(s.Options))
	for _, option := range s.Options {
		declared[option.Key] = option
		valid = append(valid, option.Key)
	}
	sort.Strings(valid)

	parsed := map[string]any{}

	var unknown []string
	for key, value := range raw {
		if _, ok := declared[key]; ok {
			continue
		}
		if !s.AcceptsAnyOption {
			unknown = append(unknown, key)
			continue
		}
		// With no schema there is nothing to convert it to, and guessing would be
		// worse than leaving it as typed.
		parsed[key] = value
	}
	if len(unknown) > 0 {
		sort.Strings(unknown)
		label := "option"
		if len(unknown) > 1 {
			label = "options"
		}
		return Options{}, fmt.Errorf("unknown %s %s for %s device %q; valid options: %s",
			label, quoteAll(unknown), s.Operation, s.Name, joinOrNone(valid))
	}

	for _, option := range s.Options {
		value, given := raw[option.Key]
		switch {
		case given:
		case option.Required:
			example := ""
			if option.Example != "" {
				example = fmt.Sprintf(", e.g. -o %s=%s", option.Key, option.Example)
			}
			return Options{}, fmt.Errorf("%s device %q needs a %q option%s",
				s.Operation, s.Name, option.Key, example)
		case option.Default == "":
			continue
		default:
			value = option.Default
		}

		parse := option.Parse
		if parse == nil {
			parse = AsString
		}
		converted, err := parse(value)
		if err != nil {
			return Options{}, fmt.Errorf("bad value for -o %s: %w", option.Key, err)
		}
		parsed[option.Key] = converted
	}

	return NewOptions(parsed), nil
}

func quoteAll(values []string) string {
	quoted := make([]string, len(values))
	for i, value := range values {
		quoted[i] = strconv.Quote(value)
	}
	return strings.Join(quoted, ", ")
}

func joinOrNone(values []string) string {
	if len(values) == 0 {
		return "none"
	}
	return strings.Join(values, ", ")
}
