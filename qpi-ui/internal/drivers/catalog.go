package drivers

// Event-type names a backend can participate in. They mirror the wire event
// types in package api; a test in that package asserts they stay in step.
const (
	eventJobDispatch     = "JobDispatch"
	eventJobResult       = "JobResult"
	eventCryostatReading = "CryostatReading"
)

// processSpec builds the spec for a QPU-shaped executor kind: it runs the job
// flow and is launched with `qpi-driver start --operation process --device <kind>`. Its runtime
// config (data dir, timeouts, …) has working defaults, so no snippet options.
func processSpec(kind Kind, extra string) Spec {
	return Spec{
		Kind:      kind,
		Operation: Process,
		Extra:     extra,
		Events:    []string{eventJobDispatch, eventJobResult},
	}
}

// Default is the catalog the server uses. Register a new backend by adding one
// line here — a processSpec for a QPU-shaped kind, or a Spec with
// Operation=Monitor and its -o options for a monitor-shaped kind.
var Default = NewRegistry(
	processSpec(Mock, ""),
	processSpec(Presto, ""),
	processSpec(QiskitAer, "qpi-driver[cli,aer]"),
	processSpec(Quantify, "qpi-driver[cli,quantify]"),
	processSpec(Qblox, "qpi-driver[cli,qblox]"),
	Spec{
		Kind:      BlueforsGen1,
		Operation: Monitor,
		Extra:     "qpi-driver[cli,bluefors_gen1]",
		Events:    []string{eventCryostatReading},
		Options: []Option{
			{Key: "base_url", Example: "http://localhost:49099"},
			{Key: "channels", Example: "mapper.bf.tmc:K,mapper.bf.pmc:mbar"},
		},
	},
)
