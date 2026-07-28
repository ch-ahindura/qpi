# qpi-driver (Go SDK)

The Go SDK for building [QPI](https://github.com/sopherapps/qpi) drivers — the
external processes that exchange typed events with QPI-UI (RFC 0001). It mirrors
the Python SDK (`qpi-driver/py`) and the TypeScript SDK (`qpi-driver/js`): the
same event envelope, the same `drivers/connect` handshake, and TLS with a *pinned*
root CA — the driver fetches the server's root certificate and refuses it unless
its SHA-256 matches the `--ca-fingerprint` the operator was handed out of band.

```
go get github.com/sopherapps/qpi/qpi-driver/go
```

> **Upgrading from a pre-RFC-0003 release?** The CLI grammar and some SDK APIs
> changed, and every removal is listed with its replacement in the
> [change log](https://github.com/sopherapps/qpi/blob/main/CHANGELOG.md).
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
  --ca-fingerprint sha256-of-the-server-root-ca \
  -o base_url=http://localhost:49099 \
  -o channels=mapper.bf.tmc:K,mapper.bf.pmc:mbar
```

Universal flags (`--qpi-addr/-a`, `--token/-t`, `--device/-d`,
`--ca-file`, `--ca-fingerprint`, `--recv-timeout-ms`) also read the matching
`QPI_*` environment variables, so `install-systemd.sh` can pass the token as
`QPI_ACCESS_TOKEN`.

`qpi-driver devices` prints what this build can run: each operation, the devices
under it, and every `-o` key each device reads with its type and default.

That table is the SDK's **catalog** — the list of operations, devices and options a
build knows about, which the `DeviceSpec`s themselves are the source of.
`qpi-driver catalog --json` is the same document for another program to read;
QPI-UI's dashboard builds its setup snippets from it, and a test fails if the
server's idea of the catalog and the SDKs' ever disagree. Because both come from
the specs, neither can fall behind the code — including for a device of your own.

## Running a built-in as a systemd service (Linux)

### Using the `install-systemd.sh` script

To keep a driver running on a Linux node, register it with `systemd`. The installer
does the whole job — `go install` the CLI, prompt for the token, address and
fingerprint, write the unit file, then enable and start it:

```bash
sudo bash -c "$(curl -LsSf https://raw.githubusercontent.com/sopherapps/qpi/main/qpi-driver/go/install-systemd.sh)"
```

Or non-interactively, with every answer supplied up front:

```bash
curl -LsSf https://raw.githubusercontent.com/sopherapps/qpi/main/qpi-driver/go/install-systemd.sh | sudo \
  QPI_DRIVER_VERSION="" \
  QPI_TOKEN="<your-qpi-access-token>" \
  QPI_ADDR="https://qpi.example.com" \
  CA_FINGERPRINT="<fingerprint>" \
  SERVICE_NAME="cryostat-1" \
  OPERATION="monitor" \
  DEVICE="bluefors_gen1" \
  DRIVER_OPTIONS="base_url=http://localhost:49099;channels=mapper.bf.tmc:K,mapper.bf.pmc:mbar" \
  bash
```

An empty `QPI_DRIVER_VERSION` installs `@latest`. `DRIVER_OPTIONS` carries the
device's own `-o` settings as `key=value;key=value`. This SDK ships only `monitor`
devices, so that is what `OPERATION` and `DEVICE` default to. `QPI_SKIP_INSTALL=1`
uses a `qpi-driver` already on `PATH` (or `QPI_DRIVER_BIN`) instead of running
`go install`.

### Doing it by hand

1. **Install the Go toolchain** (https://go.dev/dl/), then the CLI:

   ```bash
   go install github.com/sopherapps/qpi/qpi-driver/go/qpi-driver@latest
   ```

2. **Write the unit file**, replacing every `<value>`:

   ```bash
   sudo bash -c 'cat > /etc/systemd/system/cryostat-1.qpi-driver.service <<EOF
   [Unit]
   Description=QPI Driver Service (cryostat-1)
   After=network.target

   [Service]
   Type=simple

   Environment="QPI_ACCESS_TOKEN=<your-qpi-access-token>"
   Environment="QPI_CA_FILE=/var/qpi-driver/cryostat-1/qpi.ca.pem"

   ExecStart=/home/<user>/go/bin/qpi-driver start \
           --operation monitor \
           --device bluefors_gen1 \
           --ca-fingerprint <your-fingerprint> \
           --qpi-addr <your-qpi-server-address> \
           -o base_url=http://localhost:49099 \
           -o channels=mapper.bf.tmc:K,mapper.bf.pmc:mbar

   Restart=on-failure
   User=<user>

   StandardOutput=journal
   StandardError=journal
   SyslogIdentifier=cryostat-1.qpi-driver

   [Install]
   WantedBy=multi-user.target
   EOF'
   ```

3. **Enable and start it**:

   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable cryostat-1.qpi-driver.service
   sudo systemctl start cryostat-1.qpi-driver.service
   sudo systemctl status cryostat-1.qpi-driver.service
   ```

Logs go to the journal: `journalctl -u cryostat-1.qpi-driver.service -f`.

## Adding a device of your own

Go has no runtime import by name, so extension is compile-time — and the SDK makes
that the whole story rather than pretending otherwise. Describe your device as a
`devices.DeviceSpec`, register it, and hand off to the SDK's CLI: your binary then
has the same `start`/`devices`/`catalog` commands, the same generated help, and the
same option validation the built-in devices get.

<!--We probably need a way to test all documentation example code and samples so that documentation and code does not drift-->
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
