# qpi-driver (Go SDK)

The Go SDK for building [QPI](https://github.com/sopherapps/qpi) drivers — the
external processes that exchange typed events with QPI-UI (RFC 0001). It mirrors
the Python SDK (`qpi-driver/py`) and the TypeScript SDK (`qpi-driver/js`): the
same event envelope, the same `drivers/connect` handshake, and TLS with the
pinned root CA.

```
go get github.com/sopherapps/qpi/qpi-driver/go
```

> **Upgrading?** This release changes the CLI grammar and some SDK APIs. The full
> before/after migration table is in the
> [CHANGELOG](https://github.com/sopherapps/qpi/blob/main/CHANGELOG.md#migration).
> **What this SDK ships:** one `monitor` device, `bluefors_gen1`. It ships no
> `process` (QPU) device — running a QPU means the Python SDK
> (`pip install "qpi-driver[cli]"`) or a device of your own, registered as below.
> `qpi-driver start --operation process` says so rather than offering a device it
> does not have.


## Writing a driver

Embed `qpidriver.Base`, implement `HandleEvent`, and call `qpidriver.Run`:

```go
package main

import (
	"log"

	qpidriver "github.com/sopherapps/qpi/qpi-driver/go"
)

type MyDriver struct {
	qpidriver.Base
}

func (d *MyDriver) HandleEvent(event qpidriver.Event) {
	if event.Type == qpidriver.JobDispatch {
		results := runMyBackend(event.Payload)
		_ = d.Emit(qpidriver.NewEvent(qpidriver.JobResult, d.DriverName(), map[string]any{
			"job_id":  event.Payload["job_id"],
			"status":  "completed",
			"results": results,
		}))
	}
}

func main() {
	if err := qpidriver.Run(&MyDriver{}, qpidriver.Config{
		QpiAddr:       "https://qpi.example.com",
		Token:         "your-driver-token",
		Name:          "my-qpu",
		CaFingerprint: "sha256-of-the-server-root-ca",
	}); err != nil {
		log.Fatal(err)
	}
}
```

- `HandleEvent` acts on inbound events, switching on `event.Type`.
- `Emit` sends an event upward (best-effort; dropped if nothing is listening).
- `Every(interval, fn)` runs a callback on a timer, for drivers that report on
  their own schedule rather than in reply to a dispatch.
- `Run` performs the handshake, opens the transport, and blocks until the
  process is signalled (SIGINT/SIGTERM) or `Stop` is called.

## Built-in drivers

Officially maintained drivers live in their own packages under the `qpi-driver`
CLI module (e.g. `qpi-driver/go/qpi-driver/bluefors`), so they are only compiled
into your binary if you import them — the Go equivalent of the Python
`qpi-driver[<extra>]` optional dependencies.

The Bluefors Gen. 1 cryostat monitor is a report-only driver that polls the
Bluefors Remote Access Control API and emits `CryostatReading` events:

```go
package main

import (
	"log"

	qpidriver "github.com/sopherapps/qpi/qpi-driver/go"
	"github.com/sopherapps/qpi/qpi-driver/go/qpi-driver/bluefors"
)

func main() {
	monitor := bluefors.New(bluefors.Options{
		BaseURL:  "http://localhost:49099",
		Channels: bluefors.ParseChannels("mapper.bf.tmc:K,mapper.bf.pmc:mbar"),
	})
	if err := qpidriver.Run(monitor, qpidriver.Config{
		QpiAddr:       "https://qpi.example.com",
		Token:         "your-driver-token",
		Name:          "cryostat-1",
		CaFingerprint: "sha256-of-the-server-root-ca",
	}); err != nil {
		log.Fatal(err)
	}
}
```

## Running a built-in from the CLI

The officially maintained built-ins also ship as a `qpi-driver` CLI (cobra),
mirroring the Python CLI: one `start` verb, `--operation` saying what the driver
does, `--device` selecting the backend within it, and a device's own settings
passed as repeatable `-o key=value`.

```
go install github.com/sopherapps/qpi/qpi-driver/go/qpi-driver@latest

qpi-driver start --operation monitor --device bluefors_gen1 \
  --qpi-addr https://qpi.example.com --token your-driver-token \
  --ca-fingerprint sha256-of-the-server-root-ca --name cryostat-1 \
  -o base_url=http://localhost:49099 \
  -o channels=mapper.bf.tmc:K,mapper.bf.pmc:mbar
```

Universal flags (`--qpi-addr/-a`, `--token/-t`, `--name/-n`, `--device/-d`,
`--ca-file`, `--ca-fingerprint`, `--recv-timeout-ms`) also read the matching
`QPI_*` environment variables, so `install-systemd.sh` can pass the token as
`QPI_ACCESS_TOKEN`.

`qpi-driver devices` lists the operations, their devices and every `-o` key each
one reads, with its type and default; `qpi-driver catalog --json` is the same
catalog for another program to read. Both are generated from the device specs, so
they cannot fall behind the code — including for a device of your own.

## Adding a device of your own

Go has no runtime import by name, so extension is compile-time — and the SDK makes
that the whole story rather than pretending otherwise. Describe your device as a
`devices.DeviceSpec`, register it, and hand off to the SDK's CLI: your binary then
has the same `start`/`devices`/`catalog` commands, the same generated help, and the
same option validation the built-in devices get.

```go
package main

import (
	"log"

	qpidriver "github.com/sopherapps/qpi/qpi-driver/go"
	"github.com/sopherapps/qpi/qpi-driver/go/cli"
	"github.com/sopherapps/qpi/qpi-driver/go/devices"
)

// ThermometerDriver is your driver: embed Base, implement HandleEvent.
type ThermometerDriver struct {
	qpidriver.Base
	probes int
}

func (d *ThermometerDriver) HandleEvent(qpidriver.Event) {}

// Spec describes it as data — which is what earns it a line in `--help`, an entry
// in `catalog --json`, and `-o` values that are checked and converted for you.
var Spec = devices.DeviceSpec{
	Name:      "thermometer",
	Operation: devices.Monitor,
	Summary:   "Reads a made-up thermometer.",
	Options: []devices.OptionSpec{{
		Key: "probes", Help: "How many probes to read.",
		Type: "int", Parse: devices.AsInt, Default: "1", Example: "4",
	}},
	Build: func(cfg qpidriver.Config, opts devices.Options) (qpidriver.Driver, error) {
		return &ThermometerDriver{probes: opts.Int("probes")}, nil
	},
}

func main() {
	if err := devices.Register(Spec); err != nil {
		log.Fatal(err)
	}
	cli.Execute() // the SDK's own CLI, now including your device
}
```

Two things worth knowing:

- **`Build` returns a driver; it does not run one.** `qpidriver.Run` is the only
  thing that starts anything, which is what lets a device be built and asserted on
  in a test with no server (RFC 0003 §7). `Driver` is sealed by an unexported
  method, so `Build` can only return something embedding `Base` — deliberately, so
  `Run` can always reach the transport.
- **Declaring an option is what gets it checked.** `Parse` runs in one place, so no
  builder hand-rolls `strconv`; an `-o` key no device declares is an error naming
  the ones that exist, rather than being silently ignored. Set
  `AcceptsAnyOption: true` only for a device that genuinely has no schema.

There is no import-path device in Go, as there is in Python (`--device
mylab.devices:PrestoV2`): Go resolves imports at compile time, so naming a type in
a string could only work by building a plugin, and `devices.Register` in your own
`main` is both simpler and type-checked.
