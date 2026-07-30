"""The two routes by which a device arrives from outside the SDK (RFC 0003 §6).

Route 1 is the built-in devices, which register themselves in
:mod:`qpi_driver.builtins`. The other two live here:

**Installed.** A distribution advertises devices through the ``qpi_driver.devices``
entry-point group. ``pip install mylab-qpi-devices`` and its devices are there for
``--device``, and listed by ``qpi-driver devices``, with nothing to change here.
This is the route for a device meant to be shared, versioned and installed on a
production node.

**Named by import path.** ``--device mylab.executors:PrestoV2`` resolves the device
by importing it, with no packaging ceremony at all — the route for "I have a class
in a file and I want to run it now".

Both execute code the operator chose to install or to name, exactly as ``python -m
mylab`` does; the server never supplies a device value and cannot introduce one
(RFC 0003 §10). What they must not do is fail messily: a broken entry point is
logged and skipped rather than taking down ``--help``, and a bad import path
becomes one clean line rather than a traceback naming directories.

:func:`resolve_device` is what the CLI asks; :func:`registry.resolve` stays the
plain table lookup, so the registry itself needs to know nothing about executors.
"""

import importlib
import importlib.metadata
import logging
import re
from typing import Any

from qpi_driver.builtins import qpu
from qpi_driver.builtins.registry import DeviceSpec, Operation, register, resolve
from qpi_driver.executors import Executor

log = logging.getLogger(__name__)

# The entry-point group a distribution advertises its devices under.
ENTRY_POINT_GROUP = "qpi_driver.devices"

# A trailing parenthesised absolute path, which Python volunteers in an ImportError
# ("cannot import name 'X' from 'y' (/home/someone/....py)"). Neither the operator
# nor the journal needs it — see `_without_paths`.
_TRAILING_PATH = re.compile(r"\s*\((?:/|[A-Za-z]:[\\/])[^()]*\)\s*$")


def resolve_device(operation: Operation, device: str) -> DeviceSpec:
    """Turn a ``--device`` value into a spec, whichever route it came by.

    A value containing ``.`` or ``:`` is an import path; anything else is a
    registered name. Registered names are plain identifiers, so the two can never
    collide and a mistyped ``mockk`` still gets the known-devices error rather
    than an import failure (RFC 0003 §6).

    Raises:
        ValueError: if the name is not registered, the path will not import, or
            what it imports is not something this operation can use.
    """
    if "." in device or ":" in device:
        return _imported_spec(operation, device)
    return resolve(operation, device)


def import_object(path: str) -> Any:
    """Import ``module:attr`` or ``module.attr`` and return the attribute.

    Both spellings are accepted, following Pydantic's ``ImportString``.

    Raises:
        ValueError: if *path* is not an import path, or importing it fails. The
            message is one line with no filesystem path in it: an operator gets a
            traceback for a bug, not for a typo, and neither a traceback nor a
            path belongs in the journal (RFC 0003 §10).
    """
    module_name, separator, attribute = path.partition(":")
    if not separator:
        module_name, _, attribute = path.rpartition(".")

    if not module_name or not attribute:
        raise ValueError(
            f"{path!r} is not an import path; expected module:attr or module.attr"
        )

    try:
        module = importlib.import_module(module_name)
        return getattr(module, attribute)
    except ImportError as exc:
        raise ValueError(
            f"could not import device {path!r}: {_without_paths(str(exc))}"
        ) from exc
    except AttributeError as exc:
        raise ValueError(
            f"could not import device {path!r}: {module_name} has no {attribute!r}"
        ) from exc


def _without_paths(message: str) -> str:
    """Strip the filesystem path an import error volunteers (RFC 0003 §10).

    ``ImportError`` names the file it was reading — useful in a traceback, not in
    a driver's journal, where it discloses the layout of the machine to anyone
    who can read the logs.
    """
    return _TRAILING_PATH.sub("", message)


def load_installed_devices() -> tuple[str, ...]:
    """Register every device advertised under :data:`ENTRY_POINT_GROUP`.

    An entry point must resolve to a :class:`DeviceSpec` or an iterable of them.
    One that does not — because it will not import, resolves to something else, or
    names a device already registered — is logged and skipped: a third party's
    mistake must not stop the CLI from starting or ``--help`` from printing.

    Returns the names of the devices registered, for the caller to log or test.
    """
    registered: list[str] = []
    for entry_point in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP):
        try:
            for spec in _specs_from(entry_point.load()):
                register(spec)
                registered.append(spec.name)
        except Exception as exc:
            log.warning(
                "skipping device entry point %r: %s: %s",
                entry_point.name,
                type(exc).__name__,
                exc,
            )
    return tuple(registered)


def _specs_from(loaded: Any) -> tuple[DeviceSpec, ...]:
    """Read one spec, or several, out of what an entry point resolved to."""
    if isinstance(loaded, DeviceSpec):
        return (loaded,)
    specs = tuple(loaded)
    if not specs or not all(isinstance(spec, DeviceSpec) for spec in specs):
        raise TypeError("expected a DeviceSpec or an iterable of DeviceSpec")
    return specs


def _imported_spec(operation: Operation, path: str) -> DeviceSpec:
    """Adapt whatever *path* imports into a device of *operation*.

    What the object has to be is already what a device means for that operation
    (RFC 0003 §6): for ``process`` a device is an executor, for ``calibrate`` it is
    a tuner, for ``monitor`` it is the driver itself. Every operation also takes a
    :class:`DeviceSpec`, which is how someone who wants a declared option schema
    and a ``--help`` entry gets one.
    """
    imported = import_object(path)

    if isinstance(imported, DeviceSpec):
        if imported.operation is not operation:
            raise ValueError(
                f"{path!r} is a {imported.operation.value} device, "
                f"not a {operation.value} one"
            )
        return imported

    if operation is Operation.PROCESS:
        if isinstance(imported, Executor) or (
            isinstance(imported, type) and issubclass(imported, Executor)
        ):
            return qpu.device_spec(path, executor=imported, pass_through=True)
        raise ValueError(
            f"{path!r} is not usable as a process device: expected an Executor "
            "subclass or instance, or a DeviceSpec"
        )

    if operation is Operation.CALIBRATE:
        # Checked before the callable fallback below: a Tuner subclass is
        # callable, so without this it would be mistaken for a device builder
        # and called with the transport arguments.
        from qpi_driver.builtins import calibrate
        from qpi_driver.tuners import Tuner

        if isinstance(imported, Tuner) or (
            isinstance(imported, type) and issubclass(imported, Tuner)
        ):
            return calibrate.device_spec(path, tuner=imported, pass_through=True)
        raise ValueError(
            f"{path!r} is not usable as a calibrate device: expected a Tuner "
            "subclass or instance, or a DeviceSpec"
        )

    if callable(imported):
        return DeviceSpec(name=path, operation=operation, build=imported)
    raise ValueError(
        f"{path!r} is not usable as a {operation.value} device: expected a "
        "device builder or a DeviceSpec"
    )
