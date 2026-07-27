"""Officially maintained drivers that ship with the SDK (RFC 0001 §3, §5).

Drivers are grouped by *operation* — what they do, which QPI-UI must have a
handler for — and within an operation by *device*, the particular backend. QPUs
that run jobs pushed to them are ``process`` devices; things that report upward on
a timer are ``monitor`` devices (RFC 0003 §5).

Each device describes itself with a :class:`~qpi_driver.builtins.registry.DeviceSpec`
declared beside its own code, so a device and its description cannot drift apart.
This module is only where they are registered: importing it makes every built-in
device visible to :func:`~qpi_driver.builtins.registry.devices`, ``--help`` and
``--device``. Adding a device is one spec in its own module and one line here, with
no CLI changes.
"""

from qpi_driver.builtins import bluefors_gen1, qpu
from qpi_driver.builtins.bluefors_gen1 import (
    BlueforsGen1Driver,
)
from qpi_driver.builtins.qpu import QpuDriver
from qpi_driver.builtins.registry import (
    DeviceBuilder,
    DeviceSpec,
    Operation,
    OperationSpec,
    OptionSpec,
    devices,
    operations,
    register,
    resolve,
)

for _spec in (*qpu.DEVICE_SPECS, bluefors_gen1.DEVICE_SPEC):
    register(_spec)

__all__ = [
    "QpuDriver",
    "BlueforsGen1Driver",
    "DeviceBuilder",
    "DeviceSpec",
    "Operation",
    "OperationSpec",
    "OptionSpec",
    "devices",
    "operations",
    "register",
    "resolve",
]
