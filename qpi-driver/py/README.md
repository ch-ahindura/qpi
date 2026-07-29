# qpi-driver (Python SDK)

The Python SDK for building [QPI](https://github.com/sopherapps/qpi) drivers — the
external processes that exchange typed events with QPI-UI (RFC 0001). It mirrors
the Go SDK (`qpi-driver/go`) and the TypeScript SDK (`qpi-driver/js`): the
same event envelope, the same `drivers/connect` handshake, and TLS with a *pinned*
root CA — the driver fetches the server's root certificate and refuses it unless
its SHA-256 matches the `--ca-fingerprint` the operator was handed out of band.

> **Upgrading from a pre-RFC-0003 release?** The CLI grammar and some SDK APIs
> changed, and every removal is listed with its replacement in the
> [change log](https://github.com/sopherapps/qpi/blob/main/CHANGELOG.md).
> **What this SDK ships:** five QPU backends — `mock`, `presto`, `qiskit_aer`,
> `quantify`, `qblox` — and one cryostat monitor, `bluefors_gen1`. It is the only SDK
> of the three that can run a QPU at all, and the only one where a device can be added
> without recompiling or rebundling.

Two words appear throughout, and the difference between them is the whole of how a
driver is described:

- An **operation** is what a driver *does*, and it is a contract with the server:
  `process` runs jobs pushed to it and returns results (a QPU); `monitor` reports
  readings upward on its own schedule (a cryostat, say). QPI-UI must have a handler
  for each, so there are exactly these two.
- A **device** is the particular backend implementing an operation —
  `qblox` is a `process` device. Anyone can add one; that is what "Adding a
  device of your own" below is about.

A driver is one operation running on one device.


## Install

### Base package (mock executor)

```bash
pip install qpi-driver
```

### With CLI support

```bash
pip install "qpi-driver[cli]"
```

### With Qiskit Aer simulator

```bash
pip install "qpi-driver[aer]"
```

### With Quantify/Qblox hardware support

```bash
pip install "qpi-driver[quantify]"
```

Requires **Python ≥ 3.12, < 3.13**.

---

## Quick Start

### CLI

```bash
# Connect a mock QPU to the server
qpi-driver start --operation process \
  --qpi-addr http://localhost:8090 \
  --token <qpu-access-token> \
  --ca-fingerprint <fingerprint> \
  --device mock \
  -o data_dir=./data
```

Environment variables are also supported for the universal flags:

```bash
export QPI_ADDR=http://localhost:8090
export QPI_ACCESS_TOKEN=<token>
export QPI_CA_FINGERPRINT=<fingerprint>
export QPI_DEVICE=mock
qpi-driver start --operation process
```

### systemd Service (Linux)

To run `qpi-driver` persistently on a Linux machine in the background, you can use `systemd`. 

We have provided a standalone interactive bash installer that automates the entire process (installing `uv`, installing the `qpi-driver` tool, prompting for your tokens/addresses, and registering the systemd service):

```bash
# Run the interactive systemd installer script directly via curl
sudo bash -c "$(curl -LsSf https://raw.githubusercontent.com/sopherapps/qpi/main/qpi-driver/py/install-systemd.sh)"
```

Alternatively, you can run the installer non-interactively by specifying all environment variables:
```bash
curl -LsSf https://raw.githubusercontent.com/sopherapps/qpi/main/qpi-driver/py/install-systemd.sh | sudo \
  QPI_DRIVER_VERSION="" \
  QPI_TOKEN="<your-qpi-access-token>" \
  QPI_ADDR="http://127.0.0.1:8090" \
  CA_FINGERPRINT="<fingerprint>" \
  SERVICE_NAME="rigetti-aspen-1" \
  OPERATION="process" \
  DEVICE="qblox" \
  bash
```

#### Manual systemd Installation
If you prefer to configure it manually, follow these steps:

1. **Install `uv`** (a fast Python package installer):
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   source $HOME/.local/bin/env
   ```

2. **Install `qpi-driver` as a tool**:
   Make sure to specify the correct extras (e.g. `[cli,qblox]`, `[cli,aer]`):
   ```bash
   uv tool install "qpi-driver[cli,qblox]"
   ```

3. **Create the systemd unit file**:
   Replace the placeholder `<values>` with your actual configuration.
   ```bash
   sudo bash -c 'cat > /etc/systemd/system/rigetti-aspen-1.qpi-driver.service <<EOF
   [Unit]
   Description=QPI Driver Service (rigetti-aspen-1)
   After=network.target

   [Service]
   Type=simple

   Environment="QPI_ACCESS_TOKEN=<your-qpi-access-token>"
   Environment="QPI_CA_FILE=/var/qpi-driver/rigetti-aspen-1/qpi.ca.pem"
   Environment=PYTHONUNBUFFERED=1

   ExecStart=/home/<user>/.local/bin/qpi-driver start --operation process \
           --ca-fingerprint <your-fingerprint> \
           --qpi-addr <your-qpi-server-address> \
           --device "qblox" \
           -o data_dir=/var/qpi-driver/rigetti-aspen-1 \
           -o quantify_device_config=/var/qpi-driver/rigetti-aspen-1/quantify.device.yml \
           -o quantify_hardware_config=/var/qpi-driver/rigetti-aspen-1/quantify.hardware.json

   Restart=on-failure
   User=<user>

   StandardOutput=journal
   StandardError=journal
   SyslogIdentifier=rigetti-aspen-1.qpi-driver

   [Install]
   WantedBy=multi-user.target
   EOF'
   ```

4. **Start and enable the service**:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable rigetti-aspen-1.qpi-driver.service
   sudo systemctl start rigetti-aspen-1.qpi-driver.service
   sudo systemctl status rigetti-aspen-1.qpi-driver.service
   ```

### Python API

```python
from pathlib import Path

from qpi_driver import QpuDriver

QpuDriver(
    qpi_addr="http://localhost:8090",
    token="<qpu-access-token>",
    ca_fingerprint="<fingerprint>",
    executor="mock",
    data_dir=Path("./data"),
).run()
```

## Adding a device of your own

A **device** is the thing `--device` names. To this SDK it is three fields — a name,
an operation, and a *builder*: a function the CLI calls to construct your driver from
the `-o` options and the connection settings. The builder returns the driver rather
than running it; the CLI calls `.run()` afterwards.

There are three shapes this takes. The first two apply to any SDK; the third is what
makes writing a QPU in Python much less work than in Go or TypeScript.

### 1. Reuse a driver the SDK ships, with your part plugged in

The Bluefors Gen. 1 monitor already does most of what any cryostat monitor does: it
polls on a timer, tolerates one channel failing without losing the rest of the tick,
and emits a `CryostatReading` event. The only part specific to Gen. 1 is the HTTP call
that reads one channel. So it takes that part as an argument — a `ChannelReader` — and
a monitor for Bluefors Gen. 2, whose control software has a different API, supplies its
own and gets the rest for nothing.

Nothing is subclassed. The reusable driver holds the replaceable part as a value, which
is the same arrangement `QpuDriver` uses for executors (see 3 below).

```python
import logging

import requests

from qpi_driver import DeviceSpec, Operation, register
from qpi_driver.builtins.bluefors_gen1 import BlueforsGen1Driver, parse_channels

log = logging.getLogger(__name__)


def gen2_reader(base_url: str, api_key: str):
    """Reads one channel from Gen. 2 Control Software, which has its own HTTP API."""

    def read(channel: str, unit: str) -> dict:
        try:
            resp = requests.get(
                f"{base_url}/api/v2/values/{channel}",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=5,
            )
            resp.raise_for_status()
            body = resp.json()
            return {"value": float(body["value"]), "unit": unit, "status": body["state"]}
        except Exception:
            # Never raise: one unreadable channel must not lose the whole tick. An
            # ERROR reading is how the driver knows to carry on with the others.
            log.exception("failed to read channel %s", channel)
            return {"value": None, "unit": unit, "status": "ERROR"}

    return read


def build_gen2(*, options, **transport) -> BlueforsGen1Driver:
    # Reading an option is what makes it an option of this device. The second
    # argument is the value used when -o does not mention it.
    return BlueforsGen1Driver(
        channels=parse_channels(options.require("channels", "gen2.temperature:K")),
        poll_interval=options.get_float("poll_interval", 5.0),
        read_channel=gen2_reader(
            options.get_str("base_url", "http://127.0.0.1:49099"),
            options.get_str("api_key"),
        ),
        **transport,
    )


BLUEFORS_GEN2 = DeviceSpec(
    name="bluefors_gen2", operation=Operation.MONITOR, build=build_gen2
)
register(BLUEFORS_GEN2)
```

`options.require` is for the one kind of option nothing can invent a value for — which
channels a given cryostat exposes depends on how its mappers are configured, so there
is no sensible default and omitting it is an error rather than a guess. `**transport`
is the connection settings (`qpi_addr`, `token`, `ca_fingerprint`, `ca_file_path`,
`recv_timeout_ms`); a builder that has nothing to say about them passes them straight
through.

### 2. Write a device from scratch

When nothing shipped is close, subclass `QpiDriver` — the SDK's own base class, which
owns the connection and the event loop. There are two things to fill in: `handle_event`,
called for every event the server pushes to this driver, and whatever you schedule with
`every()` to report upward on a timer.

```python
import logging

from qpi_driver import DeviceSpec, Event, EventType, Operation, QpiDriver, register

log = logging.getLogger(__name__)


class ThermometerDriver(QpiDriver):
    def __init__(self, probes: int = 1, interval: float = 5.0, **transport):
        super().__init__(**transport)
        self.probes = probes
        # Runs every `interval` seconds once the driver is connected, not before.
        self.every(interval, self._report)

    def handle_event(self, event: Event) -> None:
        """A monitor is told nothing and asked nothing: it only reports.

        Log and drop, so an event sent here by mistake is visible rather than
        silently swallowed.
        """
        log.warning("not a QPU; dropping %s", event.type.value)

    def _report(self) -> None:
        readings = {
            f"probe.{probe}": {"value": 4.2, "unit": "K", "status": "OK"}
            for probe in range(self.probes)
        }
        self.emit(
            Event(
                type=EventType.CRYOSTAT_READING,
                driver=self.name,
                payload={"readings": readings},
            )
        )


def build_thermometer(*, options, **transport) -> ThermometerDriver:
    return ThermometerDriver(
        probes=options.get_int("probes", 1),
        interval=options.get_float("interval", 5.0),
        **transport,
    )


THERMOMETER = DeviceSpec(
    name="thermometer", operation=Operation.MONITOR, build=build_thermometer
)
register(THERMOMETER)
```

The **operation** you choose is not free: it is a contract the server implements, so it
has to be one QPI-UI already has a handler for. `Operation.MONITOR` means "reports
readings upward on its own schedule"; `Operation.PROCESS` means "runs jobs pushed to it
and returns results" — and for that one, this SDK does the work for you:

### 3. A QPU: write an executor, not a driver

For `process` you never write a driver at all. Every `process` device is the one
built-in `QpuDriver` holding a different **executor**, which is the same arrangement as
1 above — and the reason it is worth knowing is that `QpuDriver` is not a small thing
to reimplement: it runs jobs in a worker subprocess so a crashing backend cannot take
down the receive loop, pumps results back on a thread, and turns each one into a
`JobResult` event.

`Executor` asks for two methods. `execute()` runs the job on your hardware and returns
an `xr.Dataset`; `process_result()` turns that into the Qiskit-shaped counts QPI-UI
stores.

```python
import numpy as np
import xarray as xr

from qpi_driver import Executor, JobPayload
from qpi_driver.builtins.qpu import device_spec


class QuantumXExecutor(Executor):
    def __init__(self, name: str = "quantum_x", qubit_count: int = 1, **options):
        super().__init__(name=name)
        self.qubit_count = int(qubit_count)

    def execute(self, payload: JobPayload) -> xr.Dataset:
        # Your QPU-specific execution logic; here every shot reads as the ground state.
        memory = np.full(payload.shots, "0" * self.qubit_count)
        return xr.Dataset({"memory": ("shot", memory)}, attrs={"shots": payload.shots})

    def process_result(self, dataset: xr.Dataset, job_id: str) -> dict:
        states, counts = np.unique(dataset["memory"].values, return_counts=True)
        return {
            "job_id": job_id,
            "counts": {str(s): int(c) for s, c in zip(states, counts)},
        }


# `device_spec` writes the spec for you: the name --device will use, bound to the
# built-in QPU driver holding your executor. `pass_through=True` hands the executor's
# constructor the -o keys the SDK knows nothing about, like qubit_count.
QUANTUM_X = device_spec("quantum_x", executor=QuantumXExecutor, pass_through=True)
```

You can also skip the spec entirely and hold the executor yourself:
`QpuDriver(executor=QuantumXExecutor(qubit_count=4)).run()`. Reach for that when you
are driving the SDK from your own Python and want no CLI; reach for a device when
anything else — a systemd unit, the dashboard's generated command — has to *launch* it.

### Getting your device to `--device`

Three routes, and all three end in the same registry.

**Register it at import** — `register(SPEC)`, as 1 and 2 above do. This only works if
your code runs before the CLI parses arguments, so it is for a program of your own that
calls into the SDK.

**Ship it as an installable distribution**, which is the route for a device meant to be
deployed. Advertise the spec under the `qpi_driver.devices` entry-point group in your
own `pyproject.toml` — this is the whole of the packaging:

```toml
[project.entry-points."qpi_driver.devices"]
quantum_x = "mylab_devices:QUANTUM_X"
```

```bash
pip install .    # your distribution, depending on qpi-driver[cli]
qpi-driver start --operation process --device quantum_x -o qubit_count=4 ...
```

`quantum_x` is now indistinguishable from a built-in: `qpi-driver devices` lists it and
`--device` accepts it, with nothing changed in the SDK. An entry point that will not
import, or that resolves to something other than a `DeviceSpec`, is logged and skipped —
one broken third-party package cannot stop the CLI from starting.

**Or name it by import path**, with nothing to install and nothing to register:

```bash
qpi-driver start --operation process --device mylab_devices:QuantumXExecutor \
  -o qubit_count=4 ...
```

`module.attr` works as well as `module:attr`. What the imported object has to be is
whatever a device means for that operation: for `process` an `Executor` subclass or
instance, for `monitor` a builder. Either also accepts a `DeviceSpec`, which is how it
gets a name of its own instead of being known by the path that imported it.

An executor named this way is handed every `-o` key the SDK does not read itself, as
the string it was typed as — so `qubit_count` arrives as `"4"` and a typo in it reaches
your constructor rather than being reported. Use this route to try a device out, and an
entry point to deploy one.

[`examples/custom_device/`](https://github.com/sopherapps/qpi/blob/main/qpi-driver/py/examples/custom_device/)
is route 3 worked all the way through, as two files you can run.

There is no separate mechanism for "a whole custom driver". An operation is a contract
QPI-UI implements server-side, so a custom driver is always a custom device of an
existing operation (RFC 0003 §6, §13.4).

### Two things worth knowing

- **A builder returns a driver; it does not run one.** `.run()` is what connects and
  starts the timers, and the CLI is what calls it. That is what lets a device be
  constructed and asserted on in a test with no server anywhere.
- **Reading an option is what declares it.** There is no list of a device's options in
  this SDK — `Options` records which keys your builder asked for, and `qpi-driver start`
  reports any `-o` key that nothing asked for, then exits 1 without connecting. If your
  device passes options on to something the SDK has never seen, call
  `options.remaining()` to take everything not yet read, which counts as reading it.

Nothing here describes your device *to QPI-UI*. Registering a driver in the dashboard
is where a device is named, configured and documented; this SDK's job is to run the one
it is told to run (RFC 0003 §9).

The TypeScript SDK has the same import-path route with a different separator —
`--device ./dist/my-device.js#MyExport` — because `:` is a URL scheme separator in a
JavaScript module specifier. The Go SDK has no import-path route at all: Go resolves
imports at compile time, so a device there is registered in your own `main`.

---

## Executor Backends

| Backend | Description | Extra |
|---------|-------------|-------|
| `mock` | Qiskit BasicSimulator, needs no hardware | — |
| `qiskit_aer` | Qiskit Aer simulator | `[aer]` |
| `quantify` | Quantify-scheduler + Qblox instruments | `[quantify]` |
| `qblox` | Qblox scheduler (legacy) | `[qblox]` |
| `presto` | Presto control system (not yet implemented) | — |

The one `monitor` device is `bluefors_gen1`, a cryostat monitor for Bluefors Control
Software Gen. 1, shipped by `[bluefors_gen1]`.

---

## Architecture

This is the `process` operation's architecture — a QPU driver. A `monitor` needs
none of it: it has no worker, because it runs no jobs, and just polls on a timer.

Execution happens in a *subprocess* so a heavy or crashing executor can never block
or take down the receive loop. Results come back over a queue and are emitted by a
*thread* in the main process, which is all that is needed to drain a queue.

- **Main process**: the NNG PULL receive loop, plus the result-pump thread that emits
  `JobResult` events on the PUSH socket.
- **Worker subprocess** (`qpu.job_worker`): resolves the executor once, then runs
  queued jobs until told to stop.

```
┌─────────────┐     NNG PUSH      ┌──────────────────────────────┐
│   Server    │ ────────────────> │ Main process                 │
│  Dispatcher │                   │  · PULL receive loop         │
└─────────────┘                   │  · result-pump thread ──┐    │
       ▲                          └───────────┬─────────────│────┘
       │ NNG PUSH                             │             │
       │ (JobResult)                 job queue│             │result queue
       └──────────────────────────────────────│─────────────┘
                                              ▼             ▲
                                   ┌──────────────────────────────┐
                                   │ Worker subprocess            │
                                   │  · resolve_executor()        │
                                   │  · executor.execute(job)     │
                                   └──────────────────────────────┘
```

---

## CLI Reference

A driver is run with one verb, `start`: `--operation` says what it does — `process`
(a QPU) or `monitor` (e.g. a cryostat) — and `--device` which backend within it. The
options below are the same for every device; a device's own settings are passed as
repeatable `-o key=value`.

```
qpi-driver start --operation process|monitor [OPTIONS]

Universal options:
      --operation TEXT    What the driver does: process | monitor [env: QPI_OPERATION]
  -a, --qpi-addr TEXT     QPI server URL [env: QPI_ADDR]
  -t, --token TEXT        Access token identifying the driver [env: QPI_ACCESS_TOKEN]
  -d, --device TEXT       Backend within the operation, e.g. mock, qblox, bluefors_gen1 [env: QPI_DEVICE]
  -o, --option KEY=VALUE  A setting of the chosen device, repeatable
  --ca-file PATH          Path to the CA root certificate [env: QPI_CA_FILE]
  --ca-fingerprint TEXT   Fingerprint pinning the CA root certificate [env: QPI_CA_FINGERPRINT]
  --recv-timeout-ms INT   How long the receive loop blocks per attempt, in ms [env: QPI_RECV_TIMEOUT_MS]
  --help                  Show this message and exit.
```

There is no default device: QPI-UI generates the command that launches a driver, and
that command always names one.

Each device decides for itself which `-o` keys it understands, simply by reading them
— there is no list of them anywhere in this package. The list an operator needs lives
in QPI-UI: registering a driver in the dashboard presents the chosen device's options
as a form, and generates the `qpi-driver start …` command with the values filled in.
Copy that command into the unit file. Keeping a second list here would only give the
two something to disagree about (RFC 0003 §9).

If you pass an `-o` key the device turns out not to read, `qpi-driver start` says so
and exits 1 — before it opens any connection to the server:

```
Error: unknown option 'data_dirr' for process device 'mock'.
```

It has to wait until the device has been constructed to know that, since being read is
the only evidence a key means anything. Nothing is lost by waiting: constructing a
device touches no network, so the check still happens before the driver is running.
Reporting it at all is the point — a mistyped option in a unit file used to be ignored,
which meant a driver silently running on a default nobody chose.

What the SDK *can* answer is which devices this particular install has — after
`pip install`ing a distribution of them, or writing one:

```bash
# Names only, by operation
qpi-driver devices

# Just one operation
qpi-driver devices --operation monitor
```

---

## Documentation

- [Main QPI Repository](https://github.com/sopherapps/qpi)
- [PyPI Project Page](https://pypi.org/project/qpi-driver/)

---

## License

MIT — see the [main repository](https://github.com/sopherapps/qpi/blob/main/LICENSE) for details.
