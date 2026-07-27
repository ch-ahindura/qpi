"""The operation and device catalog (RFC 0003 §5, §6, §7).

Two ideas, deliberately asymmetric:

An **operation** is a contract with QPI-UI — ``process`` runs jobs pushed to it,
``monitor`` reports upward on its own schedule. QPI-UI needs a server-side handler
for each, so the set is closed: :class:`Operation` below is the whole of it, and
growing it is a coordinated change across the SDKs and the server.

A **device** is one backend implementing an operation — an executor for
``process``, a piece of lab hardware for ``monitor``. The set is open: anyone can
add one without touching this module, and this registry is where they land.

A device is described by data (:class:`DeviceSpec`), never by running it, so
``--help``, ``catalog --json`` and QPI-UI's own catalog can all be generated from
one source. Specs live beside the code they describe — ``qpu.py`` owns the
``process`` specs, ``bluefors_gen1.py`` owns its own — and are registered here as
the package imports them.
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from qpi_driver.events import EventType
from qpi_driver.sdk import QpiDriver


class Operation(str, Enum):
    """What a driver does, and the contract QPI-UI implements for it.

    Closed by design: every value needs a handler on the server side, so a new
    operation is a coordinated change across the SDKs and QPI-UI, never a
    third-party extension (RFC 0003 §13.1). Devices are the open half.
    """

    PROCESS = "process"
    MONITOR = "monitor"


# A device builder takes the parsed ``-o`` options plus the universal transport
# arguments and returns an *unstarted* driver; the caller decides when to start
# it by calling :meth:`QpiDriver.run` (RFC 0003 §7). Returning rather than running
# is what lets a device be built and asserted on in a test with no server.
DeviceBuilder = Callable[..., QpiDriver]


@dataclass(frozen=True)
class OptionSpec:
    """One ``-o key=value`` setting a device reads.

    Attributes:
        key: The option name as typed, e.g. ``data_dir``.
        help: One line for ``--help``, in the imperative or descriptive voice
            used by the CLI's own option help.
        parse: Turns the raw string into the value the builder wants. Defaults to
            ``str``, i.e. no coercion.
        default: What the device uses when the option is absent. ``None`` means
            the device decides.
        required: Whether omitting the option is an error.
        example: A ready-to-paste value for the generated help and snippets.
    """

    key: str
    help: str
    parse: Callable[[str], Any] = str
    default: Any = None
    required: bool = False
    example: str = ""


@dataclass(frozen=True)
class DeviceSpec:
    """The data-only description of one device.

    Deliberately the same shape as ``qpi-ui/internal/drivers.Spec``, extended
    with the fields ``--help`` needs, so the two catalogs can be checked against
    each other rather than drifting.

    Attributes:
        name: How ``--device`` names it, e.g. ``qblox``. A plain identifier —
            values containing ``.`` or ``:`` are import paths, not names.
        operation: Which operation this device implements.
        build: Returns an unstarted driver; see :data:`DeviceBuilder`.
        options: The ``-o`` keys this device reads.
        extra: The install target that ships it, e.g. ``qpi-driver[cli,qblox]``.
            Empty means the base ``[cli]`` extra is enough.
        summary: One line describing the device, for generated help.
    """

    name: str
    operation: Operation
    build: DeviceBuilder
    options: tuple[OptionSpec, ...] = ()
    extra: str = ""
    summary: str = ""


@dataclass(frozen=True)
class OperationSpec:
    """The data-only description of one operation.

    Mirrors what QPI-UI records per driver kind, so the event types an operation
    involves are stated once rather than implied by whichever devices happen to
    be registered.

    Attributes:
        name: The operation itself.
        summary: One line describing what drivers of this operation do.
        default_device: The device used when ``--device`` is omitted.
        events: The event types drivers of this operation take part in.
    """

    name: Operation
    summary: str
    default_device: str
    events: tuple[EventType, ...] = ()


OPERATIONS: dict[Operation, OperationSpec] = {
    Operation.PROCESS: OperationSpec(
        name=Operation.PROCESS,
        summary="Run quantum jobs pushed by QPI-UI and report their results.",
        default_device="mock",
        events=(EventType.JOB_DISPATCH, EventType.JOB_RESULT),
    ),
    Operation.MONITOR: OperationSpec(
        name=Operation.MONITOR,
        summary="Report readings upward on a timer.",
        default_device="bluefors_gen1",
        events=(EventType.CRYOSTAT_READING,),
    ),
}

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


def operations() -> tuple[OperationSpec, ...]:
    """Every operation, in declaration order."""
    return tuple(OPERATIONS.values())


def devices(operation: Operation) -> tuple[DeviceSpec, ...]:
    """Every device registered for *operation*, sorted by name.

    Sorted rather than insertion-ordered so generated help and
    ``catalog --json`` are stable no matter what imported what first.
    """
    return tuple(spec for _, spec in sorted(_DEVICES[operation].items()))


def resolve(operation: Operation, device: str) -> DeviceSpec:
    """Look up *device* within *operation*.

    Raises:
        ValueError: if no such device is registered, listing the ones that are.
    """
    spec = _DEVICES[operation].get(device)
    if spec is None:
        known = ", ".join(sorted(_DEVICES[operation]))
        raise ValueError(
            f"unknown {operation.value} device {device!r}. Known devices: {known}."
        )
    return spec
