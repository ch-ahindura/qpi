"""One schedule over several targets, and the dataset it returns (RFC 0009 §6).

A Qblox cluster is one arm-and-start resource, so parallelism is not several
schedules submitted at once — the scheduler's own ``start`` disarms every sequencer
in the cluster before arming its own, and two submissions would have no shared time
origin anyway. Simultaneity has to be expressed *inside* one schedule, and that is
what :func:`add_together` is for.

The dataset comes back with one variable per acquisition channel, and
:func:`channel_of` slices it so each routine's `analyse` sees the single-variable
shape it already reads. No fit learns that it ran in company.
"""

import logging
from collections.abc import Callable, Iterable, Sequence
from typing import Any

log = logging.getLogger(__name__)


def add_together(schedule: Any, operations: Iterable[Any]) -> Any:
    """Add every operation to *schedule* at one start time, and return the anchor.

    ``schedule.add`` appends, so a group's resets and pulses would otherwise play one
    after another and a fused schedule would be exactly as long as the sequential run
    it is meant to replace.

    The anchor is the longest of them, so a following stage referencing its end cannot
    begin before every operation in this one has finished — which matters when a
    group's targets are configured with different pulse or readout durations.
    """
    placed: list[tuple[Any, Any]] = []
    anchor = None
    for operation in operations:
        if anchor is None:
            anchor = schedule.add(operation)
            placed.append((operation, anchor))
            continue
        placed.append(
            (
                operation,
                schedule.add(
                    operation, ref_op=anchor, ref_pt="start", ref_pt_new="start"
                ),
            )
        )
    if not placed:
        return None
    return max(placed, key=lambda pair: _duration_of(pair[0]))[1]


def add_after(schedule: Any, operations: Iterable[Any], anchor: Any) -> Any:
    """The same, starting where *anchor* ends rather than where the schedule does."""
    operations = list(operations)
    if not operations:
        return anchor
    first = schedule.add(operations[0], ref_op=anchor, ref_pt="end", ref_pt_new="start")
    placed = [(operations[0], first)]
    for operation in operations[1:]:
        placed.append(
            (
                operation,
                schedule.add(
                    operation, ref_op=first, ref_pt="start", ref_pt_new="start"
                ),
            )
        )
    return max(placed, key=lambda pair: _duration_of(pair[0]))[1]


def channel_of(dataset: Any, channel: int) -> Any:
    """Acquisition *channel* alone, in the shape `signal_of` reads.

    Data variables are keyed by the integer channel, so this is a selection and not a
    reconstruction. Falls back to the whole dataset when it carries no such channel and
    only one variable, which is the unfused shape: a group of one must go down exactly
    the path it did before fusion existed.
    """
    variables = list(getattr(dataset, "data_vars", {}) or {})
    if channel in variables:
        return dataset[[channel]]
    if len(variables) == 1 and channel == 0:
        return dataset
    raise KeyError(
        f"the acquisition has no channel {channel}; it carries {variables or 'nothing'}"
    )


def grouped_by_grid(
    targets: Iterable[str], grid_of: Callable[[str], Iterable[float]]
) -> list[list[str]]:
    """*targets* partitioned by the grid each one resolves (RFC 0009 D7).

    What `CalibrationRoutine.compatible_groups` is usually built from: a routine whose
    sweep is derived per target hands the grid function in and gets back the subgroups
    that agree. Partitioned rather than isolating the odd one out, so three qubits that
    agree are still measured together when a fourth does not.

    Order is the targets' own, so the same config always produces the same subgroups.
    """
    by_grid: dict[tuple[float, ...], list[str]] = {}
    for target in targets:
        by_grid.setdefault(tuple(grid_of(target)), []).append(target)
    return list(by_grid.values())


def channels_of(dataset: Any, targets: Sequence[str]) -> dict[str, Any]:
    """Each target's own slice of a fused acquisition, by position in the group.

    A target whose channel is missing is left out rather than given someone else's
    data — the walk records that as its own failure and the rest of the group stands.
    """
    sliced: dict[str, Any] = {}
    for channel, target in enumerate(targets):
        try:
            sliced[target] = channel_of(dataset, channel)
        except KeyError as absent:
            log.warning("%s has no acquisition in this group: %s", target, absent)
    return sliced


def _duration_of(operation: Any) -> float:
    duration = getattr(operation, "duration", None)
    if duration is None:
        duration = getattr(operation, "kwargs", {}).get("duration")
    try:
        return float(duration)
    except (TypeError, ValueError):
        return 0.0
