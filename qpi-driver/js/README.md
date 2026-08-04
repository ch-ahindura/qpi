# qpi-driver (TypeScript SDK)

The TypeScript/JavaScript SDK for building [QPI](https://github.com/sopherapps/qpi)
drivers — the external processes that exchange typed events with QPI-UI
(RFC 0001). It mirrors the Python SDK (`qpi-driver/py`) and the Go SDK
(`qpi-driver/go`): the same event envelope, the same `drivers/connect`
handshake, and TLS with a *pinned* root CA — the driver fetches the server's root
certificate and refuses it unless its SHA-256 matches the `--ca-fingerprint` the
operator was handed out of band.

It has **zero runtime dependencies** — the NNG (nanomsg SP) pipeline is
implemented over Node's built-in `tls`, and the handshake uses the global
`fetch`.

```
npm install qpi-driver
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

Subclass `QpiDriver`, implement `handleEvent`, and call `run`:

```typescript
import { QpiDriver, Event, EventType } from "qpi-driver";

class MyDriver extends QpiDriver {
  handleEvent(event: Event): void {
    if (event.type === EventType.JobDispatch) {
      const results = runMyBackend(event.payload);
      this.emit(
        new Event(EventType.JobResult, {
          job_id: event.payload.job_id,
          status: "completed",
          results,
        }),
      );
    }
  }
}

await new MyDriver({
  qpiAddr: "https://qpi.example.com",
  token: "your-driver-token",
  caFingerprint: "sha256-of-the-server-root-ca",
}).run();
```

- `handleEvent` acts on inbound events, switching on `event.type`.
- `emit` sends an event upward (best-effort; dropped if nothing is listening).
- `every(intervalMs, fn)` runs a callback on a timer, for drivers that report on
  their own schedule rather than in reply to a dispatch.
- `run` does the setup — fetches and verifies the server's root certificate,
  registers this driver over HTTP, opens the two sockets events travel on — and then
  resolves once `stop()` is called or the process receives SIGINT/SIGTERM. Until
  `run` is called a driver has touched no network at all.

## Built-in drivers

Officially maintained drivers ship as separate sub-modules, so they are only
pulled into your bundle when imported — the bundler equivalent of the Python
`qpi-driver[<extra>]` optional dependencies.

The Bluefors Gen. 1 cryostat monitor polls the Bluefors Remote Access Control
API and emits `CryostatReading` events:

<!-- docs-check: compile=ts-bluefors -->
```typescript
import { QpiDriver } from "qpi-driver";
import { BlueforsGen1Driver } from "qpi-driver/builtins/bluefors-gen1";

await new BlueforsGen1Driver({
  qpiAddr: "https://qpi.example.com",
  token: "your-driver-token",
  caFingerprint: "sha256-of-the-server-root-ca",
  blueforsBaseUrl: "http://localhost:49099",
  channels: { "mapper.bf.tmc": "K", "mapper.bf.pmc": "mbar" },
}).run();
```

## Running a built-in from the CLI

The officially maintained built-ins also ship as a `qpi-driver` CLI (commander),
exposed as the package's `bin`. It mirrors the Python CLI: one `start` verb,
`--operation` saying what the driver does, `--device` selecting the backend within
it, and a device's own settings passed as repeatable `-o key=value`.

```
npm install -g qpi-driver          # or: npx -y qpi-driver …

qpi-driver start --operation monitor --device bluefors_gen1 \
  --qpi-addr https://qpi.example.com --token your-driver-token \
  --ca-fingerprint sha256-of-the-server-root-ca \
  -o base_url=http://localhost:49099 \
  -o channels=mapper.bf.tmc:K,mapper.bf.pmc:mbar
```

Those flags before the `-o` ones are the same for every device — where the server
is, which token identifies this driver, which certificate to trust. Each also reads
a matching `QPI_*` environment variable (`--qpi-addr` / `QPI_ADDR`, `--token` /
`QPI_ACCESS_TOKEN`, and so on), which is how `install-systemd.sh` keeps the token out
of the unit file's command line.
`--ca-fingerprint` is **required**: there is no code path that connects without
verifying the pinned root CA, because an opt-out reachable by leaving an argument
out is one a copy-pasted command hits by accident.

`qpi-driver devices` prints the device names this build can run, grouped by
operation. There is no default, so `--device` is always given.

### Where a device's `-o` options are documented

Not here, and not in the SDK. Each device decides for itself which `-o` keys it
understands, simply by reading them — there is no list of them anywhere in this
package. The list an operator needs lives in QPI-UI: registering a driver in the
dashboard presents the chosen device's options as a form, and generates the
`qpi-driver start …` command with the values filled in. Copy that command into the
unit file. Keeping a second list here would only give the two something to disagree
about (RFC 0003 §9).

If you pass an `-o` key the device turns out not to read, `qpi-driver start` says so
and exits 1 — before it opens any connection to the server:

```
Error: unknown option 'base_urll' for monitor device 'bluefors_gen1'
```

It has to wait until the device has been constructed to know that, since being read
is the only evidence a key means anything. Nothing is lost by waiting: constructing a
device touches no network, so the check still happens before the driver is running.
Reporting it at all is the point — a mistyped option in a unit file used to be
ignored, which meant a driver silently running on a default nobody chose.

## Running a built-in as a systemd service (Linux)

### Using the `install-systemd.sh` script

To keep a driver running on a Linux node, register it with `systemd`. The installer
does the whole job — install Node if it is missing, `npm install -g qpi-driver`,
prompt for the token, address and fingerprint, write the unit file, then enable and
start it:

```bash
sudo bash -c "$(curl -LsSf https://raw.githubusercontent.com/sopherapps/qpi/main/qpi-driver/js/install-systemd.sh)"
```

Or non-interactively, with every answer supplied up front:

```bash
curl -LsSf https://raw.githubusercontent.com/sopherapps/qpi/main/qpi-driver/js/install-systemd.sh | sudo \
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

An empty `QPI_DRIVER_VERSION` installs `qpi-driver@latest`. `DRIVER_OPTIONS` carries
the device's own `-o` settings as `key=value;key=value`. This SDK ships only
`monitor` devices, so that is what `OPERATION` and `DEVICE` default to.
`QPI_SKIP_INSTALL=1` uses a `qpi-driver` already on `PATH` (or `QPI_DRIVER_BIN`)
instead of installing one.

### Doing it by hand

1. **Install Node.js**:

   ```bash
   curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.6/install.sh | bash
   source "$HOME/.nvm/nvm.sh"
   nvm install 24
   ```

2. **Install the CLI**:

   ```bash
   npm install -g qpi-driver
   ```

3. **Write the unit file**, replacing every `<value>`. `ExecStart` needs an absolute
   path, so use the one `which qpi-driver` reports — and because `qpi-driver` is a
   Node script with a `#!/usr/bin/env node` shebang, the unit's `PATH` has to
   contain that `node` too. systemd's default `PATH` does not include an nvm
   install, so name it: `dirname "$(which node)"`.

   ```bash
   sudo bash -c 'cat > /etc/systemd/system/cryostat-1.qpi-driver.service <<EOF
   [Unit]
   Description=QPI Driver Service (cryostat-1)
   After=network.target

   [Service]
   Type=simple

   Environment="QPI_ACCESS_TOKEN=<your-qpi-access-token>"
   Environment="QPI_CA_FILE=/var/qpi-driver/cryostat-1/qpi.ca.pem"
   Environment="PATH=<node-bin-dir>:/usr/local/bin:/usr/bin:/bin"

   ExecStart=<path-to>/qpi-driver start \
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

4. **Enable and start it**:

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
than running it; the CLI calls `driver.run()` afterwards.

There are two shapes this takes, and which one you want depends on whether the SDK
already ships something close to what you need.

### 1. Reuse a driver the SDK ships, with your part plugged in

The Bluefors Gen. 1 monitor already does most of what any cryostat monitor does: it
polls on a timer, tolerates one channel failing without losing the rest of the tick,
and emits a `CryostatReading` event. The only part specific to Gen. 1 is the HTTP call
that reads one channel. So it takes that part as an argument — a `ChannelReader` — and
a monitor for Bluefors Gen. 2, whose control software has a different API, supplies
its own and gets the rest for nothing.

This is the same arrangement the Python SDK's QPU uses: every `process` device is the
one shipped QPU driver holding a different `Executor`. The reusable driver takes the
replaceable part as a value. Nothing is subclassed, and your code cannot be broken by
the driver's internals changing, because it never sees them.

<!-- docs-check: compile=ts-extend-device -->
```typescript
import type { DeviceSpec } from "qpi-driver/devices";
import { Operation, registerDevice } from "qpi-driver/devices";
import {
  BlueforsGen1Driver,
  type ChannelReader,
  parseChannels,
} from "qpi-driver/builtins/bluefors-gen1";

/** Reads one channel from Gen. 2 Control Software, which has its own HTTP API. */
function gen2Reader(baseUrl: string, apiKey: string): ChannelReader {
  return async (channel, unit) => {
    try {
      const resp = await fetch(`${baseUrl}/api/v2/values/${channel}`, {
        headers: { Authorization: `Bearer ${apiKey}` },
        signal: AbortSignal.timeout(5000),
      });
      const body = (await resp.json()) as { value: number; state: string };
      return { value: body.value, unit, status: body.state };
    } catch (err) {
      // Never throw: one unreadable channel must not lose the whole tick. An ERROR
      // reading is how the driver knows to carry on with the others.
      console.error(`[bluefors_gen2] failed to read ${channel}:`, err);
      return { value: null, unit, status: "ERROR" };
    }
  };
}

export const BLUEFORS_GEN2: DeviceSpec = {
  name: "bluefors_gen2",
  operation: Operation.Monitor,
  build: (config, options) =>
    new BlueforsGen1Driver({
      ...config,
      // Reading an option is what makes it an option of this device. The second
      // argument is the value used when `-o` does not mention it.
      channels: parseChannels(
        options.require("channels", "gen2.temperature:K"),
      ),
      pollIntervalMs: options.ms("poll_interval"),
      channelReader: gen2Reader(
        options.str("base_url", "http://127.0.0.1:49099"),
        options.str("api_key"),
      ),
    }),
};

registerDevice(BLUEFORS_GEN2);
```

`options.require` is for the one kind of option nothing can invent a value for —
which channels a given cryostat exposes depends on how its mappers are configured, so
there is no sensible default and omitting it is an error rather than a guess.

Run it exactly as you would a built-in:

```bash
qpi-driver start --operation monitor --device bluefors_gen2 \
  --qpi-addr https://qpi.example.com --token … --ca-fingerprint … \
  -o channels=gen2.temperature:K -o base_url=http://localhost:49099
```

### 2. Write a device from scratch

When nothing shipped is close, subclass `QpiDriver` — the SDK's own base class, which
owns the connection and the event loop. There are two things to fill in: `handleEvent`,
called for every event the server pushes to this driver, and whatever you schedule
with `every()` to report upward on a timer.

<!-- docs-check: compile=ts-register-device -->
```typescript
import { Event, EventType, QpiDriver, type QpiDriverOptions } from "qpi-driver";
import { type DeviceSpec, Operation, registerDevice } from "qpi-driver/devices";

interface ThermometerOptions extends QpiDriverOptions {
  probes: number;
  intervalMs: number;
}

class ThermometerDriver extends QpiDriver {
  private readonly probes: number;

  constructor(options: ThermometerOptions) {
    super(options);
    this.probes = options.probes;
    // Runs every intervalMs once the driver is connected, and not before.
    this.every(options.intervalMs, () => this.report());
  }

  /**
   * A monitor is told nothing and asked nothing: it only reports. Log and drop, so
   * an event sent here by mistake is visible rather than silently swallowed.
   */
  handleEvent(event: Event): void {
    console.warn(`[thermometer] not a QPU; dropping ${event.type}`);
  }

  private report(): void {
    const readings: Record<string, unknown> = {};
    for (let probe = 0; probe < this.probes; probe += 1) {
      readings[`probe.${probe}`] = { value: 4.2, unit: "K", status: "OK" };
    }
    this.emit(new Event(EventType.CryostatReading, { readings }));
  }
}

// The `: DeviceSpec` annotation is what gives `build`'s two parameters their types,
// so a mistake in `options.int(…)` is caught here rather than at the first
// `-o probes=4`.
export const THERMOMETER: DeviceSpec = {
  name: "thermometer",
  operation: Operation.Monitor,
  build: (config, options) =>
    new ThermometerDriver({
      ...config,
      probes: options.int("probes", 1),
      intervalMs: options.ms("interval") ?? 5000,
    }),
};

registerDevice(THERMOMETER);
```

The **operation** you choose is not free: it is a contract the server implements, so
it has to be one QPI-UI already has a handler for. `Operation.Monitor` means "reports
readings upward on its own schedule"; `Operation.Process` means "runs jobs pushed to
it and returns results". This SDK ships no `process` device — writing a QPU in
TypeScript means implementing job execution yourself, where the Python SDK gives you
the whole QPU driver and asks only for an executor.

### Running a device without registering it

`registerDevice` has to run before the CLI parses arguments, which means your code has
to be the entry point. If it is not — you have a module with an exported device in it
and want the shipped `qpi-driver` binary to run it — name the export on the command
line instead:

```bash
qpi-driver start --operation monitor --device ./dist/my-device.js#THERMOMETER \
  --token … --ca-fingerprint … -o probes=4
```

Everything before the `#` is a module path, resolved relative to the directory you run
from; everything after it is the name of an export in that module. It must be a module
Node can `import`, so point at built `.js`, not `.ts`.

The separator is `#`, where the Python SDK uses `:` (`--device mylab.devices:Presto`).
That is not gratuitous: `:` is a URL scheme separator in a JavaScript module
specifier, so `./m.js:X` could not be told apart from a URL. `#` cannot appear in a
bare identifier either, so a plain `--device bluefors` is still read as a registered
name — a typo gets the known-devices error rather than a confusing import failure.

The export can be either of two things:

- **A `DeviceSpec`**, like `THERMOMETER` above. Its own `name` and `operation` are
  used, and `--operation` must agree with the spec's or the CLI says so.
- **A builder function on its own** — the `(config, options) => driver` part, with no
  spec around it. There is then no `name` or `operation` to read, so both come from
  the command line: the operation is whatever `--operation` says, and the device's
  name is the `./dist/my-device.js#MyExport` string itself, which is what appears in
  error messages about it. Use this when you have a factory function and do not want
  to write the three-field object just to name it.

### Two things worth knowing

- **A builder returns a driver; it does not run one.** `driver.run()` is what connects
  and starts the timers, and the CLI is what calls it. That is what lets a device be
  constructed and asserted on in a test with no server anywhere.
- **Reading an option is what declares it.** There is no list of a device's options in
  this SDK — `Options` records which keys your builder asked for, and `qpi-driver
  start` reports any `-o` key that nothing asked for, then exits 1 without connecting.
  If your device passes options on to something the SDK has never seen, call
  `options.remaining()` to take everything not yet read, which counts as reading it.
