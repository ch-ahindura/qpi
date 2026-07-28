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
extending: a **device**. Describe it as data, and the CLI gains it — a line in
`--help`, an entry in `catalog --json`, and `-o` values that are checked and
converted for you, exactly as the built-in devices get.

For `process` — a QPU — a device is an **executor**, because every process device is
the one built-in QPU driver over a different executor. `Executor` asks for two
methods: `execute()` runs the job on your hardware and returns an `xr.Dataset`, and
`process_result()` turns that into the Qiskit-shaped counts QPI-UI stores.

```python
import numpy as np
import xarray as xr

from qpi_driver import Executor, JobPayload, OptionSpec
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


# The executor, described as data. This is what earns it a --help entry and gets
# `-o qubit_count=4` converted to an int rather than passed through as "4".
QUANTUM_X = device_spec(
    "quantum_x",
    executor=QuantumXExecutor,
    summary="MyLab's QuantumX control system.",
    options=(
        OptionSpec(key="qubit_count", help="How many qubits the chip has.",
                   parse=int, default="1", example="4"),
    ),
)
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

`quantum_x` is now indistinguishable from a built-in: in `qpi-driver devices`, in
`--help` with its own options, and in `catalog --json`. An entry point that will not
import, or resolves to something other than a `DeviceSpec`, is logged and skipped —
it cannot stop the CLI from starting.

**Or name it by import path**, with nothing to install and nothing to register:

```bash
qpi-driver start --operation process --device mylab_devices:QuantumXExecutor \
  -o qubit_count=4 ...
```

`module.attr` works as well as `module:attr`. There is no declared schema behind an
import path, so `qubit_count` arrives at the constructor as the string `"4"` and a
typo in it is not caught; the `-o` options the SDK *does* declare, `data_dir`
included, are still validated. Use it to try a device out, and the entry point to
deploy one.

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

The TypeScript SDK has the same import-path route with a different separator —
`--device ./dist/my-device.js#MyExport` — because `:` is a URL scheme separator in a
JavaScript module specifier. The Go SDK has no import-path route at all: Go resolves
imports at compile time, so a device there is registered in your own `main`.

---

## Executor Backends

| Backend | Description | Extra |
|---------|-------------|-------|
| `mock` | Qiskit BasicSimulator (default) | — |
| `qiskit_aer` | Qiskit Aer simulator | `[aer]` |
| `quantify` | Quantify-scheduler + Qblox instruments | `[quantify]` |
| `qblox` | Qblox scheduler (legacy) | `[qblox]` |

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

Each device declares the `-o` keys it reads, so `--help` lists them with their
types, defaults and examples rather than the list being maintained by hand — and
a key no device reads is an error naming the ones that exist, not a silent no-op.

The whole of that — which operations exist, which devices each one can run, and
which `-o` keys each device reads — is the SDK's **catalog**, and the `DeviceSpec`s
are its only source. `devices` prints it for a person; `catalog --json` hands it to
another program, which is how the dashboard builds its setup snippets and how
QPI-UI's own drift test notices when server and SDK disagree.

```bash
# Every operation, its devices, and each device's -o options
qpi-driver devices

# Just one operation
qpi-driver devices --operation monitor

# The same catalog for another program to read
qpi-driver catalog --json
```

Today's catalog — generated from `qpi-driver catalog --json`, so it cannot fall
behind the code:

<!-- catalog:begin -->

<!-- Generated by `make sync-driver-catalog`. Do not edit by hand. -->

| Operation | Device | Ships with |
|-----------|--------|------------|
| `process` | `mock` | base `[cli]` |
| `process` | `presto` | base `[cli]` |
| `process` | `qblox` | `qpi-driver[cli,qblox]` |
| `process` | `qiskit_aer` | `qpi-driver[cli,aer]` |
| `process` | `quantify` | `qpi-driver[cli,quantify]` |
| `monitor` | `bluefors_gen1` | `qpi-driver[cli,bluefors_gen1]` |

`-o` options for `mock`, `presto`, `qblox`, `qiskit_aer`, `quantify`:

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `data_dir` | `safe_dir` | `./bin/data` | Directory the executor writes datasets and artefacts to. |
| `job_timeout` | `int` | `10` | Seconds a single job may run before it is abandoned. |
| `is_dummy` | `bool` | `false` | Run against the vendor's dummy instruments instead of real hardware. |
| `quantify_hardware_config` | `Path` | `./quantify.hardware.json` | Path to the quantify hardware configuration JSON. |
| `quantify_device_config` | `Path` | `./quantify.device.yml` | Path to the quantify device configuration YAML. |

`-o` options for `bluefors_gen1`:

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `channels` | `channels` | **required** | Value-tree channels to poll, as path[:unit] pairs. |
| `base_url` | `str` | `http://127.0.0.1:49099` | Base URL of the Bluefors Control API. |
| `api_key` | `str` | — | Bluefors API access key, if the API requires one. |
| `poll_interval` | `float` | `5.0` | Seconds between polls of every channel. |
| `timeout` | `float` | `5.0` | HTTP timeout per channel read, in seconds. |

<!-- catalog:end -->

---

## Documentation

- [Main QPI Repository](https://github.com/sopherapps/qpi)
- [PyPI Project Page](https://pypi.org/project/qpi-driver/)

---

## License

MIT — see the [main repository](https://github.com/sopherapps/qpi/blob/main/LICENSE) for details.
