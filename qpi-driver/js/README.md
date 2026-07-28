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
> **What this SDK ships:** one `monitor` device, `bluefors_gen1`. It ships no
> `process` (QPU) device — running a QPU means the Python SDK
> (`pip install "qpi-driver[cli]"`) or a device of your own, registered as below.
> `qpi-driver start --operation process` says so rather than offering a device it
> does not have.


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
- `run` performs the handshake, opens the transport, and resolves once `stop()`
  is called or the process receives SIGINT/SIGTERM.

## Built-in drivers

Officially maintained drivers ship as separate sub-modules, so they are only
pulled into your bundle when imported — the bundler equivalent of the Python
`qpi-driver[<extra>]` optional dependencies.

The Bluefors Gen. 1 cryostat monitor polls the Bluefors Remote Access Control
API and emits `CryostatReading` events:

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

Universal flags (`--qpi-addr/-a`, `--token/-t`, `--device/-d`,
`--ca-file`, `--ca-fingerprint`) also read the matching `QPI_*` environment
variables, so `install-systemd.sh` can pass the token as `QPI_ACCESS_TOKEN`.
`--ca-fingerprint` is **required**: there is no code path that connects without
verifying the pinned root CA, because an opt-out reachable by leaving an argument
out is one a copy-pasted command hits by accident.

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

A driver becomes runnable by the CLI by being described as a device. Either register
it, or name it by import path — the same two routes the Python SDK has, minus
entry points, which npm has no equivalent of.

**Register it.** Describe the device as data, and the CLI gains it: a line in
`--help`, an entry in `catalog --json`, and `-o` values that are checked and
converted for you.

```typescript
import { QpiDriver } from "qpi-driver";
import { asInt, Operation, registerDevice } from "qpi-driver/devices";

class ThermometerDriver extends QpiDriver {
  handleEvent(): void {}
}

export const THERMOMETER = {
  name: "thermometer",
  operation: Operation.Monitor,
  summary: "Reads a made-up thermometer.",
  options: [
    { key: "probes", help: "How many probes to read.", type: "int",
      parse: asInt, default: "1", example: "4" },
  ],
  build: (config, options) =>
    new ThermometerDriver({ ...config, probes: options.int("probes") }),
};

registerDevice(THERMOMETER);
```

**Or name it by import path**, with nothing to register:

```bash
qpi-driver start --operation monitor --device ./dist/my-device.js#THERMOMETER \
  --token … --ca-fingerprint … -o probes=4
```

The separator is `#`, where the Python SDK uses `:` (`--device mylab.devices:Presto`).
That is not gratuitous: `:` is a URL scheme separator in a JavaScript module
specifier, so `./m.js:X` could not be told from a URL. `#` cannot appear in a bare
identifier either, so a plain `--device bluefors` is still read as a name and a typo
still gets the known-devices error rather than an import failure.

The export may be a `DeviceSpec` — which is how it gets a declared option schema and
a `--help` entry — or a builder, in which case the operation comes from
`--operation` and its `-o` options are passed through as typed, unchecked. The path
is resolved relative to the working directory, and must be a module Node can import,
so point at built `.js`, not `.ts`.

`build` returns a driver; it does not run one. `driver.run()` is the only thing that
starts anything, which is what lets a device be built and asserted on with no
server.