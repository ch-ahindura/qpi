<pre align="center">
 ██████╗ ██████╗ ██╗
██╔═══██╗██╔══██╗██║
██║   ██║██████╔╝██║
██║▄▄ ██║██╔═══╝ ██║
╚██████╔╝██║     ██║
 ╚══▀▀═╝ ╚═╝     ╚═╝
</pre>

<h1 align="center">QPI QPU Driver</h1>

<p align="center">
  <a href="https://badge.fury.io/py/qpi-driver"><img src="https://badge.fury.io/py/qpi-driver.svg" alt="PyPI version"></a>
  <a href="https://github.com/sopherapps/qpi/actions/workflows/ci.yml"><img src="https://github.com/sopherapps/qpi/actions/workflows/ci.yml/badge.svg" alt="CI/CD Workflow"></a>
  <a href="https://github.com/sopherapps/qpi/releases"><img src="https://img.shields.io/github/v/tag/sopherapps/qpi?label=version" alt="GitHub Tag"></a>
  <img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License">
</p>

<p align="center">
  Python QPU driver for the <a href="https://github.com/sopherapps/qpi">QPI</a> quantum computing platform.
  Runs on isolated hardware nodes controlling the QPU via multiple executor backends.
</p>

<p align="center">
  <strong><a href="https://sopherapps.github.io/qpi/driver/">📚 Read the Documentation</a></strong>
</p>

---

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
qpi-driver process \
  --qpi-addr http://localhost:8090 \
  --token <qpu-access-token> \
  --ca-fingerprint <fingerprint> \
  --name qpu_sim_01 \
  --device mock \
  -o data_dir=./data
```

Environment variables are also supported for the universal flags:

```bash
export QPI_ADDR=http://localhost:8090
export QPI_ACCESS_TOKEN=<token>
export QPI_CA_FINGERPRINT=<fingerprint>
export QPI_DRIVER_NAME=qpu_sim_01
export QPI_DEVICE=mock
qpi-driver process
```

### systemd Service (Linux)

To run `qpi-driver` persistently on a Linux machine in the background, you can use `systemd`. 

We have provided a standalone interactive bash installer that automates the entire process (installing `uv`, installing the `qpi-driver` tool, prompting for your tokens/addresses, and registering the systemd service):

```bash
# Run the interactive systemd installer script directly via curl
sudo bash -c "$(curl -LsSf https://raw.githubusercontent.com/sopherapps/qpi/main/qpi-driver/install-systemd.sh)"
```

Alternatively, you can run the installer non-interactively by specifying all environment variables:
```bash
curl -LsSf https://raw.githubusercontent.com/sopherapps/qpi/main/qpi-driver/install-systemd.sh | sudo \
  QPI_TOKEN="<your-qpi-access-token>" \
  QPI_ADDR="http://127.0.0.1:8090" \
  CA_FINGERPRINT="<fingerprint>" \
  QPU_NAME="rigetti-aspen-1" \
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

   ExecStart=/home/<user>/.local/bin/qpi-driver process \
           --ca-fingerprint <your-fingerprint> \
           --qpi-addr <your-qpi-server-address> \
           --name "rigetti-aspen-1" \
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
    name="qpu_sim_01",
    executor="mock",
    data_dir=Path("./data"),
).run()
```

### Custom executor

An executor the SDK does not ship needs no registration — pass the class or an
instance of it as `executor`:

```python
from qpi_driver import Executor, QpuDriver

class MyCustomExecutor(Executor):
    def execute(self, payload):
        # Your QPU-specific execution logic
        ...

QpuDriver(
    qpi_addr="http://localhost:8090",
    token="<token>",
    ca_fingerprint="<fingerprint>",
    name="my_qpu",
    executor=MyCustomExecutor(),
).run()
```

To run that same executor from the CLI, either name it by import path or ship it
as a device. [`examples/custom_device/`](examples/custom_device/) is a worked
example of both:

```bash
# A class in a file, with no packaging at all
qpi-driver process --device mylab_devices:ThermometerExecutor -o probe_count=4 ...

# Or, after `pip install` of a distribution declaring the qpi_driver.devices
# entry point — now it is in --help and catalog --json like any built-in
qpi-driver process --device thermometer -o probe_count=4 ...
```

For `monitor`, a device is the driver rather than an executor, so the import path
points at a builder returning one, or at a `DeviceSpec` naming it. There is no
separate "custom driver" mechanism: an operation is a contract QPI-UI implements,
so a custom driver is always a custom device of an existing operation.

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

The driver uses Python's `multiprocessing` library to isolate responsibilities:

- **Main Process**: NNG PULL listener, receives commands from server
- **Worker Process**: Executes quantum circuits via the configured executor
- **Result Sender Process**: NNG PUSH, sends results back to server

```
┌─────────────┐     NNG PUSH      ┌─────────────────┐
│ Server│ ────────────────> │  Main Process   │
│  Dispatcher │                   │  (PULL listener)│
└─────────────┘                   └────────┬────────┘
                                           │
                              multiprocessing.Queue
                                           │
                                           ▼
                                   ┌───────────────┐
                                   │ Worker Process│
                                   │  (Executor)   │
                                   └───────┬───────┘
                                           │
                              multiprocessing.Queue
                                           │
                                           ▼
                                   ┌───────────────┐
                                   │Result Sender  │
                                   │  (PUSH)       │
                                   └───────┬───────┘
                                           │ NNG PUSH
                                           ▼
                                   ┌───────────────┐
                                   │  Server │
                                   │   Listener    │
                                   └───────────────┘
```

---

## CLI Reference

A driver is run by its operation subcommand — `process` (a QPU) or `monitor`
(e.g. a cryostat) — on a specific `--device`. Both share the same universal
options; each device's own settings are passed as repeatable `-o key=value`.

```
qpi-driver process|monitor [OPTIONS]

Universal options:
  -a, --qpi-addr TEXT     QPI server URL [env: QPI_ADDR]
  -t, --token TEXT        Access token identifying the driver [env: QPI_ACCESS_TOKEN]
  -n, --name TEXT         Human-readable driver name [env: QPI_DRIVER_NAME]
  -d, --device TEXT       Backend within the operation, e.g. mock, qblox, bluefors_gen1 [env: QPI_DEVICE]
  -o, --option KEY=VALUE  A setting of the chosen device, repeatable
  --ca-file PATH          Path to the CA root certificate [env: QPI_CA_FILE]
  --ca-fingerprint TEXT   Fingerprint pinning the CA root certificate [env: QPI_CA_FINGERPRINT]
  --help                  Show this message and exit.
```

Each device declares the `-o` keys it reads, so `--help` lists them with their
types, defaults and examples rather than the list being maintained by hand — and
a key no device reads is an error naming the ones that exist, not a silent no-op.

```bash
# Every operation, its devices, and each device's -o options
qpi-driver devices

# Just one operation
qpi-driver devices --operation monitor

# The same catalog for another program to read
qpi-driver catalog --json
```

Today's catalog:

| Operation | Device | `-o` options |
|-----------|--------|--------------|
| `process` | `mock`, `presto`, `qiskit_aer`, `quantify`, `qblox` | `data_dir`, `job_timeout`, `is_dummy`, `quantify_hardware_config`, `quantify_device_config` |
| `monitor` | `bluefors_gen1` | `channels` (required), `base_url`, `api_key`, `poll_interval`, `timeout` |

---

## Documentation

- [Main QPI Repository](https://github.com/sopherapps/qpi)
- [PyPI Project Page](https://pypi.org/project/qpi-driver/)

---

## License

MIT — see the [main repository](https://github.com/sopherapps/qpi/blob/main/LICENSE) for details.
