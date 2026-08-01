"""Unit tests for the shared clbit-mapping/counts-assembly helper.

These exercise ``build_acquisition_counts`` directly against synthetic
datasets shaped like real Qblox/Quantify acquisition output -- dims
``(repetition, acq_index_<channel>)`` plus a ``clbit_map`` attr -- so the
core bit-placement logic is covered even on platforms where qblox-scheduler
and quantify-scheduler cannot be installed.
"""

import numpy as np
import xarray as xr
from qpi_driver.executors.utils.counts import build_acquisition_counts, parse_clbit_map


def test_double_measurement_yields_independent_bits():
    """q0 measured twice into c0 and c1 must keep both measurements, not just the last."""
    channel_values = {0: [[0, 1], [1, 0]]}  # acq_index 0 shots, acq_index 1 shots
    clbit_map = [(0, 0, 0), (0, 1, 1)]
    dataset = _make_dataset(channel_values, shots=2, clbit_map=clbit_map, num_clbits=2)

    counts = build_acquisition_counts(
        dataset, qubit_vars=[0], discriminate=_discriminate
    )

    assert counts["10"] == 1
    assert counts["01"] == 1
    assert counts["00"] == 0
    assert counts["11"] == 0
    assert sum(counts.values()) == 2


def test_partial_measurement_pads_unmeasured_clbits_with_zero():
    """Measuring only c1 must still yield a num_clbits-wide string with c0 as a gap."""
    channel_values = {0: [[1, 1]]}
    clbit_map = [(0, 0, 1)]  # qubit 0's only measurement targets c1, not c0
    dataset = _make_dataset(channel_values, shots=2, clbit_map=clbit_map, num_clbits=2)

    counts = build_acquisition_counts(
        dataset, qubit_vars=[0], discriminate=_discriminate
    )

    assert counts["10"] == 2
    assert set(counts) == {"00", "01", "10", "11"}


def test_bit_position_follows_clbit_index_not_qubit_index():
    """measure q[0]->c[1]; q[1]->c[0] must place bits by clbit index, not qubit index."""
    channel_values = {0: [[1]], 1: [[0]]}
    clbit_map = [(0, 0, 1), (1, 0, 0)]
    dataset = _make_dataset(channel_values, shots=1, clbit_map=clbit_map, num_clbits=2)

    counts = build_acquisition_counts(
        dataset, qubit_vars=[0, 1], discriminate=_discriminate
    )

    # q0=1 -> c1 (leftmost), q1=0 -> c0 (rightmost): "10", not the qubit-ordered "01"
    assert counts["10"] == 1
    assert counts["01"] == 0


def test_single_measurement_per_qubit_matches_legacy_qubit_ordering():
    """Identity qubit->clbit mapping must reproduce the pre-existing bitstring convention."""
    channel_values = {0: [[1]], 1: [[0]]}
    clbit_map = [(0, 0, 0), (1, 0, 1)]
    dataset = _make_dataset(channel_values, shots=1, clbit_map=clbit_map, num_clbits=2)

    counts = build_acquisition_counts(
        dataset, qubit_vars=[0, 1], discriminate=_discriminate
    )

    assert counts["01"] == 1


def test_missing_clbit_map_falls_back_to_final_measurement_per_qubit():
    """Older datasets without a clbit_map keep one bit per qubit, final measurement only."""
    # qubit 0 measured twice (acq_index 0 then 1); only the final one should count.
    channel_values = {0: [[0], [1]]}
    dataset = _make_dataset(channel_values, shots=1, clbit_map=None, num_clbits=0)

    counts = build_acquisition_counts(
        dataset, qubit_vars=[0], discriminate=_discriminate
    )

    assert counts["1"] == 1
    assert counts["0"] == 0


def test_parse_clbit_map_returns_none_when_absent():
    dataset = xr.Dataset({"0": xr.DataArray([1.0 + 0j])})
    clbit_map, num_clbits = parse_clbit_map(dataset)
    assert clbit_map is None
    assert num_clbits == 0


def _make_dataset(
    channel_values: dict[int, list[list[complex]]],
    *,
    shots: int,
    clbit_map: list[tuple[int, int, int]] | None,
    num_clbits: int,
) -> xr.Dataset:
    """Build a fake acquisition dataset.

    ``channel_values[q]`` is a list of per-acquisition-index shot lists, i.e.
    ``channel_values[q][acq_idx]`` holds ``shots`` values for that particular
    measurement on qubit ``q``.
    """
    data_vars = {}
    for q_idx, per_acq in channel_values.items():
        arr = np.array(per_acq, dtype=complex).T  # shape (shots, num_acquisitions)
        data_vars[str(q_idx)] = xr.DataArray(
            arr,
            dims=["repetition", f"acq_index_{q_idx}"],
            coords={
                "repetition": list(range(shots)),
                f"acq_index_{q_idx}": list(range(len(per_acq))),
            },
        )
    dataset = xr.Dataset(data_vars)
    dataset.attrs["shots"] = shots
    dataset.attrs["num_clbits"] = num_clbits
    if clbit_map is not None:
        dataset.attrs["clbit_map"] = [list(entry) for entry in clbit_map]
    return dataset


def _discriminate(val: complex, _qubit: int = 0) -> str:
    """Takes the qubit index the real one does, and ignores it: these tests are
    about bit *placement*, not about which line each qubit is assigned against."""
    r = val.real if not np.isnan(val.real) else 0.0
    return "1" if r >= 1 else "0"
