"""Reading either scheduler's compiled schedule.

quantify-scheduler and qblox-scheduler describe a compiled schedule almost
identically and not quite. The differences are small, undocumented, and would
otherwise be scattered through the coordinator's walk, so they are named here
instead:

- `pulse_info` and `acquisition_info` are a *list* of dicts under quantify and
  a single dict under qblox;
- a subschedule is a `Schedule` with `.operations` under quantify, and a
  `TimeableSchedule` carrying `operation_dict` inside `.data` under qblox —
  and qblox nests them several deep where quantify has one level;
- a drive pulse's amplitude is `G_amp` under quantify and `amplitude` under
  qblox, with `amp` used by both for square pulses.

quantify-scheduler is being deprecated, so the qblox spelling is the one that
has to keep working; supporting both is what makes that transition a
non-event rather than a rewrite of the simulator.
"""

from typing import Any


def read_operations(schedule: Any) -> Any:
    """The operation table of a compiled schedule, by either spelling."""
    operations = getattr(schedule, "operations", None)
    if operations is not None:
        return operations
    return schedule.data["operation_dict"]


def is_subschedule(operation: Any) -> bool:
    """Whether an operation is itself a schedule to descend into."""
    if getattr(operation, "schedulables", None) is not None:
        return True
    data = getattr(operation, "data", None)
    return isinstance(data, dict) and "schedulables" in data


def read_info_entries(operation: Any, key: str) -> list[dict]:
    """``pulse_info`` or ``acquisition_info`` as a list, whichever shape it is."""
    data = getattr(operation, "data", None)
    if not isinstance(data, dict):
        return []
    info = data.get(key)
    if not info:
        return []
    return list(info) if isinstance(info, (list, tuple)) else [info]


def read_amplitude(pulse: dict) -> Any:
    """A drive pulse's amplitude, by whichever key the scheduler used."""
    for key in ("G_amp", "amplitude", "amp"):
        if pulse.get(key) is not None:
            return pulse[key]
    return None
