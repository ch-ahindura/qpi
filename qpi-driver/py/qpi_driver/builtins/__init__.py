"""Officially maintained drivers that ship with the SDK (RFC 0001 §3, §5).

Drivers are grouped by *operation* — what they do, which QPI-UI must have a
handler for — and within an operation by *device*, the particular backend. QPUs
that run jobs pushed to them are ``process`` devices; things that report upward on
a timer are ``monitor`` devices (RFC 0003 §5).

Each device is a :class:`~qpi_driver.builtins.registry.DeviceSpec` — a name, an
operation and a builder — declared beside its own code. This module is only where
they are registered: importing it makes every built-in device visible to
:func:`~qpi_driver.builtins.registry.devices` and to ``--device``. Adding a device
is one spec in its own module and one line here, with no CLI changes.

Devices installed from elsewhere land in the same registry, right after the
built-ins, so nothing downstream can tell them apart
(:mod:`qpi_driver.builtins.discovery`).
"""

from qpi_driver.builtins import bluefors_gen1, calibrate, qpu
from qpi_driver.builtins.bluefors_gen1 import (
    BlueforsGen1Driver,
)
from qpi_driver.builtins.calibrate import CalibrateDriver
from qpi_driver.builtins.qpu import QpuDriver
from qpi_driver.builtins.registry import (
    DeviceBuilder,
    DeviceSpec,
    Operation,
    devices,
    register,
    resolve,
)
from qpi_driver.options import Options

for _spec in (*qpu.DEVICE_SPECS, bluefors_gen1.DEVICE_SPEC, *calibrate.DEVICE_SPECS):
    register(_spec)

# Imported last, and after registration: discovery reaches back into `qpu` for the
# process builder, so it cannot be imported while this module is still setting up.
#
# `load_installed_devices()` is *not* called here. An installed device imports the
# SDK — the documented way to write one is `from qpi_driver import Executor,
# JobPayload, Options` — and this module is imported by `qpi_driver/__init__.py`
# on its way to binding those names, so loading here means loading a third party's
# module against a half-initialised `qpi_driver`. Every such device was skipped with
# "cannot import name 'Executor' from partially initialized module". The call moved
# to the end of `qpi_driver/__init__.py`, which is the first moment the package a
# device imports is actually complete.
from qpi_driver.builtins.discovery import (  # noqa: E402
    ENTRY_POINT_GROUP,
    import_object,
    load_installed_devices,
    resolve_device,
)

__all__ = [
    "QpuDriver",
    "BlueforsGen1Driver",
    "CalibrateDriver",
    "DeviceBuilder",
    "DeviceSpec",
    "ENTRY_POINT_GROUP",
    "Operation",
    "Options",
    "devices",
    "import_object",
    "load_installed_devices",
    "register",
    "resolve",
    "resolve_device",
]
