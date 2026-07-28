# qpi-driver (TypeScript SDK)

The TypeScript/JavaScript SDK for building [QPI](https://github.com/sopherapps/qpi)
drivers — the external processes that exchange typed events with QPI-UI
(RFC 0001). It mirrors the Python SDK (`qpi-driver/py`) and the Go SDK
(`qpi-driver/go`): the same event envelope, the same `drivers/connect`
handshake, and TLS with the pinned root CA.

It has **zero runtime dependencies** — the NNG (nanomsg SP) pipeline is
implemented over Node's built-in `tls`, and the handshake uses the global
`fetch`.

```
npm install qpi-driver
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
  name: "my-qpu",
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
  name: "cryostat-1",
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
  --ca-fingerprint sha256-of-the-server-root-ca --name cryostat-1 \
  -o base_url=http://localhost:49099 \
  -o channels=mapper.bf.tmc:K,mapper.bf.pmc:mbar
```

Universal flags (`--qpi-addr/-a`, `--token/-t`, `--name/-n`, `--device/-d`,
`--ca-file`, `--ca-fingerprint`) also read the matching `QPI_*` environment
variables, so `install-systemd.sh` can pass the token as `QPI_ACCESS_TOKEN`.
`--ca-fingerprint` is **required**: there is no code path that connects without
verifying the pinned root CA, because an opt-out reachable by leaving an argument
out is one a copy-pasted command hits by accident.

`qpi-driver devices` lists the operations, their devices and every `-o` key each one
reads, with its type and default; `qpi-driver catalog --json` is the same catalog for
another program to read. Both are generated from the device specs, so they cannot
fall behind the code — including for a device of your own.

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