from typing import Literal, Optional

import numpy as np
import qiskit.circuit
from qiskit import QuantumCircuit
from qiskit.circuit import library as qiskit_library

from qpi_driver.compat.qblox import (
    CZ,
    H,
    IdlePulse,
    Measure,
    Operation,
    Reset,
    Rxy,
    Rz,
    S,
    Schedule,
    SDagger,
    SetClockFrequency,
    T,
    TDagger,
    X,
    Y,
    Z,
)

_RefPtNewType = Optional[Literal["start", "center", "end"]]


def to_qblox_gates(
    circuit: QuantumCircuit,
    instruction: qiskit.circuit.CircuitInstruction,
    acq_indices: dict[int, int],
    acq_protocol: str = "SSBIntegrationComplex",
    acq_kwargs: dict | None = None,
    acq_overrides: dict[int, dict] | None = None,
    clbit_map: list[tuple[int, int, int]] | None = None,
    only_qubit: int | None = None,
) -> list[Operation]:
    """Converts a qiskit Instruction to Qblox gate operations.

    Args:
        circuit: The circuit in which the instruction is.
        instruction: The instruction to convert.
        acq_indices: A mapping of qubit and current acquisitions/measurements done on said qubit in circuit..
        acq_protocol: Acquisition protocol to use when measuring.
        acq_kwargs: Additional arguments passed for acquisition.
        acq_overrides: Per-qubit acquisition arguments, keyed by qubit index,
            merged over ``acq_kwargs``. The discriminator is per qubit — each
            has its own readout chain and so its own rotation and threshold —
            and applying one qubit's to all of them misassigns the others.
        clbit_map: If given, appended to with a ``(qubit_idx, acq_index, clbit_idx)``
            triple for every Measure operation, recording which classical bit
            each acquisition targets.

    Returns:
        list of qblox Operations

    Raises:
        ValueError: if the gate is not supported by QbloxExecutor
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
        c, t = qubits[0], qubits[1]
        return [H(t), CZ(qC=c, qT=t), H(t)]

    if isinstance(gate, qiskit_library.CCXGate):
        c1, c2, t = qubits[0], qubits[1], qubits[2]
        return [
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

    if isinstance(gate, qiskit_library.CZGate):
        return [CZ(qC=qubits[0], qT=qubits[1])]

    if isinstance(gate, qiskit_library.CRZGate):
        c, t = qubits[0], qubits[1]
        theta_deg = float(np.degrees(params[0]))
        return _crz_ops(c, t, theta_deg)

    if isinstance(gate, qiskit_library.CPhaseGate):
        # CPhase(theta) = CRZ(theta) with an extra Rz(theta/2) on the control:
        # CPhase and RZ differ by a global phase e^{i*theta/2} that only becomes
        # observable, as a phase conditioned on the control qubit, once the
        # gate is controlled. See CRZ/CPhase identity used in _crz_ops.
        c, t = qubits[0], qubits[1]
        theta_deg = float(np.degrees(params[0]))
        return _crz_ops(c, t, theta_deg) + [Rz(theta_deg / 2, c)]

    if isinstance(gate, qiskit_library.SwapGate):
        c, t = qubits[0], qubits[1]
        return [
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
        # which is why CRZGate and CPhaseGate below are handled separately
        # rather than falling through to this branch.
        theta_deg = float(np.degrees(params[0]))
        return [Rz(theta_deg, q) for q in qubits]

    if isinstance(gate, qiskit_library.UGate):
        theta_deg = float(np.degrees(params[0]))
        phi_deg = float(np.degrees(params[1]))
        lam_deg = float(np.degrees(params[2]))

        ops = []
        for q in qubits:
            ops.extend(
                [
                    Rz(theta=lam_deg, qubit=q),
                    Rxy(theta=theta_deg, phi=90.0, qubit=q),
                    Rz(theta=phi_deg, qubit=q),
                ]
            )
        return ops

    if isinstance(gate, qiskit_library.Measure):
        result = []
        extra = acq_kwargs or {}
        overrides = acq_overrides or {}
        clbit_indices = [circuit.find_bit(c).index for c in instruction.clbits]
        for idx, clbit_idx in zip(qubit_indices, clbit_indices):
            acq_idx = acq_indices.get(idx, 0)
            if only_qubit is not None and idx != only_qubit:
                continue
            # Use unique acq_channel per qubit to avoid overlaps
            result.append(
                Measure(
                    f"q{idx}",
                    acq_channel=idx,
                    acq_index=acq_idx,
                    acq_protocol=acq_protocol,
                    **{**extra, **overrides.get(idx, {})},
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

    raise ValueError(f"Gate '{name}' is not supported by QbloxExecutor")


def generate_schedule(
    name: str,
    circuit: QuantumCircuit,
    shots: int,
    acq_protocol: str,
    acq_kwargs: dict,
    acq_overrides: dict[int, dict] | None = None,
    readout_points: dict[int, dict[str, float]] | None = None,
    only_qubit: int | None = None,
) -> tuple[Schedule, list[tuple[int, int, int]], int]:
    """Generate a schedule from the given circuit.

    Args:
        name: the name of the schedule
        circuit: the Qiskit circuit specifying shots, circuits, meas_level, etc.
        shots: number of shots for the experiment
        acq_protocol: Acquisition protocol to use when measuring
        acq_kwargs: Additional arguments passed for acquisition.
        acq_overrides: Per-qubit acquisition arguments, keyed by qubit index.
        readout_points: Per-qubit readout frequency for discriminated shots. The
            measure operation's clock is fixed at ``{qubit}.ro``, so the frequency is
            applied by moving that clock rather than as a `Measure` argument.

    Returns:
        A tuple of the Schedule with all the proper timings, the clbit_map
        (a list of ``(qubit_idx, acq_index, clbit_idx)`` triples recording
        which classical bit each measurement targets), and the number of
        classical bits declared in the circuit.
    """
    schedule = Schedule(name=name, repetitions=shots)
    # Only for thresholded shots: the point that best separates the two clouds is not
    # the one that returns the most signal, so applying it to a raw trace or to level-1
    # IQ would degrade exactly the measurements that want the signal.
    if acq_protocol == "ThresholdedAcquisition":
        for index, point in sorted((readout_points or {}).items()):
            if only_qubit is not None and index != only_qubit:
                continue
            schedule.add(
                SetClockFrequency(
                    clock=f"q{index}.ro", clock_freq_new=point["frequency"]
                )
            )
    acq_indices: dict[int, int] = {}
    clbit_map: list[tuple[int, int, int]] = []

    for instruction in circuit.data:
        parsed_ops = to_qblox_gates(
            circuit=circuit,
            instruction=instruction,
            acq_indices=acq_indices,
            acq_protocol=acq_protocol,
            acq_kwargs=acq_kwargs,
            acq_overrides=acq_overrides,
            clbit_map=clbit_map,
            only_qubit=only_qubit,
        )

        import qiskit

        is_parallel_op = isinstance(
            instruction.operation,
            (qiskit.circuit.Measure, qiskit.circuit.Delay, qiskit.circuit.Barrier),
        )

        if is_parallel_op and parsed_ops:
            first_op = schedule.add(parsed_ops[0])
            for op in parsed_ops[1:]:
                schedule.add(op, ref_op=first_op, ref_pt="start")
        else:
            for op in parsed_ops:
                schedule.add(op)

    return schedule, clbit_map, circuit.num_clbits


def _to_multiple_of_4(time_ns: float) -> int:
    """Convert a time ns to multiple of 4"""
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
