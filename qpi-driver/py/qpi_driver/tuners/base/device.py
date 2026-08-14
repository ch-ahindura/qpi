"""Reading and writing device parameters across two schedulers (RFC 0004 §6.2).

The two device models are not the same shape. quantify-scheduler builds on
qcodes, where a parameter is a callable — ``element.rxy.amp180()`` reads and
``element.rxy.amp180(0.1)`` writes. qblox-scheduler uses pydantic models, where
the same parameter is a plain float attribute.

A routine that knew the difference would have to be written twice, so it does
not: everything here takes either shape. The alternative — two copies of every
routine — is exactly what this package is arranged to avoid.
"""

from collections.abc import Collection
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


def has_path(component: Any, dotted: str) -> bool:
    """Whether *dotted* resolves on *component* at all.

    What separates a parameter a routine chose not to write from one this element has
    nowhere to keep: several `updates` are opt-in fields of `CalibratedTransmon` that a
    plain `BasicTransmonElement` does not have, and `apply` skips those rather than
    failing (RFC 0008 phase 2).
    """
    try:
        owner, name = _walk(component, dotted)
        _name_on(owner, name)
    except Exception:  # noqa: BLE001 - an unresolvable path is simply not there
        return False
    return True


def component_for(device: Any, target: str, kind: str = "qubits") -> Any:
    """The element or edge named *target*, or ``None`` if it cannot be resolved."""
    accessor = "get_edge" if kind == "edges" else "get_element"
    try:
        return getattr(device, accessor)(target)
    except Exception:  # noqa: BLE001 - an unresolvable target is not an error here
        return None


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


def parameters_of(owner: Any, skip: Collection[str] = ()) -> dict[str, Any]:
    """Every readable parameter of *owner* as plain values.

    qcodes exposes ``parameters`` as a mapping of Parameter objects and pydantic
    as a mapping of values already; both reduce to the same dict here.

    *skip* is applied before the read, not after: reading a qcodes parameter runs
    its getter, and ``IDN``'s sends ``*IDN?`` to a device element that has no
    ``ask`` — which qcodes logs as a warning with a full traceback. Filtering the
    result instead still pays for the read.
    """
    values: dict[str, Any] = {}
    for name, parameter in (getattr(owner, "parameters", None) or {}).items():
        if name in skip:
            continue
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


def spectroscopy_amplitude_path(element: Any, transition: str = "01") -> str | None:
    """Where this element stores its spectroscopy drive amplitude, if anywhere.

    Not a spelling difference like :func:`drag_parameter_name` — a
    `BasicTransmonElement` has no such parameter under any name, and a config is free
    to keep using one. Returning ``None`` is how a routine learns it has nowhere to
    write, so it can decline rather than raise.
    """
    if getattr(element, "spec", None) is None:
        return None
    name = "amplitude" if transition == "01" else f"amplitude_{transition}"
    return f"spec.{name}" if hasattr(element.spec, name) else None


def has_flux_port(device: Any, name: str) -> bool:
    """Whether the wiring routes a flux line to *name* — a qubit or an edge.

    The question a chip's architecture answers. On flux-tunable *qubits* every qubit
    has a ``q<n>:fl`` and a CZ is a baseband pulse pushing one onto the crossing; on a
    flux-tunable *coupler* the flux goes to the coupler instead, the qubits have no
    line of their own, and the CZ is a microwave tone on ``q<n>_q<m>:fl``.

    A routine that plays flux on a port the connectivity does not carry fails deep in
    the compiler with ``KeyError: 'q0:fl was not found in the connectivity.'``, naming
    neither the routine nor the reason. Asked here it is not a failure at all — the
    routine describes the other kind of chip, and declines.

    True when the wiring cannot be read, so an unrecognised config leaves a routine
    running and failing as it did rather than being silently skipped.
    """
    try:
        return f"{name}:fl" in device.hardware_config().connectivity.graph
    except Exception:  # noqa: BLE001 - unreadable wiring is not a declined routine
        return True


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


def resonator_linewidth_path(element: Any) -> str | None:
    """``resonator.linewidth`` if this element has one, else ``None``.

    The same opt-in shape as :func:`spectroscopy_amplitude_path`: a
    `BasicTransmonElement` has nowhere to keep a measured linewidth, and a config using
    one is not broken — the nodes that want it fall back to their own constant.
    """
    submodule = getattr(element, "resonator", None)
    if submodule is None or not hasattr(submodule, "linewidth"):
        return None
    return "resonator.linewidth"


def relaxation_time_path(element: Any) -> str | None:
    """``coherence.t1`` if this element has one, else ``None``.

    The same opt-in shape as :func:`resonator_linewidth_path`.
    """
    submodule = getattr(element, "coherence", None)
    if submodule is None or not hasattr(submodule, "t1"):
        return None
    return "coherence.t1"


def measured_t1(element: Any, fallback: float = 0.0) -> float:
    """What `t1` measured for this qubit, or *fallback*.

    Zero means "not measured", and `fit_t2` skips its ceiling rather than comparing
    against nothing — see :class:`CoherenceTimes`.
    """
    path = relaxation_time_path(element)
    if path is None:
        return fallback
    try:
        value = read_path(element, path)
    except Exception:  # noqa: BLE001 - an unreadable field is an unmeasured one
        return fallback
    return float(value) if value else fallback


def measured_linewidth(element: Any, fallback: float) -> float:
    """What `resonator_spectroscopy` measured for this resonator, or *fallback*.

    Zero counts as absent, which is the convention every `CalibratedTransmon` field
    uses: it is the initial value, so "has a field for it" and "has measured it" are
    different questions and only the second one may size a sweep.
    """
    path = resonator_linewidth_path(element)
    if path is None:
        return fallback
    try:
        value = read_path(element, path)
    except Exception:  # noqa: BLE001 - an unreadable field is an unmeasured one
        return fallback
    return float(value) if value else fallback
