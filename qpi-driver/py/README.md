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
> **What this SDK ships:** five `process` (QPU) devices — `mock`, `presto`,
> `qiskit_aer`, `quantify`, `qblox` — and one `monitor` device, `bluefors_gen1`. It is
> the only SDK of the three with a `process` device, and the only one where a device
> can be added without recompiling or rebundling.


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

There is one thing to write, and it is the same thing whichever operation you are
extending: a **device**. Register it under a name, and `--device <name>` runs it —
on exactly the same footing as the devices the SDK ships.

For `process` — a QPU — a device is an **executor**, because every process device is
the one built-in QPU driver over a different executor. `Executor` asks for two
methods: `execute()` runs the job on your hardware and returns an `xr.Dataset`, and
`process_result()` turns that into the Qiskit-shaped counts QPI-UI stores.

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


# The name --device will use, bound to the executor behind it. `pass_through=True`
# hands your executor the -o keys the SDK knows nothing about, like qubit_count.
QUANTUM_X = device_spec("quantum_x", executor=QuantumXExecutor, pass_through=True)
```

**Ship it** by advertising the spec under the `qpi_driver.devices` entry-point group
in your own `pyproject.toml` — this is the whole of the packaging:

```toml
[project.entry-points."qpi_driver.devices"]
quantum_x = "mylab_devices:QUANTUM_X"
```

```bash
pip install .    # your distribution, depending on qpi-driver[cli]
qpi-driver start --operation process --device quantum_x -o qubit_count=4 ...
```

`quantum_x` is now indistinguishable from a built-in: it is in `qpi-driver devices`
and it is what `--device` will accept. An entry point that will not import, or
resolves to something other than a `DeviceSpec`, is logged and skipped — it cannot
stop the CLI from starting.

**Or name it by import path**, with nothing to install and nothing to register:

```bash
qpi-driver start --operation process --device mylab_devices:QuantumXExecutor \
  -o qubit_count=4 ...
```

`module.attr` works as well as `module:attr`. An executor named this way is given
every `-o` key the SDK does not read itself, as the string it was typed as — so
`qubit_count` arrives as `"4"` and a typo in it reaches your constructor rather than
being reported. Use it to try a device out, and the entry point to deploy one.

[`examples/custom_device/`](https://github.com/sopherapps/qpi/blob/main/qpi-driver/py/examples/custom_device/)
is this worked all the way through, as two files you can run.

For `monitor`, a device is the driver itself rather than an executor, so the spec's
builder returns a `QpiDriver` and the import path points at that builder — or at a
`DeviceSpec` naming it. There is no separate "custom driver" mechanism: an operation
is a contract QPI-UI implements server-side, so a custom driver is always a custom
device of an existing operation (RFC 0003 §6, §13.4).

> **You can also pass an executor directly.** `QpuDriver(executor=…)` takes a class
> or an instance as well as a name, so `QpuDriver(executor=QuantumXExecutor(qubit_count=4))`
> runs your backend from Python with no spec and no packaging. It is the same
> mechanism — `device_spec` above binds an executor to the QPU driver in exactly this
> way — and it is what the `--device mylab:Cls` route does for you. Reach for it when
> you are driving the SDK from your own Python and want no CLI at all; reach for a
> device when anything else has to *launch* the driver.

Nothing here describes your device to QPI-UI. Registering a driver in the dashboard
is where a device is named, configured and documented; the SDK's job is to run the
one it is told to run (RFC 0003 §9).

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

A driver is run with one verb, `start`: `--operation` says what it does —
`process` (a QPU) or `monitor` (e.g. a cryostat) — and `--device` which backend
within it. Every operation shares the same universal options; each device's own
settings are passed as repeatable `-o key=value`.

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

Which `-o` keys a device reads is the device's own business — the keys its builder
looks at, and nothing else. The SDK publishes no schema for them, because QPI-UI
already holds one: the dashboard's registration form is where a device's options are
described and filled in, and a second copy here would be a second copy to keep in
step (RFC 0003 §9). A key nothing read is reported rather than ignored:

```
Error: unknown option 'data_dirr' for process device 'mock'.
```

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
