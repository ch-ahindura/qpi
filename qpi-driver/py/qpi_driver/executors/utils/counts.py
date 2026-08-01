"""Shared helpers for turning acquisition/measurement datasets into Qiskit-style counts.

Two families of executors produce counts dicts from an ``xr.Dataset``:

* Hardware-acquisition executors (Qblox, Quantify) store one data variable per
  *qubit* acquisition channel, with a separate acquisition-index axis for each
  measurement performed on that qubit. Reading out the correct classical
  bitstring requires knowing which classical bit each ``(qubit, acq_index)``
  measurement was meant for -- the ``clbit_map`` captured at schedule
  generation time (see ``build_acquisition_counts``).
* Simulator executors (Mock, Qiskit Aer) delegate the qubit/clbit bookkeeping
  to Qiskit itself: ``memory_to_dataset`` stores one data variable per
  classical bit, already ordered and collapsed the way Qiskit's own
  ``Result.get_memory`` reports it (see ``simulator_dataset_to_result``).
"""

from typing import Any, Callable

import numpy as np
import xarray as xr

from qpi_driver.executors.utils.types import cast_to


def qubit_key(dataset: xr.Dataset, q_idx: int) -> Any:
    """Return the data-variable key for a channel, tolerating int or str names."""
    return q_idx if q_idx in dataset else str(q_idx)


def per_shot_values(data_array: xr.DataArray, index: int = -1) -> np.ndarray:
    """Return a 1D per-shot array for one acquisition of a channel.

    Single-shot acquisitions (``BinMode.APPEND``) arrive with dims
    ``(repetition, acq_index_<ch>)``; averaged acquisitions carry only the
    acquisition-index axis. The repetition axis, when present, becomes the
    shot axis. ``index`` selects which acquisition (measurement) to read off
    the acquisition-index axis; the default of ``-1`` preserves the legacy
    behavior of reading a channel's final measurement.
    """
    values = np.asarray(data_array.values)
    if "repetition" in data_array.dims:
        values = np.moveaxis(values, data_array.dims.index("repetition"), 0)
    else:
        values = values[np.newaxis, ...]
    values = values.reshape(values.shape[0], -1)
    width = values.shape[1]
    idx = index if -width <= index < width else -1
    return values[:, idx]


def parse_clbit_map(
    dataset: xr.Dataset,
) -> tuple[list[tuple[int, int, int]] | None, int]:
    """Read the qubit/acquisition -> clbit mapping stashed in ``dataset.attrs``.

    Args:
        dataset: Acquisition dataset, as produced by ``execute()``.

    Returns:
        Tuple of ``(clbit_map, num_clbits)`` where ``clbit_map`` is a list of
        ``(qubit_idx, acq_index, clbit_idx)`` triples. ``clbit_map`` is
        ``None`` for datasets that predate this mapping, signalling callers
        to fall back to one-bit-per-qubit behavior.
    """
    raw_map = dataset.attrs.get("clbit_map")
    if raw_map is None:
        return None, 0

    clbit_map = [(int(q), int(a), int(c)) for q, a, c in raw_map]
    num_clbits = dataset.attrs.get("num_clbits")
    if num_clbits is None:
        num_clbits = max((c for _, _, c in clbit_map), default=-1) + 1
    return clbit_map, int(num_clbits)


def build_discriminator(
    dataset: xr.Dataset,
    acq_protocol: str,
    get_threshold_params: Callable[[], dict[int, dict[str, float]]],
) -> Callable[[complex, int], str]:
    """Build a function mapping one qubit's raw acquisition value to a "0"/"1" bit.

    Shared by the Qblox and Quantify executors, whose only difference is how the
    device defaults are looked up (hence the ``get_threshold_params`` callback).

    Takes the qubit index as well as the value, because the rotation and threshold
    are properties of that qubit's readout chain. Applying one qubit's line to
    another's shots assigns them against a boundary drawn somewhere else — invisible
    while every qubit's line was the uncalibrated `0`/`0`, and wrong as soon as
    `readout_discrimination` measures them.

    Args:
        dataset: Acquisition dataset, carrying per-job overrides in its attrs.
        acq_protocol: "ThresholdedAcquisition" or "SSBIntegrationComplex".
        get_threshold_params: Returns each qubit's rotation and threshold by index,
            used for qubits the dataset attrs do not carry.

    Returns:
        Callable discriminating one raw acquisition value for one qubit.
    """
    if acq_protocol == "ThresholdedAcquisition":

        def discriminate(val: complex, _qubit: int = 0) -> str:
            r = val.real if not np.isnan(val.real) else 0.0
            return "1" if r >= 0.5 else "0"

        return discriminate

    lines = _discriminator_lines(dataset, get_threshold_params)

    def discriminate(val: complex, qubit: int = 0) -> str:
        if np.isnan(val.real) and np.isnan(val.imag):
            return "0"
        rotation, threshold = lines.get(qubit, (0.0, 0.0))
        # Negative, matching `fit_readout_discrimination`, which defines the rotation
        # as the direction from one cloud centre to the other and then turns the
        # plane *back* by it so the separation lands on the real axis. The simulated
        # instrument applies the same sign. Turning the other way is the same
        # rotation only when it is zero, which is why an uncalibrated chip could not
        # show the difference.
        rotated = val * np.exp(-1j * np.radians(rotation))
        return "1" if rotated.real > threshold else "0"

    return discriminate


def _discriminator_lines(
    dataset: xr.Dataset,
    get_threshold_params: Callable[[], dict[int, dict[str, float]]],
) -> dict[int, tuple[float, float]]:
    """Each qubit's ``(rotation, threshold)``, from the job first and the device after.

    Also accepts the single ``acq_rotation``/``acq_threshold`` pair older datasets
    carry, applying it to every qubit — which is what those datasets meant by it.
    """
    lines = {
        int(index): (
            float(v.get("acq_rotation", 0.0)),
            float(v.get("acq_threshold", 0.0)),
        )
        for index, v in (dataset.attrs.get("acq_discriminators") or {}).items()
    }
    if lines:
        return lines

    legacy_rotation = dataset.attrs.get("acq_rotation")
    legacy_threshold = dataset.attrs.get("acq_threshold")
    if legacy_rotation is not None and legacy_threshold is not None:
        pair = (float(legacy_rotation), float(legacy_threshold))
        return _EveryQubit(pair)

    return {
        int(index): (
            float(values.get("acq_rotation", 0.0)),
            float(values.get("acq_threshold", 0.0)),
        )
        for index, values in (get_threshold_params() or {}).items()
    }


class _EveryQubit(dict):
    """A mapping that answers with the same line whatever qubit is asked for."""

    def __init__(self, pair: tuple[float, float]) -> None:
        super().__init__()
        self._pair = pair

    def get(self, _key, _default=None):  # noqa: D102 - dict's own contract
        return self._pair


def build_acquisition_counts(
    dataset: xr.Dataset,
    qubit_vars: list[int],
    discriminate: Callable[[complex, int], str],
) -> dict[str, int]:
    """Assemble a Qiskit-style counts dict from a hardware acquisition dataset.

    When the dataset carries a ``clbit_map``, every recorded measurement is
    discriminated independently and placed at its own classical-bit position
    (little-endian: clbit 0 is the rightmost character), so a qubit measured
    more than once yields independent, correctly-positioned bits. Without a
    ``clbit_map`` (older datasets), falls back to the legacy behavior: one
    bit per qubit, ordered by qubit index (qubit 0 rightmost), keeping only
    each qubit's final measurement.

    Args:
        dataset: Dataset with one data variable per qubit acquisition channel.
        qubit_vars: Sorted qubit indices present in the dataset.
        discriminate: Maps one qubit's raw acquisition value to ``"0"`` or ``"1"``.

    Returns:
        Binary-string-keyed counts dict, zero-padded for every state of the
        resulting bit width.
    """
    clbit_map, num_clbits = parse_clbit_map(dataset)
    counts: dict[str, int] = {}

    if clbit_map:
        measurements = [
            (
                clbit_idx,
                q_idx,
                per_shot_values(dataset[qubit_key(dataset, q_idx)], acq_idx),
            )
            for q_idx, acq_idx, clbit_idx in clbit_map
            if q_idx in qubit_vars
        ]
        width = num_clbits
        num_samples = len(measurements[0][2]) if measurements else 0
        for s in range(num_samples):
            bits = ["0"] * width
            for clbit_idx, q_idx, values in measurements:
                bits[width - 1 - clbit_idx] = discriminate(values[s], q_idx)
            state = "".join(bits)
            counts[state] = counts.get(state, 0) + 1
    else:
        width = len(qubit_vars)
        per_shot = {
            q_idx: per_shot_values(dataset[qubit_key(dataset, q_idx)])
            for q_idx in qubit_vars
        }
        num_samples = len(per_shot[qubit_vars[0]]) if qubit_vars else 0
        for s in range(num_samples):
            bits = [
                discriminate(per_shot[q_idx][s], q_idx)
                for q_idx in reversed(qubit_vars)
            ]
            state = "".join(bits)
            counts[state] = counts.get(state, 0) + 1

    for i in range(2**width):
        state = format(i, f"0{width}b")
        counts.setdefault(state, 0)

    return counts


def simulator_dataset_to_result(
    dataset: xr.Dataset, meas_level: int, meas_return: str
) -> dict:
    """Extract counts or IQ memory from a single-circuit simulator dataset slice.

    Shared by ``MockExecutor`` and ``QiskitAerExecutor``, whose datasets are
    built by ``memory_to_dataset`` with one data variable per measured
    classical bit, already correctly positioned by Qiskit's own simulator.

    Args:
        dataset: Dataset produced by ``memory_to_dataset``.
        meas_level: 1 for IQ memory, otherwise classified counts.
        meas_return: "avg" to average IQ memory across shots.

    Returns:
        dict with either "memory" or "counts", plus "shots".
    """
    clbit_vars = []
    for var in dataset.data_vars:
        try:
            clbit_vars.append(int(var))
        except ValueError:
            pass
    if not clbit_vars:
        return {"raw": str(dataset), "shots": 0}

    clbit_vars.sort()
    c0_key = str(clbit_vars[0])
    shots = cast_to(int, dataset.attrs.get("shots"), len(dataset[c0_key]))

    if meas_level == 1:
        from qpi_driver.executors.utils.result import iq_memory_avg

        memory: list[list[list[float]]] = []
        num_samples = len(dataset[c0_key])
        for s in range(num_samples):
            shot_iq = []
            for clbit_idx in clbit_vars:
                val = dataset[str(clbit_idx)].values[s]
                shot_iq.append([float(val.real), float(val.imag)])
            memory.append(shot_iq)

        if meas_return == "avg" and memory:
            memory = iq_memory_avg(memory, len(clbit_vars))

        return {"memory": memory, "shots": shots}

    n_clbits = len(clbit_vars)
    counts_dict: dict[str, int] = {}
    num_samples = len(dataset[c0_key])
    for s in range(num_samples):
        bits = []
        for clbit_idx in reversed(clbit_vars):
            val = dataset[str(clbit_idx)].values[s]
            bits.append("1" if val.real > 0.5 else "0")
        state = "".join(bits)
        counts_dict[state] = counts_dict.get(state, 0) + 1

    if num_samples == 1 and shots > 1:
        state = next(iter(counts_dict))
        counts_dict = {state: shots}

    for i in range(2**n_clbits):
        state = format(i, f"0{n_clbits}b")
        counts_dict.setdefault(state, 0)

    return {"counts": counts_dict, "shots": shots}
