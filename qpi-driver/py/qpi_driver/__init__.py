import importlib.metadata

try:
    __version__ = importlib.metadata.version("qpi-driver")
except importlib.metadata.PackageNotFoundError:
    __version__ = "0.4.2-rc.15"

from qpi_driver.builtins import (
    DeviceBuilder,
    DeviceSpec,
    Operation,
    Options,
    load_installed_devices,
    register,
)
from qpi_driver.builtins.bluefors_gen1 import (
    BlueforsGen1Driver,
)
from qpi_driver.builtins.calibrate import CalibrateDriver
from qpi_driver.builtins.qpu import QpuDriver
from qpi_driver.events import Event, EventType
from qpi_driver.executors.base import CircuitPayload, Executor, JobPayload
from qpi_driver.executors.mock import MockExecutor
from qpi_driver.executors.presto import PrestoExecutor
from qpi_driver.executors.qblox import QbloxExecutor
from qpi_driver.executors.qiskit_aer import QiskitAerExecutor
from qpi_driver.executors.quantify import QuantifyExecutor
from qpi_driver.sdk import QpiDriver
from qpi_driver.tuners import Tuner

# The Python SDK lives at `qpi-driver/py/`, alongside the TypeScript
# (`qpi-driver/js/`) and Go (`qpi-driver/go/`) SDKs, mirroring qpi-client's
# per-language layout (RFC 0001 §2, ROADMAP.md Phase 4).

__all__ = [
    "__version__",
    "Event",
    "EventType",
    "QpiDriver",
    "QpuDriver",
    "BlueforsGen1Driver",
    "CalibrateDriver",
    "DeviceBuilder",
    "DeviceSpec",
    "Operation",
    "Options",
    "load_installed_devices",
    "register",
    "CircuitPayload",
    "Executor",
    "JobPayload",
    "MockExecutor",
    "QiskitAerExecutor",
    "QuantifyExecutor",
    "QbloxExecutor",
    "PrestoExecutor",
    "Tuner",
]

# Devices installed from elsewhere, registered last so nothing downstream can tell
# them apart from a built-in (RFC 0003 §6).
#
# Here, and not in `qpi_driver.builtins`, because a device imports the SDK: loading
# one before every name above is bound means importing a third party's module against
# a half-initialised `qpi_driver`, which is an ImportError it gets blamed for. By this
# line the package a device imports is complete. Importing any `qpi_driver` submodule
# runs this file first, so there is no route into the registry that skips it.
load_installed_devices()
