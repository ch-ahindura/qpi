# A device the SDK does not ship

Two files, and three ways to run the same device. Which one you want depends on
whether the device is a thing you are trying out or a thing you are deploying.

[pyproject.toml](pyproject.toml) points `qpi-driver` at the sibling source tree with
a `[tool.uv.sources]` stanza, because this example lives inside the SDK's own
repository and `make test-docs` installs it to check that the entry-point route still
works. Your own project would resolve `qpi-driver[cli]` from PyPI; delete that stanza
when you copy this.

## 1. Name it by import path — nothing to install

The device is any importable `Executor`, so if `mylab_devices.py` is on your
`PYTHONPATH`, it is already runnable:

```bash
qpi-driver start --operation process \
  --device mylab_devices:QuantumXExecutor \
  --qpi-addr http://localhost:8090 \
  --token "$QPI_ACCESS_TOKEN" \
  --ca-fingerprint "$QPI_CA_FINGERPRINT" \
  -o data_dir=./bin/data \
  -o qubit_count=4
```

`module.attr` works as well as `module:attr`. `qubit_count` reaches the executor's
constructor as the string `"4"` — the executor converts it, being the only thing
that knows the key exists. The `-o` options the SDK reads itself, such as `data_dir`,
are still checked, so a `data_dir` outside a driver's safe roots is refused on this
route exactly as on any other.

## 2. Install it — it joins the registry

Ship the `DeviceSpec` under the `qpi_driver.devices` entry-point group, as
[pyproject.toml](pyproject.toml) does:

```toml
[project.entry-points."qpi_driver.devices"]
quantum_x = "mylab_devices:QUANTUM_X"
```

```bash
pip install .
qpi-driver start --operation process --device quantum_x -o qubit_count=4 ...
```

Now it is indistinguishable from a built-in: it appears in `qpi-driver devices` and
`--device quantum_x` runs it. What it *is* — what it does, which `-o` keys to fill in
— belongs in QPI-UI, where the driver is registered; the SDK only needs to know the
name and what to call.

An entry point that will not import, or resolves to something other than a
`DeviceSpec`, is logged and skipped. It cannot stop the CLI from starting.

## 3. Neither — build the driver yourself

Nothing above is required to use a custom executor from Python:

```python
from qpi_driver import QpuDriver
from mylab_devices import QuantumXExecutor

QpuDriver(
    qpi_addr="http://localhost:8090",
    token="<token>",
    ca_fingerprint="<fingerprint>",
    executor=QuantumXExecutor(qubit_count=4),
).run()
```

## Why there is no "custom driver" mechanism

An operation — `process`, `monitor` — is a contract QPI-UI has a server-side
handler for, so a driver that did something genuinely new would have nothing on
the other end to talk to. A custom driver is therefore always a custom *device* of
an existing operation, and one concept covers it (RFC 0003 §6, §13.4). For
`process` a device is an executor; for `monitor` it is a builder returning a
driver, or a `DeviceSpec` naming one.
