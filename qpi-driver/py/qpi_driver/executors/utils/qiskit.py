import re

import xarray as xr
from qiskit import QuantumCircuit, qasm2, qasm3

_QASM_VERSION_PATTERN = re.compile(r"OPENQASM\s+(2|3)(?:\.\d+)?\s*;")


def load_qasm(value: str, num_qubits: int | None = None) -> QuantumCircuit:
    """Loads QASM from the QASM string

    Args:
        value: the QASM string from which to load the circuit
        num_qubits: provides number of physical/virtual qubits

    Returns:
        the QASM circuit as got from the string value passed

    Raises:
        ValueError: missing or unrecognized OPENQASM version header
        ValueError: Failed to parse QASM circuit: exc
    """
    version = _detect_qasm_version(value)
    if version is None:
        raise ValueError(
            "Failed to parse QASM circuit: missing or unrecognized OPENQASM "
            "version header, expected 'OPENQASM 2.0;' or 'OPENQASM 3.0;'"
        )

    try:
        if version == 3:
            return qasm3.loads(value, num_qubits=num_qubits)
        return qasm2.loads(value, strict=True)
    except Exception as exc:
        raise ValueError(f"Failed to parse QASM circuit: {exc}") from exc


def memory_to_dataset(
    memory: list[str], n_clbits: int, shots: int, meas_level: int
) -> xr.Dataset:
    """Convert shot memory (list of bitstrings) into an xr.Dataset.

    Qiskit's own simulators already resolve each ``measure q[i] -> c[j]``
    mapping (including repeated measurements or remapped clbits) into a
    single bitstring per shot, positioned by classical bit index. This just
    fans that string out into one data variable per classical bit.

    The returned dataset format depends on ``meas_level``:

    * ``meas_level=2`` – Store as ``complex(bit_value, 0.0)``.
    * ``meas_level=1`` – Synthesize IQ-like complex values with Gaussian noise
      so that downstream IQ processing pipelines can be tested against simulators.
    * ``meas_level=0`` – Not supported for simulators (raises ``NotImplementedError``).

    Args:
        memory: List of bitstring outcomes (e.g. ['00', '01', '11']).
        n_clbits: Number of classical bits in the circuit.
        shots: Number of shots.
        meas_level: Measurement level (0=raw IQ, 1=kerneled IQ, 2=counts).

    Returns:
        xr.Dataset with one data variable per classical bit.

    Raises:
        NotImplementedError: If ``meas_level=0`` is requested.
    """
    if meas_level == 0:
        raise NotImplementedError(
            "Raw waveform data (meas_level=0) is not available from simulators"
        )

    import numpy as np

    data_vars = {}
    for i in range(n_clbits):
        # clbit 0 is the rightmost (LSB) character, so its index is n_clbits - 1 - i.
        bit_vals = [float(outcome[n_clbits - 1 - i]) for outcome in memory]

        if meas_level == 1:
            # Synthesize IQ-like complex values with Gaussian noise
            # |0⟩ cluster centred at (0.1, 0.05), |1⟩ cluster centred at (0.9, 0.85)
            iq_vals = []
            for val in bit_vals:
                noise_i = float(np.random.normal(0, 0.05))
                noise_q = float(np.random.normal(0, 0.05))
                if val < 0.5:  # state |0⟩
                    iq_vals.append(complex(0.1 + noise_i, 0.05 + noise_q))
                else:  # state |1⟩
                    iq_vals.append(complex(0.9 + noise_i, 0.85 + noise_q))
            data_vars[str(i)] = xr.DataArray(
                iq_vals,
                dims=[f"acq_index_{i}"],
                coords={f"acq_index_{i}": list(range(shots))},
            )
        else:
            # meas_level=2: classified counts as complex(bit, 0)
            data_vars[str(i)] = xr.DataArray(
                [complex(val, 0.0) for val in bit_vals],
                dims=[f"acq_index_{i}"],
                coords={f"acq_index_{i}": list(range(shots))},
            )
    return xr.Dataset(data_vars)


def _detect_qasm_version(value: str) -> int | None:
    """Detects the OpenQASM major version declared in a QASM string.

    Args:
        value: the QASM string to inspect

    Returns:
        2 or 3 if a version header is found, else None
    """
    match = _QASM_VERSION_PATTERN.search(value)
    return int(match.group(1)) if match else None


def measured_qubits(circuit: "QuantumCircuit") -> list[int]:
    """Qubit indices the circuit measures, in order, without repeats.

    Both schedulers need this for the same reason: a Qblox module can put one
    sequencer into scope mode, so a raw trace over several qubits has to be
    taken one qubit per run.
    """
    from qiskit.circuit import library as qiskit_library

    found: list[int] = []
    for instruction in circuit.data:
        if not isinstance(instruction.operation, qiskit_library.Measure):
            continue
        for qubit in instruction.qubits:
            index = circuit.find_bit(qubit).index
            if index not in found:
                found.append(index)
    return found
