package bluefors

import (
	qpidriver "github.com/sopherapps/qpi/qpi-driver/go"
	"github.com/sopherapps/qpi/qpi-driver/go/devices"
)

// DeviceSpec describes this monitor to the CLI: the `-o` options it reads, with
// their types, defaults and examples, and the builder that turns them into a
// driver (RFC 0003 §5).
//
// It lives here, beside the driver it describes, rather than in a table somewhere
// central — so the two cannot drift apart. Whichever main assembles the CLI
// registers it: `devices.Register(bluefors.DeviceSpec)`.
//
// The option keys, types and defaults match the Python SDK's bluefors_gen1 device
// exactly; a test compares the two catalogs, since QPI-UI renders setup snippets
// from one of them and an operator may well run the other (RFC 0003 §9).
var DeviceSpec = devices.DeviceSpec{
	Name:      "bluefors_gen1",
	Operation: devices.Monitor,
	Summary:   "Cryostat monitor for Bluefors Control Software Gen. 1.",
	Build:     build,
	Options: []devices.OptionSpec{
		{
			Key:      "channels",
			Help:     "Value-tree channels to poll, as path[:unit] pairs.",
			Type:     "channels",
			Parse:    asChannels,
			Required: true,
			Example:  "mapper.bf.tmc:K,mapper.bf.pmc:mbar",
		},
		{
			Key:     "base_url",
			Help:    "Base URL of the Bluefors Control API.",
			Type:    "str",
			Default: DefaultBaseURL,
			Example: "http://localhost:49099",
		},
		{
			Key:     "api_key",
			Help:    "Bluefors API access key, if the API requires one.",
			Type:    "str",
			Example: "<bluefors-api-key>",
		},
		{
			Key:     "poll_interval",
			Help:    "Seconds between polls of every channel.",
			Type:    "float",
			Parse:   devices.AsFloat,
			Default: "5.0",
			Example: "5",
		},
		{
			Key:     "timeout",
			Help:    "HTTP timeout per channel read, in seconds.",
			Type:    "float",
			Parse:   devices.AsFloat,
			Default: "5.0",
			Example: "5",
		},
	},
}

// build returns an unstarted driver from the parsed options. There is nothing to
// validate or convert here: the spec's own schema did that, so a missing or
// malformed option has already become a clean CLI error by the time this runs.
func build(cfg qpidriver.Config, opts devices.Options) (qpidriver.Driver, error) {
	return New(Options{
		BaseURL:      opts.String("base_url"),
		Channels:     opts.Channels("channels"),
		APIKey:       opts.String("api_key"),
		PollInterval: opts.Seconds("poll_interval"),
		Timeout:      opts.Seconds("timeout"),
	}), nil
}

// asChannels adapts [ParseChannels] to an option parser.
func asChannels(raw string) (any, error) { return ParseChannels(raw), nil }
