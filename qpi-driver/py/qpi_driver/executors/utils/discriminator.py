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
DISCRIMINATOR_PATHS = ("acq_rotation", "acq_threshold")

#: Preferred first. A `CalibratedTransmon` carries a readout operating point chosen
#: for *discrimination* rather than for signal, and a line fitted there — see that
#: element. An element without one falls back to `measure`, which is what every
#: config written before it has.
DISCRIMINATOR_SUBMODULES = ("measure_2state", "measure")

#: Also read from `measure_2state`, and applied only to discriminated shots. The
#: frequency is not a `Measure` argument: the measure operation's clock is fixed at
#: ``{qubit}.ro`` in the device config, so it is overridden by setting that clock.
READOUT_POINT_PATHS = ("frequency", "pulse_amp")


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
        line = _line_of(_element(device, name))
        if line is not None:
            found[index] = line
    return found


def _line_of(element: Any) -> dict[str, float] | None:
    """The discriminator this element carries, from the best submodule that has one.

    A `measure_2state` left at its defaults is not a calibration — the frequency is
    zero, which is nowhere — so it is skipped rather than preferred, and the element
    falls back to whatever `measure` holds.
    """
    for submodule in DISCRIMINATOR_SUBMODULES:
        try:
            values = [
                read_path(element, f"{submodule}.{path}")
                for path in DISCRIMINATOR_PATHS
            ]
        except (ParameterError, AttributeError, KeyError):
            continue
        if any(value is None for value in values):
            continue
        if submodule == "measure_2state" and not readout_point(element):
            continue
        rotation, threshold = values
        return {"acq_rotation": float(rotation), "acq_threshold": float(threshold)}
    return None


def readout_point(element: Any) -> dict[str, float] | None:
    """The frequency and amplitude discriminated shots should be taken at.

    ``None`` when the element has no `measure_2state`, or has one nothing has
    calibrated — a frequency of zero is not a readout, so it means "leave the clock
    where the device config put it" rather than "drive at DC".
    """
    try:
        values = [
            read_path(element, f"measure_2state.{path}") for path in READOUT_POINT_PATHS
        ]
    except (ParameterError, AttributeError, KeyError):
        return None
    if any(value is None for value in values):
        return None
    frequency, amplitude = (float(value) for value in values)
    if frequency <= 0.0 or amplitude <= 0.0:
        return None
    return {"frequency": frequency, "pulse_amp": amplitude}


def readout_points_by_qubit(device: Any) -> dict[int, dict[str, float]]:
    """Each qubit's discriminated-readout operating point, by qubit index."""
    points: dict[int, dict[str, float]] = {}
    for name in element_names(device):
        index = qubit_index(name)
        if index is None:
            continue
        point = readout_point(_element(device, name))
        if point is not None:
            points[index] = point
    return points


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
