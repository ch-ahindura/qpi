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
> **What this SDK ships:** one device, `bluefors_gen1`, a cryostat monitor. It ships
> no QPU — running one means the Python SDK (`pip install "qpi-driver[cli]"`) or a
> device of your own, written as described below. `qpi-driver start --operation
> process` says so plainly rather than offering a device it has not got.

Two words appear throughout, and the difference between them is the whole of how a
driver is described:

- An **operation** is what a driver *does*, and it is a contract with the server:
  `process` runs jobs pushed to it and returns results (a QPU); `monitor` reports
  readings upward on its own schedule (a cryostat, say); `calibrate` executes
  calibration routines to track parameter drift (a tuner). QPI-UI must have a handler
  for each, so there are exactly these three. This SDK ships a device for `monitor`
  only.
- A **device** is the particular backend implementing an operation —
  `bluefors_gen1` is a `monitor` device. Anyone can add one; that is what "Adding a
  device of your own" below is about.

A driver is one operation running on one device.


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
- `Run` does the setup — fetches and verifies the server's root certificate,
  registers this driver over HTTP, opens the two sockets events travel on — and then
  blocks until the process is signalled (SIGINT/SIGTERM) or `Stop` is called. Until
  `Run` is called a driver has touched no network at all.

## Built-in drivers

Officially maintained drivers live in their own packages under the `qpi-driver`
CLI module (e.g. `qpi-driver/go/qpi-driver/bluefors`), so they are only compiled
into your binary if you import them — the Go equivalent of the Python
`qpi-driver[<extra>]` optional dependencies.

The Bluefors Gen. 1 cryostat monitor is a report-only driver that polls the
Bluefors Remote Access Control API and emits `CryostatReading` events:

<!-- docs-check: compile=go-bluefors -->
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

Those flags before the `-o` ones are the same for every device — where the server is,
which token identifies this driver, which certificate to trust. Each also reads a
matching `QPI_*` environment variable (`--qpi-addr` / `QPI_ADDR`, `--token` /
`QPI_ACCESS_TOKEN`, and so on), which is how `install-systemd.sh` keeps the token out
of the unit file's command line.

`qpi-driver devices` prints the device names this build can run, grouped by
operation. There is no default, so `--device` is always given.

### Where a device's `-o` options are documented

Not here, and not in the SDK. Each device decides for itself which `-o` keys it
understands, simply by reading them — there is no list of them anywhere in this
module. The list an operator needs lives in QPI-UI: registering a driver in the
dashboard presents the chosen device's options as a form, and generates the
`qpi-driver start …` command with the values filled in. Copy that command into the
unit file. Keeping a second list here would only give the two something to disagree
about (RFC 0003 §9).

If you pass an `-o` key the device turns out not to read, `qpi-driver start` says so
and exits 1 — before it opens any connection to the server:

```
Error: unknown option "base_urll" for monitor device "bluefors_gen1"
```

It has to wait until the device has been constructed to know that, since being read
is the only evidence a key means anything. Nothing is lost by waiting: constructing a
device touches no network, so the check still happens before the driver is running.
Reporting it at all is the point — a mistyped option in a unit file used to be
ignored, which meant a driver silently running on a default nobody chose.

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
`go install`. If the node has no Go toolchain, the installer downloads one into
`/usr/local/go` — `GO_VERSION` says which.

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

A **device** is the thing `--device` names. To this SDK it is three fields — a name,
an operation, and a *builder*: a function the CLI calls to construct your driver from
the `-o` options and the connection settings. The builder returns the driver rather
than running it; `qpidriver.Run` is what starts it.

Go resolves imports at compile time, so a device cannot be named in a string and
loaded — extension here is compile-time, and the SDK makes that the whole story rather
than pretending otherwise. Your own `main` registers a `devices.DeviceSpec` and hands
off to the SDK's CLI, and your binary then has the same `start`/`devices`/`version`
commands the stock one does, with your device among them.

There are two shapes this takes, and which one you want depends on whether the SDK
already ships something close to what you need.

### 1. Reuse a driver the SDK ships, with your part plugged in

The `bluefors` package already does most of what any cryostat monitor does: it polls on
a timer, tolerates one channel failing without losing the rest of the tick, and emits
a `CryostatReading` event. The only part specific to Gen. 1 is the HTTP call that reads
one channel. So `bluefors.Options` takes that part as a field — `ReadChannel` — and a
monitor for Bluefors Gen. 2, whose control software has a different API, supplies its
own and gets the rest for nothing.

Go has no inheritance, and this needs none: the reusable driver takes the replaceable
part as a value. (It is the same arrangement the Python SDK's QPU uses — every
`process` device is the one shipped QPU driver holding a different `Executor`.)

<!-- docs-check: compile=go-extend-device -->
```go
package main

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"time"

	qpidriver "github.com/sopherapps/qpi/qpi-driver/go"
	"github.com/sopherapps/qpi/qpi-driver/go/cli"
	"github.com/sopherapps/qpi/qpi-driver/go/devices"
	"github.com/sopherapps/qpi/qpi-driver/go/qpi-driver/bluefors"
)

// gen2Reader reads one channel from Gen. 2 Control Software, which has its own API.
func gen2Reader(baseURL, apiKey string) func(channel, unit string) bluefors.Reading {
	client := &http.Client{Timeout: 5 * time.Second}

	return func(channel, unit string) bluefors.Reading {
		// Never panic and never give up on the tick: an ERROR reading is how the
		// driver knows to carry on with the other channels.
		fail := bluefors.Reading{Unit: unit, Status: "ERROR"}

		req, err := http.NewRequest(http.MethodGet,
			fmt.Sprintf("%s/api/v2/values/%s", baseURL, channel), nil)
		if err != nil {
			log.Printf("[bluefors_gen2] bad request for %s: %v", channel, err)
			return fail
		}
		req.Header.Set("Authorization", "Bearer "+apiKey)

		resp, err := client.Do(req)
		if err != nil {
			log.Printf("[bluefors_gen2] failed to read %s: %v", channel, err)
			return fail
		}
		defer func() { _ = resp.Body.Close() }()

		var body struct {
			Value float64 `json:"value"`
			State string  `json:"state"`
		}
		if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
			log.Printf("[bluefors_gen2] unreadable response for %s: %v", channel, err)
			return fail
		}
		return bluefors.Reading{Value: &body.Value, Unit: unit, Status: body.State}
	}
}

// Gen2Spec is the name `--device` will use, bound to the builder behind it.
var Gen2Spec = devices.DeviceSpec{
	Name:      "bluefors_gen2",
	Operation: devices.Monitor,
	Build: func(cfg qpidriver.Config, opts *devices.Options) (qpidriver.Driver, error) {
		// Reading an option is what makes it an option of this device. The second
		// argument is the value used when `-o` does not mention it.
		monitor := bluefors.New(bluefors.Options{
			Channels: bluefors.ParseChannels(
				opts.Require("channels", "gen2.temperature:K")),
			PollInterval: opts.Seconds("poll_interval", 5*time.Second),
			ReadChannel: gen2Reader(
				opts.String("base_url", "http://127.0.0.1:49099"),
				opts.String("api_key", ""),
			),
		})
		// One check, after every read: opts.Err() carries the first value that would
		// not convert, and Require's complaint if `channels` was missing.
		if err := opts.Err(); err != nil {
			return nil, err
		}
		return monitor, nil
	},
}

func main() {
	if err := devices.Register(Gen2Spec); err != nil {
		log.Fatal(err)
	}
	cli.Execute() // the SDK's own CLI, now including your device
}
```

`opts.Require` is for the one kind of option nothing can invent a value for — which
channels a given cryostat exposes depends on how its mappers are configured, so there
is no sensible default and omitting it is an error rather than a guess.

Build your binary and run it exactly as you would the stock one:

```bash
qpi-driver start --operation monitor --device bluefors_gen2 \
  --qpi-addr https://qpi.example.com --token … --ca-fingerprint … \
  -o channels=gen2.temperature:K -o base_url=http://localhost:49099
```

### 2. Write a device from scratch

When nothing shipped is close, write the driver yourself: embed `qpidriver.Base`,
which owns the connection and the event loop, and implement `HandleEvent` — called for
every event the server pushes to this driver. Schedule whatever reports upward with
`Every`.

<!-- docs-check: compile=go-custom-device -->
```go
package main

import (
	"fmt"
	"log"
	"time"

	qpidriver "github.com/sopherapps/qpi/qpi-driver/go"
	"github.com/sopherapps/qpi/qpi-driver/go/cli"
	"github.com/sopherapps/qpi/qpi-driver/go/devices"
)

// ThermometerDriver is your driver. Embedding Base is not optional: it is what makes
// this a driver the SDK can connect and run.
type ThermometerDriver struct {
	qpidriver.Base
	probes int
}

// HandleEvent is called for every event the server pushes to this driver. A monitor
// is told nothing and asked nothing — it only reports — so log and drop, and an event
// sent here by mistake is visible rather than silently swallowed.
func (d *ThermometerDriver) HandleEvent(event qpidriver.Event) {
	log.Printf("[thermometer] not a QPU; dropping %s", event.Type)
}

func (d *ThermometerDriver) report() {
	readings := map[string]any{}
	for probe := 0; probe < d.probes; probe++ {
		readings[fmt.Sprintf("probe.%d", probe)] = map[string]any{
			"value": 4.2, "unit": "K", "status": "OK",
		}
	}
	payload := map[string]any{"readings": readings}
	if err := d.Emit(qpidriver.NewEvent(
		qpidriver.CryostatReading, d.DriverName(), payload)); err != nil {
		log.Printf("[thermometer] emit failed: %v", err)
	}
}

var Spec = devices.DeviceSpec{
	Name:      "thermometer",
	Operation: devices.Monitor,
	Build: func(cfg qpidriver.Config, opts *devices.Options) (qpidriver.Driver, error) {
		driver := &ThermometerDriver{probes: opts.Int("probes", 1)}
		if err := opts.Err(); err != nil {
			return nil, err
		}
		// Runs every interval once the driver is connected, and not before.
		driver.Every(opts.Seconds("interval", 5*time.Second), driver.report)
		return driver, nil
	},
}

func main() {
	if err := devices.Register(Spec); err != nil {
		log.Fatal(err)
	}
	cli.Execute()
}
```

The **operation** you choose is not free: it is a contract the server implements, so
it has to be one QPI-UI already has a handler for. `devices.Monitor` means "reports
readings upward on its own schedule"; `devices.Process` means "runs jobs pushed to it
and returns results". This SDK ships no `process` device — writing a QPU in Go means
implementing job execution yourself, where the Python SDK gives you the whole QPU
driver and asks only for an executor.

### Two things worth knowing

- **`Build` returns a driver; it does not run one.** `qpidriver.Run` is what connects
  and starts the timers, and the CLI is what calls it. That is what lets a device be
  constructed and asserted on in a test with no server (RFC 0003 §7). `Driver` is
  sealed by an unexported method, so `Build` can only return something embedding
  `Base` — deliberately, so `Run` can always reach the transport.
- **Reading an option is what declares it.** There is no list of a device's options in
  this SDK — `Options` records which keys your builder asked for, and `qpi-driver
  start` reports any `-o` key that nothing asked for, then exits 1 without connecting.
  Conversion failures accumulate in `opts.Err()` rather than being returned one at a
  time, so a builder stays a flat run of statements with one check at the end. If your
  device passes options on to something the SDK has never seen, call `opts.Remaining()`
  to take everything not yet read, which counts as reading it.

There is no import-path device in Go, as there is in Python (`--device
mylab.devices:PrestoV2`) and TypeScript (`--device ./dist/mine.js#Export`): naming a
type in a string could only work by building a plugin, and `devices.Register` in your
own `main` is both simpler and type-checked.
