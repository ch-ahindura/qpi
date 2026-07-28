package drivers

// Event-type names a backend can participate in. They mirror the wire event
// types in package api; a test in that package asserts they stay in step.
const (
	eventJobDispatch     = "JobDispatch"
	eventJobResult       = "JobResult"
	eventCryostatReading = "CryostatReading"
)

// processOptions are the `-o` keys every process device reads, mirroring the SDKs'
// qpu OPTIONS. All of them have working defaults, so a QPU needs no -o options at
// all and none is pre-filled in a snippet — but they belong here, because the
// catalog is checked against the SDK's own and because the dashboard shows them.
//
// The two quantify paths are only meaningful to the quantify and qblox executors;
// they are listed for every process device because the SDK declares them for every
// process device, and the catalog's job is to say what the SDK accepts.
func processOptions() []Option {
	return []Option{
		{
			Key:     "data_dir",
			Help:    "Directory the executor writes datasets and artefacts to.",
			Default: "./bin/data",
			Example: "./bin/data",
		},
		{
			Key:     "job_timeout",
			Help:    "Seconds a single job may run before it is abandoned.",
			Default: "10",
			Example: "30",
		},
		{
			Key:     "is_dummy",
			Help:    "Run against the vendor's dummy instruments instead of real hardware.",
			Default: "false",
			Example: "true",
		},
		{
			Key:     "quantify_hardware_config",
			Help:    "Path to the quantify hardware configuration JSON.",
			Default: "./quantify.hardware.json",
			Example: "./quantify.hardware.json",
		},
		{
			Key:     "quantify_device_config",
			Help:    "Path to the quantify device configuration YAML.",
			Default: "./quantify.device.yml",
			Example: "./quantify.device.yml",
		},
	}
}

// processSpec builds the spec for a QPU-shaped executor kind: it runs the job
// flow and is launched with `qpi-driver start --operation process --device <kind>`.
//
// Python only: it is the one SDK with a process device, and the Go and TypeScript
// CLIs say so rather than pretending (RFC 0003 §8). A QPU in another language is a
// Custom driver.
func processSpec(kind Kind, extra string) Spec {
	return Spec{
		Kind:      kind,
		Operation: Process,
		Extra:     extra,
		Events:    []string{eventJobDispatch, eventJobResult},
		Options:   processOptions(),
		Languages: []Language{Python},
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
		// The one device all three SDKs ship.
		Languages: []Language{Python, Go, TypeScript},
		// Only the channels are unknowable in advance — which mappers a given
		// system has is configuration — so they are the one required option. The
		// base URL is pre-filled too: the default points at the cryostat host's
		// own loopback, which is right only when the driver runs there.
		Options: []Option{
			{
				Key:       "channels",
				Help:      "Value-tree channels to poll, as path[:unit] pairs.",
				Required:  true,
				Example:   "mapper.bf.tmc:K,mapper.bf.pmc:mbar",
				InSnippet: true,
			},
			{
				Key:       "base_url",
				Help:      "Base URL of the Bluefors Control API.",
				Default:   "http://127.0.0.1:49099",
				Example:   "http://localhost:49099",
				InSnippet: true,
			},
			{
				Key:     "api_key",
				Help:    "Bluefors API access key, if the API requires one.",
				Example: "<bluefors-api-key>",
			},
			{
				Key:     "poll_interval",
				Help:    "Seconds between polls of every channel.",
				Default: "5.0",
				Example: "5",
			},
			{
				Key:     "timeout",
				Help:    "HTTP timeout per channel read, in seconds.",
				Default: "5.0",
				Example: "5",
			},
		},
	},
)
