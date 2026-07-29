package bluefors

import (
	qpidriver "github.com/sopherapps/qpi/qpi-driver/go"
	"github.com/sopherapps/qpi/qpi-driver/go/devices"
)

// DeviceSpec is what `--device bluefors_gen1` names: this package's driver
// (RFC 0003 §6).
//
// A name, an operation and a builder is the whole of it. What the device is and
// which `-o` keys to fill in belongs to QPI-UI, where the driver is registered;
// describing it here as well would be a second catalog to keep in step with the
// first (RFC 0003 §9).
//
// Whichever main assembles the CLI registers it:
// `devices.Register(bluefors.DeviceSpec)`.
var DeviceSpec = devices.DeviceSpec{
	Name:      "bluefors_gen1",
	Operation: devices.Monitor,
	Build:     build,
}

// build returns an unstarted driver from its `-o` options.
//
// The options this monitor reads are the ones read here. Only the channels are
// unknowable in advance — which ones a system exposes depends on how its mappers
// are configured — so they are the one option with no default and the one whose
// absence is an error.
func build(cfg qpidriver.Config, opts *devices.Options) (qpidriver.Driver, error) {
	channels := opts.Require("channels", "mapper.bf.tmc:K,mapper.bf.pmc:mbar")
	options := Options{
		BaseURL:      opts.String("base_url", DefaultBaseURL),
		Channels:     ParseChannels(channels),
		APIKey:       opts.String("api_key", ""),
		PollInterval: opts.Seconds("poll_interval", DefaultPollInterval),
		Timeout:      opts.Seconds("timeout", DefaultTimeout),
	}
	if err := opts.Err(); err != nil {
		return nil, err
	}
	return New(options), nil
}
