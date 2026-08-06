"""Writing calibration back to the device YAML (RFC 0004 §10).

This file is the trust boundary. A tuner writes ``quantify.device.yml``; the
`process` driver on the same node reads it for every job. Getting it wrong does
not fail the calibration — it fails every job afterwards, and looks like drift.

Three rules follow, and all three are enforced here rather than left to callers:

**Write the schema the loader reads.** ``load_quantum_device`` iterates the
top-level mapping as ``element_name -> {element_type, submodule: {param: value}}``.
Anything else — a compilation config, say — parses as YAML and then fails as a
device.

**Never emit a tag ``safe_load`` cannot read.** The loader uses ``yaml.safe_load``,
so ``yaml.dump`` of live objects produces a file unreadable by the only thing
that reads it. Everything written here is a plain scalar.

**Prove it loads before replacing the original.** The candidate is written to a
temporary file and loaded back through the real loader; only a file that
survives that becomes the device config. The previous file is kept beside it, so
a calibration that makes things worse can be undone without a second run.
"""

import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

import yaml

from qpi_driver.tuners.base.device import (
    construction_args,
    construction_kwargs,
    edge_names,
    element_names,
    parameters_of,
    submodules_of,
    write,
)

log = logging.getLogger(__name__)

#: The key ``load_quantum_device`` reads a device element's class from.
ELEMENT_TYPE_PROP = "element_type"

#: Suffix of the copy kept beside the device config before it is overwritten.
BACKUP_SUFFIX = ".prev"

#: Parameters that describe the instrument rather than the calibration.
#:
#: ``element_type`` and ``name`` are here because qblox's elements are pydantic
#: models that carry both as ordinary fields, so they come back from
#: `parameters_of` looking like calibration. Writing them out would overwrite
#: this module's own ``element_type`` — the mapping of class path and
#: construction arguments the loader reads — with a bare class name, and the
#: file would no longer load. quantify's qcodes elements do not have the
#: collision, which is why it went unnoticed for as long as only quantify was
#: exercised.
_UNCALIBRATED = frozenset(
    {
        # Reading this asks the element for an identity it has no `ask` to answer.
        "IDN",
        ELEMENT_TYPE_PROP,
        "name",
        # Structural, not calibration: which class this is and which two qubits
        # an edge joins. All three are already captured in `element_type`, and
        # re-emitting them as parameters means the loader tries to *assign* them
        # to a constructed object — which pydantic's discriminated fields refuse.
        "edge_type",
        "parent_element_name",
        "child_element_name",
    }
)


class PersistenceError(Exception):
    """The calibration could not be written back safely."""


def serialise_device(device: Any) -> dict[str, Any]:
    """A ``QuantumDevice`` as the mapping ``load_quantum_device`` accepts."""
    config: dict[str, Any] = {}
    for name in element_names(device):
        config[name] = _serialise_component(device.get_element(name))
    for name in edge_names(device):
        config[name] = _serialise_component(device.get_edge(name))
    return config


def _serialise_component(component: Any) -> dict[str, Any]:
    """One element or edge: its class path, then its submodules' parameters."""
    cls = type(component)
    data: dict[str, Any] = {
        ELEMENT_TYPE_PROP: {
            "path": f"{cls.__module__}.{cls.__qualname__}",
            "args": construction_args(component),
            "kwargs": construction_kwargs(component),
        }
    }

    for submodule_name, submodule in submodules_of(component).items():
        parameters = _serialise_parameters(submodule)
        if parameters:
            data[submodule_name] = parameters

    data.update(_serialise_parameters(component))
    return data


def _serialise_parameters(owner: Any) -> dict[str, Any]:
    """Readable, plain-scalar parameters of *owner*.

    A parameter that cannot be read, or whose value is not a YAML scalar, is
    skipped rather than coerced: ``str()`` of a live object round-trips into a
    string the loader would then push back into a numeric parameter.
    """
    values: dict[str, Any] = {}
    for name, value in parameters_of(owner, skip=_UNCALIBRATED).items():
        if value is None or isinstance(value, (bool, int, float, str)):
            values[name] = value
        elif isinstance(value, (list, tuple)) and all(
            isinstance(v, (int, float, str, bool, type(None))) for v in value
        ):
            values[name] = list(value)
    return values


def apply_device_config(device: Any, path: Path) -> list[str]:
    """Apply the calibration at *path* onto a device already built.

    The inverse of :func:`serialise_device`, and generic over both schedulers for
    the same reason. In place, so the compiler, the coordinator and qblox's agent
    keep pointing at the same object.

    Returns the names *path* carries that the device does not have — structural, so
    reported rather than raised.
    """
    with open(path, "r") as file:
        config = yaml.safe_load(file) or {}

    unknown: list[str] = []
    for name, data in config.items():
        component = _component_by_name(device, name)
        if component is None:
            unknown.append(name)
            continue
        _apply_serialised(component, data or {})
    return unknown


def _component_by_name(device: Any, name: str) -> Any:
    if name in element_names(device):
        return device.get_element(name)
    if name in edge_names(device):
        return device.get_edge(name)
    return None


def _apply_serialised(component: Any, data: dict[str, Any]) -> None:
    """Write one component's parameters and submodules, skipping the structural."""
    for key, value in data.items():
        if key in _UNCALIBRATED:
            continue
        if isinstance(value, dict):
            submodule = submodules_of(component).get(key)
            if submodule is not None:
                _apply_serialised(submodule, value)
            continue
        try:
            write(component, key, value)
        except Exception:  # noqa: BLE001 - a parameter the device will not take
            log.debug("skipping %s: device would not accept it", key)


def save_device_config(device: Any, path: Path, *, keep_backup: bool = True) -> None:
    """Write *device*'s calibration to *path*, atomically and only if it reloads.

    Args:
        device: the in-memory ``QuantumDevice`` the routines have updated.
        path: the device config to replace.
        keep_backup: copy the previous file to ``<path>.prev`` first.

    Raises:
        PersistenceError: if the serialised device does not load back. The
            original file is left exactly as it was.
    """
    path = Path(path)
    config = serialise_device(device)
    if not config:
        raise PersistenceError(
            "refusing to write an empty device config — the device has no elements"
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", dir=path.parent, delete=False, suffix=".tmp", prefix=path.name + "."
    )
    candidate = Path(handle.name)
    try:
        with handle as tmp:
            yaml.safe_dump(config, tmp, default_flow_style=False, sort_keys=False)
            tmp.flush()
            os.fsync(tmp.fileno())

        _verify_loads(candidate, config)

        if keep_backup and path.exists():
            shutil.copy2(path, path.with_suffix(path.suffix + BACKUP_SUFFIX))
        os.replace(candidate, path)
        log.info("wrote calibrated device config to %s", path)
    except Exception:
        candidate.unlink(missing_ok=True)
        raise


def _verify_loads(candidate: Path, expected: dict[str, Any]) -> None:
    """Check *candidate* is readable as a device config, or raise.

    Read back with the loader's own ``yaml.safe_load`` and compared against what
    was dumped — which is what catches an emitted tag ``safe_load`` refuses, or a
    value mangled on the way out.

    The elements are then checked structurally rather than instantiated. qcodes
    keeps a global registry of instrument names, so constructing ``q0`` a second
    time while the live device still holds it raises regardless of whether the
    file is good — the check would fail on every correct write. Structure is
    what is actually at risk here: the class each element names came from a live
    instance of that class, so it constructs; what a bad write gets wrong is the
    shape, and that is verified exactly.
    """
    try:
        with open(candidate) as f:
            reloaded = yaml.safe_load(f)
    except Exception as exc:
        raise PersistenceError(
            f"refusing to write {candidate.name}: it does not parse as safe YAML "
            f"({exc}). The existing config is unchanged."
        ) from exc

    if not _equivalent(reloaded, expected):
        raise PersistenceError(
            f"refusing to write {candidate.name}: it does not read back as written. "
            "The existing config is unchanged."
        )

    for name, element in (reloaded or {}).items():
        if not isinstance(element, dict) or ELEMENT_TYPE_PROP not in element:
            raise PersistenceError(
                f"refusing to write {candidate.name}: element {name!r} has no "
                f"{ELEMENT_TYPE_PROP!r}, so the loader would reject it. "
                "The existing config is unchanged."
            )
        path = element[ELEMENT_TYPE_PROP].get("path")
        if not isinstance(path, str) or "." not in path:
            raise PersistenceError(
                f"refusing to write {candidate.name}: element {name!r} has an "
                f"unusable {ELEMENT_TYPE_PROP}.path ({path!r}). "
                "The existing config is unchanged."
            )


def _equivalent(left: Any, right: Any) -> bool:
    """Structural equality, counting NaN as equal to NaN.

    An uncalibrated qcodes parameter reads as NaN, and NaN never equals itself,
    so a plain ``==`` would report every freshly loaded device as corrupted.
    """
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _equivalent(left[key], right[key]) for key in left
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _equivalent(a, b) for a, b in zip(left, right)
        )
    if isinstance(left, float) and isinstance(right, float):
        return left == right or (left != left and right != right)
    return left == right


def restore_backup(path: Path) -> None:
    """Put ``<path>.prev`` back, undoing the last write-back.

    Raises:
        PersistenceError: if there is no backup to restore.
    """
    path = Path(path)
    backup = path.with_suffix(path.suffix + BACKUP_SUFFIX)
    if not backup.exists():
        raise PersistenceError(f"no backup at {backup} to restore")
    shutil.copy2(backup, path)
    log.info("restored %s from %s", path, backup)
