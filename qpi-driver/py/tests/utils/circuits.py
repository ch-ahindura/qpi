"""Comparing a gate-conversion function's output against qiskit's own unitary."""

from typing import Any, Callable, Iterable, Sequence

import numpy as np
from qiskit import QuantumCircuit
from qiskit.quantum_info import Operator

# Every unitary gate branch handled by to_qblox_gates/to_quantify_gates, paired
# with a function that applies it to a fresh circuit. Reset/Measure/Delay/Barrier
# are excluded since they aren't fixed unitaries.
GATE_CONVERSION_CASES: list[tuple[str, int, Callable[[QuantumCircuit], None]]] = [
    ("x", 1, lambda qc: qc.x(0)),
    ("y", 1, lambda qc: qc.y(0)),
    ("z", 1, lambda qc: qc.z(0)),
    ("h", 1, lambda qc: qc.h(0)),
    ("s", 1, lambda qc: qc.s(0)),
    ("sdg", 1, lambda qc: qc.sdg(0)),
    ("t", 1, lambda qc: qc.t(0)),
    ("tdg", 1, lambda qc: qc.tdg(0)),
    ("sx", 1, lambda qc: qc.sx(0)),
    ("sxdg", 1, lambda qc: qc.sxdg(0)),
    ("rx", 1, lambda qc: qc.rx(0.37, 0)),
    ("ry", 1, lambda qc: qc.ry(0.51, 0)),
    ("rz", 1, lambda qc: qc.rz(0.63, 0)),
    ("p", 1, lambda qc: qc.p(0.63, 0)),
    ("u", 1, lambda qc: qc.u(0.3, 0.4, 0.5, 0)),
    ("cx", 2, lambda qc: qc.cx(0, 1)),
    ("cz", 2, lambda qc: qc.cz(0, 1)),
    ("swap", 2, lambda qc: qc.swap(0, 1)),
    ("crz", 2, lambda qc: qc.crz(0.63, 0, 1)),
    ("cp", 2, lambda qc: qc.cp(0.63, 0, 1)),
    ("ccx", 3, lambda qc: qc.ccx(0, 1, 2)),
]


def operations_to_unitary(
    operations: Iterable[Any], qubits: Sequence[str]
) -> np.ndarray:
    """Build the unitary of a list of quantify/qblox gate operations.

    Each operation's own ``gate_info`` unitary is embedded onto the global qubit
    space, so the result reflects exactly what the emitted gates mean. Qubits are
    ordered as given, with the first qubit the most significant bit.
    """
    order = list(qubits)
    position = {qubit: bit for bit, qubit in enumerate(order)}
    num_qubits = len(order)
    unitary = np.eye(2**num_qubits, dtype=complex)

    for operation in operations:
        gate_info = operation.data["gate_info"]
        gate_unitary = np.asarray(gate_info["unitary"], dtype=complex)
        targets = [position[qubit] for qubit in _gate_qubits(gate_info)]
        unitary = _embed(gate_unitary, targets, num_qubits) @ unitary

    return unitary


def equal_up_to_global_phase(a: np.ndarray, b: np.ndarray, atol: float = 1e-9) -> bool:
    """Whether two matrices are equal up to a global phase factor."""
    pivot = np.unravel_index(np.argmax(np.abs(a)), a.shape)
    if abs(a[pivot]) < atol:
        return False
    phase = b[pivot] / a[pivot]
    return bool(np.allclose(a * phase, b, atol=atol))


def qiskit_unitary(gate: Any) -> np.ndarray:
    """Reference unitary for a qiskit gate, in the qubit-order convention used by
    operations_to_unitary/_embed (first qubit is the most significant bit).

    Qiskit's own ``Operator(gate).data`` is little-endian (first qubit is the
    least significant bit), so ``reverse_qargs()`` flips it to line up.
    """
    return Operator(gate).reverse_qargs().data


def assert_gate_conversion_matches_qiskit(
    to_gates_fn: Callable[..., Iterable[Any]],
    num_qubits: int,
    apply_gate: Callable[[QuantumCircuit], None],
) -> None:
    """Asserts a gate-conversion function reproduces qiskit's own unitary for that gate.

    Builds a circuit with a single gate application, converts it via
    ``to_gates_fn``, and compares the resulting operations' unitary against
    qiskit's reference, up to a global phase.
    """
    circuit = QuantumCircuit(num_qubits)
    apply_gate(circuit)
    instruction = circuit.data[0]
    qubits = [f"q{idx}" for idx in range(num_qubits)]

    operations = to_gates_fn(circuit=circuit, instruction=instruction, acq_indices={})

    actual = operations_to_unitary(operations, qubits)
    expected = qiskit_unitary(instruction.operation)
    assert equal_up_to_global_phase(actual, expected)


def _gate_qubits(gate_info: dict) -> list[str]:
    return gate_info.get("device_elements") or gate_info["qubits"]


def _embed(
    gate_unitary: np.ndarray, targets: Sequence[int], num_qubits: int
) -> np.ndarray:
    """Embed a gate acting on ``targets`` into the full ``num_qubits`` space.

    ``targets`` are ordered to match the tensor factors of ``gate_unitary``, with
    the first target the most significant qubit of the gate.
    """
    dimension = 2**num_qubits
    num_targets = len(targets)
    embedded = np.zeros((dimension, dimension), dtype=complex)

    for column in range(dimension):
        column_bits = [
            (column >> (num_qubits - 1 - qubit)) & 1 for qubit in range(num_qubits)
        ]
        gate_column = 0
        for qubit in targets:
            gate_column = (gate_column << 1) | column_bits[qubit]

        for gate_row in range(2**num_targets):
            amplitude = gate_unitary[gate_row, gate_column]
            if amplitude == 0:
                continue
            row_bits = list(column_bits)
            for offset, qubit in enumerate(targets):
                row_bits[qubit] = (gate_row >> (num_targets - 1 - offset)) & 1
            row = 0
            for bit in row_bits:
                row = (row << 1) | bit
            embedded[row, column] += amplitude

    return embedded
