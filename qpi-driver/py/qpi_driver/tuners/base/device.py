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


def element_names(device: Any) -> list[str]:
    """The device's qubit names, however this scheduler exposes them."""
    return _names(getattr(device, "elements", None))


def edge_names(device: Any) -> list[str]:
    """The device's edge names, however this scheduler exposes them.

    quantify's ``QuantumDevice.elements``/``edges`` are methods returning names;
    qblox's are dicts of name to object. Calling the wrong one raises
    ``'dict' object is not callable`` — which is how the qblox write-back was
    found to have never worked.
    """
    return _names(getattr(device, "edges", None))


def _names(accessor: Any) -> list[str]:
    if accessor is None:
        return []
    return list(accessor() if callable(accessor) else accessor)


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


def edge_endpoints(component: Any) -> tuple[str, str] | None:
    """The two element names an edge joins, or ``None`` for a plain element.

    The two schedulers differ over whether those are public attributes, so both
    spellings are tried.
    """
    for parent_attr, child_attr in (
        ("parent_element_name", "child_element_name"),
        ("_parent_element_name", "_child_element_name"),
    ):
        parent = getattr(component, parent_attr, None)
        child = getattr(component, child_attr, None)
        if parent and child:
            return str(parent), str(child)
    return None


def phase_correction_names(edge: Any) -> tuple[str, str] | None:
    """The two parameters an edge's CZ holds its virtual-Z corrections in.

    The schedulers disagree on the spelling, and neither is ``phase_correction``:
    quantify's `CompositeSquareEdge` names them after the qubits
    (``q0_phase_correction``), qblox's after the roles
    (``parent_phase_correction``). Asking the edge which it has is the only way
    to write to it that works on both — and writing to a name neither uses is
    how the conditional-phase routine came to apply nothing at all.

    Returns ``(parent_name, child_name)``, or ``None`` for an edge with no CZ
    corrections to set.
    """
    cz = getattr(edge, "cz", None)
    endpoints = edge_endpoints(edge)
    if cz is None or endpoints is None:
        return None

    parent, child = endpoints
    for names in (
        (f"{parent}_phase_correction", f"{child}_phase_correction"),
        ("parent_phase_correction", "child_phase_correction"),
    ):
        if all(hasattr(cz, name) for name in names):
            return names
    return None


def drag_parameter_name(element: Any) -> str | None:
    """What this element's ``rxy`` calls its DRAG parameter.

    The same disagreement as :func:`phase_correction_names`, one level down:
    quantify's transmon has ``rxy.motzoi``, qblox's has ``rxy.beta``. They are not
    even the same quantity — see `SchedulerBackend.drag_span` — but for writing a
    fitted value back, what matters is that the name exists. `drag` wrote
    ``rxy.motzoi`` unconditionally, so under qblox it measured an optimum and then
    had nowhere to put it.

    Returns ``None`` for an element with no DRAG parameter at all.
    """
    rxy = getattr(element, "rxy", None)
    if rxy is None:
        return None
    for name in ("motzoi", "beta"):
        if hasattr(rxy, name):
            return name
    return None


def construction_args(component: Any) -> list[str]:
    """The positional arguments *component*'s class is rebuilt from.

    An element takes its own name. An edge takes none: its two endpoints go
    through :func:`construction_kwargs` instead, because qblox's edges are
    pydantic models and pydantic accepts no positional arguments at all — a
    file written with them parses and then fails to instantiate.
    """
    return [] if edge_endpoints(component) else [component.name]


def construction_kwargs(component: Any) -> dict[str, str]:
    """The keyword arguments *component*'s class is rebuilt from.

    Named rather than positional for edges, which is the spelling both
    schedulers accept — quantify's qcodes edge takes them either way and
    qblox's pydantic one only this way.
    """
    endpoints = edge_endpoints(component)
    if endpoints is None:
        return {}
    parent, child = endpoints
    return {"parent_element_name": parent, "child_element_name": child}


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
