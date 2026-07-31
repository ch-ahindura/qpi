import numpy as np
import qiskit.circuit
from qiskit import QuantumCircuit
from qiskit.circuit import library as qiskit_library

from qpi_driver.compat.quantify import (
    CZ,
    H,
    IdlePulse,
    Measure,
    Operation,
    Reset,
    Rxy,
    Rz,
    S,
    SDagger,
    T,
    TDagger,
    X,
    Y,
    Z,
)


def to_quantify_gates(
    circuit: QuantumCircuit,
    instruction: qiskit.circuit.CircuitInstruction,
    acq_indices: dict[int, int],
    acq_protocol: str = "SSBIntegrationComplex",
    acq_kwargs: dict | None = None,
    clbit_map: list[tuple[int, int, int]] | None = None,
    only_qubit: int | None = None,
) -> list[Operation]:
    """Converts a qiskit Instruction to Quantify gate operations.

    Args:
        circuit: The circuit in which the instruction is.
        instruction: The instruction to convert.
        acq_indices: A mapping of qubit and current acquisitions/measurements done on said qubit in circuit..
        acq_protocol: Acquisition protocol to use when measuring.
        acq_kwargs: Additional arguments passed for acquisition.
        clbit_map: If given, appended to with a ``(qubit_idx, acq_index, clbit_idx)``
            triple for every Measure operation, recording which classical bit
            each acquisition targets.
        only_qubit: If given, acquire on that qubit alone and leave the other
            measurements out of the schedule. This exists for raw traces: a
            Qblox module can put only one sequencer into scope mode, so a
            multi-qubit trace has to be taken one qubit per run.

    Returns:
        list of quantify Operations

    Raises:
        ValueError: if the gate is not supported by QuantifyExecutor
    """
    gate = instruction.operation
    name = instruction.name
    params = instruction.params

    qubit_indices = [circuit.find_bit(q).index for q in instruction.qubits]
    qubits = [f"q{idx}" for idx in qubit_indices]

    if isinstance(gate, qiskit_library.Reset):
        return [Reset(q) for q in qubits]

    if isinstance(gate, qiskit_library.XGate):
        return [X(q) for q in qubits]

    if isinstance(gate, qiskit_library.YGate):
        return [Y(q) for q in qubits]

    if isinstance(gate, qiskit_library.ZGate):
        return [Z(q) for q in qubits]

    if isinstance(gate, qiskit_library.HGate):
        return [H(q) for q in qubits]

    if isinstance(gate, qiskit_library.SGate):
        return [S(q) for q in qubits]

    if isinstance(gate, qiskit_library.SdgGate):
        return [SDagger(q) for q in qubits]

    if isinstance(gate, qiskit_library.TGate):
        return [T(q) for q in qubits]

    if isinstance(gate, qiskit_library.TdgGate):
        return [TDagger(q) for q in qubits]

    if isinstance(gate, qiskit_library.SXGate):
        return [Rxy(theta=90.0, phi=0.0, qubit=q) for q in qubits]

    if isinstance(gate, qiskit_library.SXdgGate):
        return [Rxy(theta=-90.0, phi=0.0, qubit=q) for q in qubits]

    if isinstance(gate, qiskit_library.CXGate):
        # CNOT: is not natively supported by quantify
        ops = []
        for c, t in zip(qubits[0::2], qubits[1::2]):
            ops.extend(
                [
                    H(t),
                    CZ(qC=c, qT=t),
                    H(t),
                ]
            )
        return ops

    if isinstance(gate, qiskit_library.CZGate):
        return [CZ(qC=c, qT=t) for c, t in zip(qubits[0::2], qubits[1::2])]

    if isinstance(gate, qiskit_library.CRZGate):
        theta_deg = float(np.degrees(params[0]))
        ops = []
        for c, t in zip(qubits[0::2], qubits[1::2]):
            ops.extend(_crz_ops(c, t, theta_deg))
        return ops

    if isinstance(gate, qiskit_library.CPhaseGate):
        # CPhase(theta) = CRZ(theta) with an extra Rz(theta/2) on the control:
        # CPhase and RZ differ by a global phase e^{i*theta/2} that only becomes
        # observable, as a phase conditioned on the control qubit, once the
        # gate is controlled. See CRZ/CPhase identity used in _crz_ops.
        theta_deg = float(np.degrees(params[0]))
        ops = []
        for c, t in zip(qubits[0::2], qubits[1::2]):
            ops.extend(_crz_ops(c, t, theta_deg))
            ops.append(Rz(theta_deg / 2, c))
        return ops

    if isinstance(gate, qiskit_library.CCXGate):
        # the Toffoli gate. Do note that CX is not natively supported so each CX in
        # the standard 6-CNOT decomposition is expanded to H, CZ, H.
        ops = []
        for c1, c2, t in zip(qubits[0::3], qubits[1::3], qubits[2::3]):
            ops.extend(
                [
                    H(t),
                    H(t),
                    CZ(qC=c2, qT=t),
                    H(t),
                    TDagger(t),
                    H(t),
                    CZ(qC=c1, qT=t),
                    H(t),
                    T(t),
                    H(t),
                    CZ(qC=c2, qT=t),
                    H(t),
                    TDagger(t),
                    H(t),
                    CZ(qC=c1, qT=t),
                    H(t),
                    T(c2),
                    T(t),
                    H(t),
                    H(c2),
                    CZ(qC=c1, qT=c2),
                    H(c2),
                    T(c1),
                    TDagger(c2),
                    H(c2),
                    CZ(qC=c1, qT=c2),
                    H(c2),
                ]
            )
        return ops

    if isinstance(gate, qiskit_library.SwapGate):
        # Expands each control/target pair into 3 CNOT operations sequentially
        # but since CNOT is not natively supported, we decompose it to H, CZ, H
        ops = []
        for c, t in zip(qubits[0::2], qubits[1::2]):
            ops.extend(
                [
                    H(t),
                    CZ(qC=c, qT=t),
                    H(t),
                    H(c),
                    CZ(qC=c, qT=t),
                    H(c),
                    H(t),
                    CZ(qC=c, qT=t),
                    H(t),
                ]
            )
        return ops

    if isinstance(gate, qiskit_library.RXGate):
        theta_deg = float(np.degrees(params[0]))
        return [Rxy(theta_deg, 0, q) for q in qubits]

    if isinstance(gate, qiskit_library.RYGate):
        theta_deg = float(np.degrees(params[0]))
        return [Rxy(theta_deg, 90, q) for q in qubits]

    if isinstance(gate, (qiskit_library.RZGate, qiskit_library.PhaseGate)):
        # PhaseGate(theta) and RZGate(theta) differ only by a global phase
        # (e^{i*theta/2}), which is unobservable for a standalone gate, so both
        # map to the same Rz. Once controlled, that phase becomes observable,
        # which is why CRZGate and CPhaseGate above are handled separately
        # rather than falling through to this branch.
        theta_deg = float(np.degrees(params[0]))
        return [Rz(theta_deg, q) for q in qubits]

    elif isinstance(gate, qiskit_library.UGate):
        theta_deg = float(np.degrees(params[0]))
        phi_deg = float(np.degrees(params[1]))
        lam_deg = float(np.degrees(params[2]))

        # Flattening a 1-to-3 sequence per qubit using a nested list comprehension
        return [
            gate
            for q in qubits
            for gate in [
                Rz(theta=lam_deg, qubit=q),
                Rxy(theta=theta_deg, phi=90.0, qubit=q),
                Rz(theta=phi_deg, qubit=q),
            ]
        ]

    if isinstance(gate, qiskit_library.Measure):
        result = []
        extra = acq_kwargs or {}
        clbit_indices = [circuit.find_bit(c).index for c in instruction.clbits]
        for idx, clbit_idx in zip(qubit_indices, clbit_indices):
            if only_qubit is not None and idx != only_qubit:
                continue
            acq_idx = acq_indices.get(idx, 0)
            # Use unique acq_channel per qubit to avoid overlaps
            result.append(
                Measure(
                    f"q{idx}",
                    acq_channel=idx,
                    acq_index=acq_idx,
                    acq_protocol=acq_protocol,
                    **extra,
                )
            )
            if clbit_map is not None:
                clbit_map.append((idx, acq_idx, clbit_idx))
            # update the measurement count for that qubit to allow for multiple measurements on a qubit
            acq_indices[idx] = acq_idx + 1
        return result

    if isinstance(gate, qiskit.circuit.Delay):
        duration = gate.duration
        unit = gate.unit
        if unit == "s":
            time_ns = duration * 1e9
        elif unit == "ms":
            time_ns = duration * 1e6
        elif unit == "us":
            time_ns = duration * 1e3
        elif unit == "ns":
            time_ns = duration
        else:
            raise ValueError(f"Delay unit '{unit}' is not supported without a backend.")

        duration_s = _to_multiple_of_4(time_ns) * 1e-9
        return [IdlePulse(duration=duration_s)]

    if isinstance(gate, qiskit_library.Barrier):
        return []

    raise ValueError(f"Gate '{name}' is not supported by QuantifyExecutor")


def _to_multiple_of_4(time_ns: float) -> int:
    """Converts the given time_ns to a multiple of 4"""
    return int(round(time_ns / 4.0) * 4)


def _crz_ops(control: str, target: str, theta_deg: float) -> list[Operation]:
    """Builds the CZ-based decomposition of a controlled-RZ(theta_deg) gate.

    Uses the standard identity Rz(theta/2)_t, CNOT, Rz(-theta/2)_t, CNOT, with
    CNOT expanded to H, CZ, H since CNOT isn't native here.
    """
    return [
        Rz(theta_deg / 2, target),
        H(target),
        CZ(qC=control, qT=target),
        H(target),
        Rz(-theta_deg / 2, target),
        H(target),
        CZ(qC=control, qT=target),
        H(target),
    ]
