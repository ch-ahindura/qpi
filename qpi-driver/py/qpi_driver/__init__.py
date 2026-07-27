import importlib.metadata

try:
    __version__ = importlib.metadata.version("qpi-driver")
except importlib.metadata.PackageNotFoundError:
    __version__ = "0.1.2"

from qpi_driver.builtins.bluefors_gen1 import (
    BlueforsGen1Driver,
)
from qpi_driver.builtins import (
    DeviceBuilder,
    DeviceSpec,
    Operation,
    OperationSpec,
    OptionSpec,
    register,
)
from qpi_driver.builtins.qpu import QpuDriver
from qpi_driver.events import Event, EventType
from qpi_driver.executors.base import Executor
from qpi_driver.executors.mock import MockExecutor
from qpi_driver.executors.presto import PrestoExecutor
from qpi_driver.executors.qblox import QbloxExecutor
from qpi_driver.executors.qiskit_aer import QiskitAerExecutor
from qpi_driver.executors.quantify import QuantifyExecutor
from qpi_driver.sdk import QpiDriver

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
    "DeviceBuilder",
    "DeviceSpec",
    "Operation",
    "OperationSpec",
    "OptionSpec",
    "register",
    "Executor",
    "MockExecutor",
    "QiskitAerExecutor",
    "QuantifyExecutor",
    "QbloxExecutor",
    "PrestoExecutor",
]
