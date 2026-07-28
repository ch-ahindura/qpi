# A device the SDK does not ship

Two files, and three ways to run the same device. Which one you want depends on
whether the device is a thing you are trying out or a thing you are deploying.

## 1. Name it by import path — nothing to install

The device is any importable `Executor`, so if `mylab_devices.py` is on your
`PYTHONPATH`, it is already runnable:

```bash
qpi-driver start --operation process \
  --device mylab_devices:ThermometerExecutor \
  --qpi-addr http://localhost:8090 \
  --token "$QPI_ACCESS_TOKEN" \
  --ca-fingerprint "$QPI_CA_FINGERPRINT" \
  --name lab-thermometer \
  -o data_dir=./bin/data \
  -o probe_count=4
```

`module.attr` works as well as `module:attr`. There is no schema behind an import
path, so `probe_count` reaches the executor's constructor as the string `"4"`,
unchecked — the executor converts it. The `-o` options the SDK does declare, such
as `data_dir`, are still validated, so a `data_dir` outside a driver's safe roots
is refused on this route exactly as on any other.

## 2. Install it — it joins the catalog

Ship the `DeviceSpec` under the `qpi_driver.devices` entry-point group, as
[pyproject.toml](pyproject.toml) does:

```toml
[project.entry-points."qpi_driver.devices"]
thermometer = "mylab_devices:THERMOMETER"
```

```bash
pip install .
qpi-driver start --operation process --device thermometer -o probe_count=4 ...
```

Now it is indistinguishable from a built-in: it appears in `qpi-driver devices`,
in `qpi-driver start --operation process --help` with its own options, and in
`qpi-driver catalog --json`. Because the spec *declares* `probe_count`, it arrives
as an `int` and `-o probe_counr=4` is an error naming the valid keys instead of a
setting silently ignored.

An entry point that will not import, or resolves to something other than a
`DeviceSpec`, is logged and skipped. It cannot stop the CLI from starting.

## 3. Neither — build the driver yourself

Nothing above is required to use a custom executor from Python:

```python
from qpi_driver import QpuDriver
from mylab_devices import ThermometerExecutor

QpuDriver(
    qpi_addr="http://localhost:8090",
    token="<token>",
    ca_fingerprint="<fingerprint>",
    name="lab-thermometer",
    executor=ThermometerExecutor(probe_count=4),
).run()
```

## Why there is no "custom driver" mechanism

An operation — `process`, `monitor` — is a contract QPI-UI has a server-side
handler for, so a driver that did something genuinely new would have nothing on
the other end to talk to. A custom driver is therefore always a custom *device* of
an existing operation, and one concept covers it (RFC 0003 §6, §13.4). For
`process` a device is an executor; for `monitor` it is a builder returning a
driver, or a `DeviceSpec` naming one.
