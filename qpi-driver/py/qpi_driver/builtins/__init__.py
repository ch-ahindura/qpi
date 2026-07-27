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

Devices installed from elsewhere land in the same registry, right after the
built-ins, so nothing downstream can tell them apart
(:mod:`qpi_driver.builtins.discovery`).
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
    as_bool,
    devices,
    operations,
    register,
    resolve,
)

for _spec in (*qpu.DEVICE_SPECS, bluefors_gen1.DEVICE_SPEC):
    register(_spec)

# Imported last, and after registration: discovery reaches back into `qpu` for the
# process builder, so it cannot be imported while this module is still setting up.
from qpi_driver.builtins.discovery import (  # noqa: E402
    ENTRY_POINT_GROUP,
    import_object,
    load_installed_devices,
    resolve_device,
)

load_installed_devices()

__all__ = [
    "QpuDriver",
    "BlueforsGen1Driver",
    "DeviceBuilder",
    "DeviceSpec",
    "ENTRY_POINT_GROUP",
    "Operation",
    "OperationSpec",
    "OptionSpec",
    "as_bool",
    "devices",
    "import_object",
    "load_installed_devices",
    "operations",
    "register",
    "resolve",
    "resolve_device",
]
