// Command qpi-driver is the CLI that runs QPI's officially maintained Go
// built-in drivers, mirroring the Python `qpi-driver` CLI (RFC 0003 §4): one
// `start` verb, `--operation` saying what the driver does, `--device` selecting
// the backend within it, universal flags shared, and a device's own settings
// passed as repeatable `-o key=value`.
//
// Install it with:
//
//	go install github.com/sopherapps/qpi/qpi-driver/go/qpi-driver@latest
//
// then, e.g.:
//
//	qpi-driver start --operation monitor --device bluefors_gen1 \
//	  --qpi-addr https://qpi.example.com --token … --ca-fingerprint … \
//	  -o base_url=http://localhost:49099 -o channels=mapper.bf.tmc:K
//
// This file is the whole of it: register the devices this build ships, then hand
// off to the importable [cli] package. A build of your own that registers a device
// of its own is the same two steps and gets the same CLI — see the module README.
package main

import (
	"log"

	"github.com/sopherapps/qpi/qpi-driver/go/cli"
	"github.com/sopherapps/qpi/qpi-driver/go/devices"
	"github.com/sopherapps/qpi/qpi-driver/go/qpi-driver/bluefors"
)

// builtinDevices are the devices this binary ships. One entry per device, and the
// same entry a downstream main writes for a device of its own (RFC 0003 §6).
func builtinDevices() []devices.DeviceSpec {
	return []devices.DeviceSpec{bluefors.DeviceSpec}
}

func main() {
	for _, spec := range builtinDevices() {
		if err := devices.Register(spec); err != nil {
			log.Fatal(err)
		}
	}
	cli.Execute()
}
