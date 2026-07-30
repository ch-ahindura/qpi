"""Reading and writing device parameters across two schedulers (RFC 0004 §6.2).

The two device models are not the same shape. quantify-scheduler builds on
qcodes, where a parameter is a callable — ``element.rxy.amp180()`` reads and
``element.rxy.amp180(0.1)`` writes. qblox-scheduler uses pydantic models, where
the same parameter is a plain float attribute.

A routine that knew the difference would have to be written twice, so it does
not: everything here takes either shape. The alternative — two copies of every
routine — is exactly what this package is arranged to avoid.
"""

from typing import Any

#: Parameters the two schedulers spell differently. Looked up in order, so a
#: routine names the concept and the shim finds whichever the device has.
ALIASES: dict[str, tuple[str, ...]] = {
    # quantify calls the DRAG coefficient `motzoi`; qblox calls it `beta`.
    "motzoi": ("motzoi", "beta"),
    "beta": ("beta", "motzoi"),
}


class ParameterError(AttributeError):
    """The device has no such parameter under any of its known names."""


def read(owner: Any, name: str) -> Any:
    """Read parameter *name* from *owner*, whichever shape it is."""
    value = _attr(owner, name)
    return value() if callable(value) else value


def write(owner: Any, name: str, value: Any) -> None:
    """Write *value* to parameter *name* on *owner*, whichever shape it is."""
    resolved = _name_on(owner, name)
    current = getattr(owner, resolved)
    if callable(current):
        current(value)  # a qcodes Parameter
    else:
        setattr(owner, resolved, value)  # a pydantic model field


def read_path(component: Any, dotted: str) -> Any:
    """Read a dotted parameter path, e.g. ``"rxy.amp180"``."""
    owner, name = _walk(component, dotted)
    return read(owner, name)


def write_path(component: Any, dotted: str, value: Any) -> None:
    """Write a dotted parameter path, e.g. ``"rxy.amp180"``."""
    owner, name = _walk(component, dotted)
    write(owner, name, value)


def parameters_of(owner: Any) -> dict[str, Any]:
    """Every readable parameter of *owner* as plain values.

    qcodes exposes ``parameters`` as a mapping of Parameter objects and pydantic
    as a mapping of values already; both reduce to the same dict here.
    """
    values: dict[str, Any] = {}
    for name, parameter in (getattr(owner, "parameters", None) or {}).items():
        try:
            values[name] = parameter.get() if hasattr(parameter, "get") else parameter
        except Exception:  # noqa: BLE001 - an unreadable parameter is not calibration
            continue
    return values


def submodules_of(owner: Any) -> dict[str, Any]:
    """Every submodule of *owner* — ``rxy``, ``clock_freqs`` and friends."""
    return dict(getattr(owner, "submodules", None) or {})


def construction_args(component: Any) -> list[str]:
    """The positional arguments *component*'s class is rebuilt from.

    An element takes its own name; an edge takes the two elements it joins. The
    two schedulers differ over whether those are public attributes, so both
    spellings are tried.
    """
    for parent_attr, child_attr in (
        ("parent_element_name", "child_element_name"),
        ("_parent_element_name", "_child_element_name"),
    ):
        parent = getattr(component, parent_attr, None)
        child = getattr(component, child_attr, None)
        if parent and child:
            return [str(parent), str(child)]
    return [component.name]


def _walk(component: Any, dotted: str) -> tuple[Any, str]:
    """Resolve all but the last segment of *dotted*, returning the owner and name."""
    *path, name = dotted.split(".")
    owner = component
    for segment in path:
        owner = _attr(owner, segment)
    return owner, name


def _name_on(owner: Any, name: str) -> str:
    """The name *owner* actually uses for *name*, following :data:`ALIASES`."""
    for candidate in ALIASES.get(name, (name,)):
        if hasattr(owner, candidate):
            return candidate
    raise ParameterError(
        f"{type(owner).__name__} has no parameter {name!r} "
        f"(tried {', '.join(ALIASES.get(name, (name,)))})"
    )


def _attr(owner: Any, name: str) -> Any:
    return getattr(owner, _name_on(owner, name))
