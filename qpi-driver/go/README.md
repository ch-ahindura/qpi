# qpi-driver (Go SDK)

The Go SDK for building [QPI](https://github.com/sopherapps/qpi) drivers — the
external processes that exchange typed events with QPI-UI (RFC 0001). It mirrors
the Python SDK (`qpi-driver/py`) and the TypeScript SDK (`qpi-driver/js`): the
same event envelope, the same `drivers/connect` handshake, and TLS with the
pinned root CA.

```
go get github.com/sopherapps/qpi/qpi-driver/go
```

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
mirroring the Python CLI: the operation is the subcommand, the device selects
the backend, and a device's own settings are passed as repeatable `-o
key=value`.

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