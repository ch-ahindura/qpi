"""Which rotation and threshold each qubit's shots are assigned with.

Every ``meas_level=2`` shot is a comparison: rotate the IQ point, then ask whether
its real part clears a threshold. Both numbers are properties of *one* qubit's
readout chain — its own resonator, its own cable, its own amplifier — so they differ
from qubit to qubit, and `readout_discrimination` measures them per qubit.

The executors used to read them from whichever element happened to have them first
and apply that to every qubit in the circuit. That was invisible while nothing
measured them, since an uncalibrated chip has zero everywhere and one qubit's zero is
as good as another's. It stopped being invisible the moment they were calibrated:
q0's rotation applied to q1 assigns q1's shots against a line drawn somewhere else.

Both executors resolve them through here, because the rule is the same and the two
differ only in how a parameter is read.
"""

from typing import Any

from qpi_driver.tuners.base.device import (
    ParameterError,
    element_names,
    read_path,
)

#: Read from each element, and passed to `Measure` under the same names.
DISCRIMINATOR_PATHS = ("measure.acq_rotation", "measure.acq_threshold")


def qubit_index(element_name: str) -> int | None:
    """The integer in ``q3``, or ``None`` for anything not shaped like a qubit.

    Acquisition channels are numbered by qubit index, so a discriminator can only be
    matched to shots through that number.
    """
    if not element_name.startswith("q"):
        return None
    try:
        return int(element_name[1:])
    except ValueError:
        return None


def discriminators_by_qubit(device: Any) -> dict[int, dict[str, float]]:
    """Each qubit's rotation and threshold, keyed by qubit index.

    A qubit missing either is left out rather than defaulted: absent means
    uncalibrated, and the caller decides what to do about that. Defaulting to zero
    here would hand back a confident-looking discriminator that assigns every shot to
    whichever side of the origin the chain happens to put both clouds.
    """
    found: dict[int, dict[str, float]] = {}
    for name in element_names(device):
        index = qubit_index(name)
        if index is None:
            continue
        element = _element(device, name)
        try:
            values = [read_path(element, path) for path in DISCRIMINATOR_PATHS]
        except (ParameterError, AttributeError, KeyError):
            continue
        if any(value is None for value in values):
            continue
        rotation, threshold = values
        found[index] = {
            "acq_rotation": float(rotation),
            "acq_threshold": float(threshold),
        }
    return found


def resolve_discriminators(
    device: Any, rotation: float | None, threshold: float | None
) -> tuple[dict[int, dict[str, float]], bool]:
    """Per-qubit discriminators, and whether every qubit on the device has one.

    *rotation* and *threshold* come from the job payload and override the device for
    **every** qubit when given — a job asking for a particular line is asking for it
    on all of its shots, not on whichever qubit the device left uncalibrated.

    The flag is what decides between asking the instrument to threshold and doing it
    in software afterwards. It is deliberately all-or-nothing: a schedule mixing the
    two protocols would return one qubit's bits alongside another's raw IQ, and the
    counts assembled from that would be silently part nonsense.
    """
    found = discriminators_by_qubit(device)
    indices = [
        index
        for index in (qubit_index(name) for name in element_names(device))
        if index is not None
    ]

    if rotation is not None or threshold is not None:
        for index in indices:
            entry = found.setdefault(index, {})
            if rotation is not None:
                entry["acq_rotation"] = float(rotation)
            if threshold is not None:
                entry["acq_threshold"] = float(threshold)

    complete = bool(indices) and all(
        len(found.get(index, {})) == len(DISCRIMINATOR_PATHS) for index in indices
    )
    return found, complete


def _element(device: Any, name: str) -> Any:
    """One element, however this scheduler hands them out."""
    if hasattr(device, "get_element"):
        return device.get_element(name)
    return device.elements[name]
