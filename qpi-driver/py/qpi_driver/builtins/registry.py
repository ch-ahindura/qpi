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

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from qpi_driver.events import EventType
from qpi_driver.sdk import QpiDriver


def as_bool(raw: str) -> bool:
    """Parse a boolean-ish option value.

    ``1``, ``true``, ``yes`` and ``on`` are true, in any case; anything else,
    including the empty string, is false.
    """
    return raw.strip().lower() in ("1", "true", "yes", "on")


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
            ``str``, i.e. no coercion. The one place an option value is coerced,
            so no builder hand-rolls ``int(...)`` or path validation.
        default: The value used when the option is absent, written the way it
            would be typed on the command line — it goes through :attr:`parse`
            like any other value, and is what ``catalog --json`` reports.
            ``None`` means the option has no default.
        required: Whether omitting the option is an error.
        example: A ready-to-paste value for the generated help and snippets.
    """

    key: str
    help: str
    parse: Callable[[str], Any] = str
    default: str | None = None
    required: bool = False
    example: str = ""

    @property
    def type_name(self) -> str:
        """The parser's name, for generated help and ``catalog --json``.

        An ``as_``/``parse_`` prefix is dropped so what is left reads as the kind
        of value expected: ``str``, ``int``, ``float``, ``Path``, ``bool``,
        ``safe_dir``, ``channels``.
        """
        name = getattr(self.parse, "__name__", type(self.parse).__name__)
        return name.removeprefix("as_").removeprefix("parse_")


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
        build: Returns an unstarted driver; see :data:`DeviceBuilder`. Its
            ``options`` argument is the output of :meth:`parse_options`, never
            raw strings.
        options: The ``-o`` keys this device reads.
        extra: The install target that ships it, e.g. ``qpi-driver[cli,qblox]``.
            Empty means the base ``[cli]`` extra is enough.
        summary: One line describing the device, for generated help.
        accepts_any_option: Whether an ``-o`` key this device does not declare is
            passed through as a raw string instead of rejected. For a device
            named by import path, which has no declared schema to check against
            (RFC 0003 §6) — a device in the catalog declares its options, and
            leaves this alone so a typo stays an error.
    """

    name: str
    operation: Operation
    build: DeviceBuilder
    options: tuple[OptionSpec, ...] = ()
    extra: str = ""
    summary: str = ""
    accepts_any_option: bool = False

    def parse_options(self, raw: Mapping[str, str]) -> dict[str, Any]:
        """Check *raw* ``-o`` values against this device's schema and coerce them.

        The result is what :attr:`build` is called with, and carries every option
        that has a value — the ones given, plus the parsed :attr:`~OptionSpec.default`
        of each one left out — so a builder reads its keys without repeating their
        defaults, and coercion happens here rather than in five builders. A key
        this device does not declare is an error, unless
        :attr:`accepts_any_option` says to pass it through unconverted.

        Raises:
            ValueError: on a key this device does not read, a missing required
                key, or a value its own parser rejects. The CLI turns any of the
                three into one clean line.
        """
        declared = {option.key: option for option in self.options}

        unknown = sorted(set(raw) - set(declared))
        if unknown and not self.accepts_any_option:
            label = "options" if len(unknown) > 1 else "option"
            raise ValueError(
                f"unknown {label} {', '.join(repr(key) for key in unknown)} for "
                f"{self.operation.value} device {self.name!r}. Valid options: "
                f"{', '.join(sorted(declared)) or 'none'}."
            )

        # Undeclared options on a device that takes them go through as typed: with
        # no schema there is nothing to convert them to, and guessing would be worse.
        parsed: dict[str, Any] = {key: raw[key] for key in unknown}
        for key, option in declared.items():
            if key in raw:
                value = raw[key]
            elif option.required:
                example = f", e.g. -o {key}={option.example}" if option.example else ""
                raise ValueError(
                    f"{self.operation.value} device {self.name!r} needs a "
                    f"{key!r} option{example}"
                )
            elif option.default is None:
                continue
            else:
                value = option.default

            try:
                parsed[key] = option.parse(value)
            except ValueError as exc:
                raise ValueError(f"bad value for -o {key}: {exc}") from exc

        return parsed


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
        default_name: The driver name used when ``--name`` is omitted. Here rather
            than in the CLI because it is the same kind of fact as
            *default_device*, and one verb serves every operation.
        events: The event types drivers of this operation take part in.
    """

    name: Operation
    summary: str
    default_device: str
    default_name: str = ""
    events: tuple[EventType, ...] = ()


OPERATIONS: dict[Operation, OperationSpec] = {
    Operation.PROCESS: OperationSpec(
        name=Operation.PROCESS,
        summary="Run quantum jobs pushed by QPI-UI and report their results.",
        default_device="mock",
        default_name="qpu_sim_01",
        events=(EventType.JOB_DISPATCH, EventType.JOB_RESULT),
    ),
    Operation.MONITOR: OperationSpec(
        name=Operation.MONITOR,
        summary="Report readings upward on a timer.",
        default_device="bluefors_gen1",
        default_name="qpi-monitor",
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
