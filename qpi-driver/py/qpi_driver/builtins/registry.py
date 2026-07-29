"""What ``--device`` can name: a table of builders (RFC 0003 §6, §9).

Two ideas, deliberately asymmetric:

An **operation** is a contract with QPI-UI — ``process`` runs jobs pushed to it,
``monitor`` reports upward on its own schedule. QPI-UI needs a server-side handler
for each, so the set is closed: :class:`Operation` below is the whole of it, and
growing it is a coordinated change across the SDKs and the server.

A **device** is one backend implementing an operation — an executor for
``process``, a piece of lab hardware for ``monitor``. The set is open: anyone can
add one without touching this module, and this registry is where they land.

What a device *is*, here, is a name and a builder. It has no description, no
declared options and no install target, because none of that is the driver's to
publish: QPI-UI is where a device is chosen and configured, and a driver that also
described itself would be a second catalog to keep in step with the first. A
driver runs what it is told to run.
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from qpi_driver.sdk import QpiDriver


class Operation(str, Enum):
    """What a driver does, and the contract QPI-UI implements for it.

    Closed by design: every value needs a handler on the server side, so a new
    operation is a coordinated change across the SDKs and QPI-UI, never a
    third-party extension (RFC 0003 §13.1). Devices are the open half.
    """

    PROCESS = "process"
    MONITOR = "monitor"


# A device builder takes the ``-o`` options plus the universal transport arguments
# and returns an *unstarted* driver; the caller decides when to start it by calling
# :meth:`QpiDriver.run` (RFC 0003 §7). Returning rather than running is what lets a
# device be built and asserted on in a test with no server.
DeviceBuilder = Callable[..., QpiDriver]


@dataclass(frozen=True)
class DeviceSpec:
    """One device: what ``--device`` names, and what to call when it does.

    Attributes:
        name: How ``--device`` names it, e.g. ``qblox``. A plain identifier —
            values containing ``.`` or ``:`` are import paths, not names.
        operation: Which operation this device implements.
        build: Returns an unstarted driver; see :data:`DeviceBuilder`. It is called
            with an :class:`~qpi_driver.options.Options` and the transport
            arguments, and reads whichever options it understands.
    """

    name: str
    operation: Operation
    build: DeviceBuilder


_DEVICES: dict[Operation, dict[str, DeviceSpec]] = {op: {} for op in Operation}


def register(spec: DeviceSpec) -> None:
    """Add *spec* to the registry, or raise if its name is already taken.

    Names are unique per operation, not globally: nothing stops a ``process``
    and a ``monitor`` device from sharing a name, since ``--device`` is always
    read in the context of an operation.

    Raises:
        ValueError: if a device of the same name is already registered for the
            same operation. Silently replacing it would make the winner depend
            on import order.
    """
    existing = _DEVICES[spec.operation].get(spec.name)
    if existing is not None:
        raise ValueError(
            f"{spec.operation.value} device {spec.name!r} is already registered"
        )
    _DEVICES[spec.operation][spec.name] = spec


def devices(operation: Operation) -> tuple[DeviceSpec, ...]:
    """Every device registered for *operation*, sorted by name.

    Sorted rather than insertion-ordered so ``qpi-driver devices`` is stable no
    matter what imported what first.
    """
    return tuple(spec for _, spec in sorted(_DEVICES[operation].items()))


def resolve(operation: Operation, device: str) -> DeviceSpec:
    """Look up *device* within *operation*.

    Raises:
        ValueError: if no such device is registered, listing the ones that are.
    """
    spec = _DEVICES[operation].get(device)
    if spec is None:
        known = ", ".join(sorted(_DEVICES[operation])) or "none"
        raise ValueError(
            f"unknown {operation.value} device {device!r}. Known devices: {known}."
        )
    return spec
