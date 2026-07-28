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
exposed as the package's `bin`. It mirrors the Python CLI: the operation is the
subcommand, the device selects the backend, and a device's own settings are
passed as repeatable `-o key=value`.

```
npm install -g qpi-driver          # or: npx -y qpi-driver …

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