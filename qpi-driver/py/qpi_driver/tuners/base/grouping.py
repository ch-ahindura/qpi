"""Which targets may be calibrated at once (RFC 0009 §5).

Conflict is proximity in the coupling graph, and the radius is a parameter — the
literature settles the shape and leaves the number to measurement (RFC 0009 §5.1).
Chip geometry does not come into it: two qubits close together with no coupler
between them are farther apart, for this purpose, than two adjacent ones.

Everything here is a function of a config and a few numbers. The device is read in
one place, :func:`outputs_of`, and only to find out which targets share an
instrument output.
"""

import logging
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

log = logging.getLogger(__name__)

#: Qubit names in an edge name, as ``calibration.yml`` writes them.
_EDGE_PARTS = 2


def endpoints_of(target: str) -> tuple[str, ...]:
    """The qubits *target* occupies — itself for a qubit, both ends for an edge.

    Unifying the two is what lets one distance rule serve both: a CZ conflicts with
    whatever either of its qubits conflicts with.
    """
    parts = target.split("_")
    if len(parts) == _EDGE_PARTS and all(parts):
        return tuple(parts)
    return (target,)


def couplings_of(edges: Iterable[str]) -> dict[str, set[str]]:
    """Each qubit's neighbours, from ``<parent>_<child>`` edge names."""
    adjacency: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        endpoints = endpoints_of(edge)
        if len(endpoints) != _EDGE_PARTS:
            continue
        parent, child = endpoints
        adjacency[parent].add(child)
        adjacency[child].add(parent)
    return dict(adjacency)


def is_too_close(
    a: str, b: str, adjacency: Mapping[str, set[str]], spacing: int
) -> bool:
    """Whether *a* and *b* are nearer than *spacing* hops apart.

    The distance between two edges is the shortest between their endpoint sets, and
    zero when they share a qubit — so ``spacing=1`` asks only that two couplers be
    disjoint, and ``spacing=2`` puts a qubit between them.

    Breadth-first and bounded by *spacing*, because the question is never how far
    apart two targets are, only whether they are far enough.
    """
    if spacing <= 0:
        return False
    ends_a = set(endpoints_of(a))
    ends_b = set(endpoints_of(b))
    frontier, seen = ends_a, set(ends_a)
    for _ in range(spacing):
        if frontier & ends_b:
            return True
        frontier = {
            neighbour
            for qubit in frontier
            for neighbour in adjacency.get(qubit, ())
            if neighbour not in seen
        }
        seen |= frontier
    return False


def groups_of(
    targets: Sequence[str],
    *,
    adjacency: Mapping[str, set[str]],
    spacing: int,
    exclude: Iterable[Sequence[str]] = (),
    max_group: int,
) -> list[list[str]]:
    """*targets* split into sets that can be measured at once.

    Greedy colouring of the conflict graph, in the order *targets* are given, so the
    same config always produces the same groups and a run is reproducible. Greedy
    reaches the optimum on the topologies anyone builds: two classes on any bipartite
    lattice at the default spacing, and Vizing's bound for couplers.
    """
    banned = {frozenset(pair) for pair in exclude if len(pair) == _EDGE_PARTS}
    classes: list[list[str]] = []
    for target in targets:
        for members in classes:
            if len(members) < max_group and not any(
                _conflicts(target, member, adjacency, spacing, banned)
                for member in members
            ):
                members.append(target)
                break
        else:
            classes.append([target])
    return classes


def readout_misfit(
    clocks: Sequence[float],
    amplitudes: Sequence[float],
    *,
    band_hz: float,
    sequencers: int = 0,
) -> str | None:
    """Why these readouts cannot share one output, or ``None`` if they can (§5.4).

    Three ceilings, all arithmetic on the configs. The message names the figure and
    the ceiling rather than saying the group is too wide, because only the first of
    those is something an operator can act on.
    """
    if len(clocks) > 1:
        centre = (min(clocks) + max(clocks)) / 2
        worst = max(abs(clock - centre) for clock in clocks)
        if worst > band_hz:
            return (
                f"readout clocks span {(max(clocks) - min(clocks)) / 1e6:.1f} MHz, so the "
                f"furthest sits {worst / 1e6:.1f} MHz from the band centre against an "
                f"addressable {band_hz / 1e6:.0f} MHz"
            )

    total = sum(abs(amplitude) for amplitude in amplitudes)
    if total > 1.0:
        return (
            f"readout amplitudes sum to {total:.3f} of full scale, so the tones would "
            f"clip when added"
        )

    if sequencers and len(clocks) > sequencers:
        return (
            f"{len(clocks)} readout clocks on one output needs that many sequencers, "
            f"and the module has {sequencers}"
        )
    return None


def split_to_fit(
    group: Sequence[str], misfit: Callable[[Sequence[str]], str | None]
) -> list[list[str]]:
    """*group* bisected until every part fits, per *misfit*.

    Bisected rather than refused: a group an operator's spacing produced is a
    statement about their chip, and the answer to one the instruments cannot play is
    fewer targets per schedule. A single target that still does not fit is returned
    alone, which is the sequential behaviour.
    """
    reason = misfit(group)
    if reason is None:
        return [list(group)]
    if len(group) < 2:
        log.warning("%s alone does not fit: %s", group[0], reason)
        return [list(group)]
    log.info("splitting %s: %s", ", ".join(group), reason)
    half = len(group) // 2
    return split_to_fit(group[:half], misfit) + split_to_fit(group[half:], misfit)


def outputs_of(device: Any) -> dict[str, str]:
    """Which instrument output each port is wired to, from the hardware config.

    The one thing here that reads a device, and it reads the connectivity as an
    iterable of ``(a, b)`` pairs — the shape both the config file and a graph object
    present. Empty when the wiring cannot be read, which leaves every target looking
    like it has an output to itself: the same convention `has_flux_port` uses, and it
    leaves the group exactly as the colouring produced it.
    """
    try:
        graph = device.hardware_config().connectivity.graph
        pairs = graph.edges if hasattr(graph, "edges") else graph
        wiring: dict[str, str] = {}
        for first, second in pairs:
            # The port is the end naming a target, and either end may be it.
            port, output = (second, first) if ":" in str(second) else (first, second)
            wiring[str(port)] = str(output)
        return wiring
    except Exception:  # noqa: BLE001 - unreadable wiring imposes no constraint
        log.debug("could not read the wiring; no shared-output limits", exc_info=True)
        return {}


def by_output(
    targets: Sequence[str], wiring: Mapping[str, str], port: str
) -> dict[str, list[str]]:
    """*targets* grouped by the output their *port* resolves to.

    Targets whose port the wiring does not name are grouped under the port itself, so
    an unreadable or partial wiring keeps them together and the checks still apply.
    """
    shared: dict[str, list[str]] = defaultdict(list)
    for target in targets:
        shared[wiring.get(f"{target}:{port}", port)].append(target)
    return dict(shared)


def _conflicts(
    a: str,
    b: str,
    adjacency: Mapping[str, set[str]],
    spacing: int,
    banned: set[frozenset[str]],
) -> bool:
    if is_too_close(a, b, adjacency, spacing):
        return True
    return any(
        frozenset((first, second)) in banned
        for first in endpoints_of(a)
        for second in endpoints_of(b)
    )
