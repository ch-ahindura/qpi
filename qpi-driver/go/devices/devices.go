// Package devices is what `--device` can name in the Go driver SDK: a table of
// builders (RFC 0003 §6, §8, §9) — the Go counterpart of the Python SDK's
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
// What a device is, here, is a name and a builder. It has no description and no
// declared options, because none of that is the driver's to publish: QPI-UI is
// where a device is chosen and configured, and a driver that also described itself
// would be a second catalog to keep in step with the first. A driver runs what it
// is told to run.
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

// DeviceBuilder builds — but does not run — the driver for one device, from the
// universal transport config and the device's own `-o` options.
//
// Returning a driver rather than running one is what lets a device be built and
// asserted on in a test with no server, and keeps [qpidriver.Run] the only thing
// that runs anything (RFC 0003 §7). Driver is sealed by an unexported method, so a
// builder can only return something embedding [qpidriver.Base] — which is the
// point: Run can always reach the transport.
type DeviceBuilder func(cfg qpidriver.Config, opts *Options) (qpidriver.Driver, error)

// DeviceSpec is one device: what --device names, and what to call when it does.
type DeviceSpec struct {
	// Name is how --device names it, e.g. "bluefors_gen1".
	Name string
	// Operation is which operation this device implements.
	Operation Operation
	// Build returns an unstarted driver; see [DeviceBuilder].
	Build DeviceBuilder
}

// operationNames is every operation, in the order help and errors list them.
var operationNames = []Operation{Process, Monitor}

// Registry is a set of devices, keyed by operation and then by name. Names are
// unique per operation rather than globally, since --device is always read in the
// context of an operation.
//
// Tests use their own registry to assemble one in isolation; the CLI uses the
// package-level [Default].
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
// and gets the same CLI.
var Default = NewRegistry()

// Register adds spec to the registry.
//
// It fails rather than replacing a device of the same name, because silently
// replacing one would make the winner depend on the order of the calls.
func (r *Registry) Register(spec DeviceSpec) error {
	if spec.Name == "" || spec.Build == nil {
		return fmt.Errorf("a device spec needs a name and a builder, got %+v", spec)
	}
	if !KnownOperation(spec.Operation) {
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
// rather than in registration order so `qpi-driver devices` is stable no matter
// what registered first.
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
	if !KnownOperation(operation) {
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
func Operations() []Operation {
	operations := make([]Operation, len(operationNames))
	copy(operations, operationNames)
	return operations
}

// OperationNames names the operations in declaration order, for help and errors.
func OperationNames() []string {
	names := make([]string, 0, len(operationNames))
	for _, operation := range operationNames {
		names = append(names, string(operation))
	}
	return names
}

// KnownOperation reports whether operation is one of the two.
func KnownOperation(operation Operation) bool {
	for _, known := range operationNames {
		if known == operation {
			return true
		}
	}
	return false
}

// DeviceNames names the given devices, in the order they come.
func DeviceNames(specs []DeviceSpec) []string {
	names := make([]string, 0, len(specs))
	for _, spec := range specs {
		names = append(names, spec.Name)
	}
	return names
}

// Options are the `-o key=value` settings a device was launched with.
//
// There is no option schema in this SDK. A device's options are whatever keys its
// builder reads, and a value's type is whichever accessor reads it — so there is no
// second description of a device to keep in step with the device, and nothing for
// QPI-UI to be checked against.
//
// Every accessor takes the fallback used when the key is absent, and a value that
// will not convert is recorded rather than returned: a builder reads its options
// as a flat run of statements and checks [Options.Err] once at the end. Reads are
// remembered, so [Options.Unread] can report a key nothing looked at — which is
// what keeps `-o pol_interval=2` an error without a declared list to check it
// against.
type Options struct {
	values map[string]string
	read   map[string]bool
	err    error
}

// NewOptions wraps the raw `-o` values, as the CLI hands them over.
func NewOptions(values map[string]string) *Options {
	copied := make(map[string]string, len(values))
	for key, value := range values {
		copied[key] = value
	}
	return &Options{values: copied, read: map[string]bool{}}
}

// Has reports whether the option was given. Counts as reading it.
func (o *Options) Has(key string) bool {
	o.read[key] = true
	_, ok := o.values[key]
	return ok
}

// String returns the option as typed, or fallback if it is absent.
func (o *Options) String(key, fallback string) string {
	o.read[key] = true
	if value, ok := o.values[key]; ok {
		return value
	}
	return fallback
}

// Require returns the option, or records an error naming it and the `-o` pair that
// was missing. For the one kind of option a device cannot make up a value for.
func (o *Options) Require(key, example string) string {
	o.read[key] = true
	value, ok := o.values[key]
	if !ok {
		hint := ""
		if example != "" {
			hint = fmt.Sprintf(", e.g. -o %s=%s", key, example)
		}
		o.fail(fmt.Errorf("missing required option %q%s", key, hint))
	}
	return value
}

// Int returns the option as a whole number, or fallback if it is absent.
func (o *Options) Int(key string, fallback int) int {
	raw, ok := o.lookup(key)
	if !ok {
		return fallback
	}
	value, err := strconv.Atoi(strings.TrimSpace(raw))
	if err != nil {
		o.fail(fmt.Errorf("bad value for -o %s: %q is not a whole number", key, raw))
		return fallback
	}
	return value
}

// Float returns the option as a decimal number, or fallback if it is absent.
func (o *Options) Float(key string, fallback float64) float64 {
	raw, ok := o.lookup(key)
	if !ok {
		return fallback
	}
	value, err := strconv.ParseFloat(strings.TrimSpace(raw), 64)
	if err != nil {
		o.fail(fmt.Errorf("bad value for -o %s: %q is not a number", key, raw))
		return fallback
	}
	return value
}

// Bool returns the option as a boolean, or fallback if it is absent. 1, true, yes
// and on are true in any case; every other value, the empty string included, is
// false. Matches the Python SDK, so the same `-o is_dummy=on` means the same thing
// in both — and nothing here can fail, so a misspelling is false rather than a
// refusal to start.
func (o *Options) Bool(key string, fallback bool) bool {
	raw, ok := o.lookup(key)
	if !ok {
		return fallback
	}
	switch strings.ToLower(strings.TrimSpace(raw)) {
	case "1", "true", "yes", "on":
		return true
	}
	return false
}

// Seconds returns the option as a duration given in seconds, which is how every
// time-valued option is written on a command line.
func (o *Options) Seconds(key string, fallback time.Duration) time.Duration {
	seconds := o.Float(key, fallback.Seconds())
	return time.Duration(seconds * float64(time.Second))
}

// Remaining returns every option not read yet, as typed, and marks them read. For a
// device passing options on to something this SDK has never seen; a device that
// reads its own options should not need it.
func (o *Options) Remaining() map[string]string {
	rest := map[string]string{}
	for key, value := range o.values {
		if !o.read[key] {
			rest[key] = value
			o.read[key] = true
		}
	}
	return rest
}

// Unread returns the keys given that nothing read, sorted. The caller reports them:
// the device has finished building by then, so a key left over is one it does not
// understand.
func (o *Options) Unread() []string {
	var unread []string
	for key := range o.values {
		if !o.read[key] {
			unread = append(unread, key)
		}
	}
	sort.Strings(unread)
	return unread
}

// Err returns the first value a read could not convert, or nil. A builder checks it
// once, after reading everything, rather than after every line.
func (o *Options) Err() error { return o.err }

func (o *Options) lookup(key string) (string, bool) {
	o.read[key] = true
	value, ok := o.values[key]
	return value, ok
}

func (o *Options) fail(err error) {
	if o.err == nil {
		o.err = err
	}
}
