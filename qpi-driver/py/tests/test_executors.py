import pytest
import xarray as xr
from qpi_driver.executors import resolve_executor
from qpi_driver.executors.base import CircuitPayload, Executor
from qpi_driver.executors.mock import MockExecutor


def test_mock_executor_execute():
    """Verify that MockExecutor runs successfully and returns the correct xarray.Dataset format."""
    executor = MockExecutor(name="mock")
    from qpi_driver.executors.base import JobPayload

    qasm = """OPENQASM 2.0;
include "qelib1.inc";
qreg q[2];
creg c[2];
h q[0];
cx q[0], q[1];
measure q -> c;"""
    payload_dict = {"shots": 500, "circuit": qasm}
    payload = JobPayload.from_dict(payload_dict)
    dataset = executor.execute(payload)

    # Assert return type
    assert isinstance(dataset, xr.Dataset)

    # Assert data variables and coordinates
    assert "0" in dataset
    assert "1" in dataset
    assert len(dataset.coords["acq_index_0"]) == 500
    assert len(dataset.coords["acq_index_1"]) == 500

    # Assert attributes
    assert dataset.attrs.get("shots") == 500
    assert dataset.attrs.get("backend") == "mock"


def test_resolve_executor():
    """Verify executor resolution logic for strings, classes, and instances."""
    # Resolve by string name
    exec_inst = resolve_executor("mock")
    assert isinstance(exec_inst, MockExecutor)

    # Resolve by class
    exec_inst = resolve_executor(MockExecutor)
    assert isinstance(exec_inst, MockExecutor)

    # Resolve by instance
    inst = MockExecutor()
    exec_inst = resolve_executor(inst)
    assert exec_inst is inst

    # Handle resolution error for unknown name
    with pytest.raises(ValueError) as excinfo:
        resolve_executor("non_existent_executor")
    assert "Unknown executor name" in str(excinfo.value)

    # Handle resolution error for invalid type
    with pytest.raises(TypeError):
        resolve_executor(123)


def test_custom_executor_resolution():
    """An executor the SDK does not ship is used by passing the class itself.

    There is no registry to add it to: the class is the identifier. Its name is
    derived from the class name, which is what makes the derivation worth
    asserting here rather than with a builtin — ``MockExecutor`` derives the
    name ``mock``, which is indistinguishable from its registry key.
    """

    from qpi_driver.executors.base import JobPayload

    class DummyExecutor(Executor):
        def execute(self, payload: JobPayload) -> xr.Dataset:
            return xr.Dataset()

        def process_result(self, dataset: xr.Dataset, job_id: str) -> dict:
            return {}

    exec_inst = resolve_executor(DummyExecutor)
    assert isinstance(exec_inst, DummyExecutor)
    assert exec_inst.name == "dummy"

    # An explicit name, and any other constructor kwarg, still wins.
    exec_inst = resolve_executor(DummyExecutor, name="cryo-qpu")
    assert exec_inst.name == "cryo-qpu"

    # An unregistered *name* is still an error — the class is the only route in.
    with pytest.raises(ValueError, match="Unknown executor name"):
        resolve_executor("dummy")


def test_placeholder_executors_raise_not_implemented():
    """Verify that placeholder executors raise NotImplementedError."""
    from qpi_driver.executors.base import JobPayload

    payload = JobPayload(circuits=[CircuitPayload(circuit="OPENQASM 2.0;")])
    for name in ["presto"]:
        executor = resolve_executor(name)
        with pytest.raises(NotImplementedError):
            executor.execute(payload)


def test_mock_executor_process_result():
    """Verify that MockExecutor.process_result converts xarray.Dataset into correct Qiskit-compatible results."""
    from qpi_driver.executors.base import JobPayload

    executor = MockExecutor(name="mock")

    qasm = """OPENQASM 2.0;
include "qelib1.inc";
qreg q[2];
creg c[2];
x q[0];
measure q -> c;"""

    # Test meas_level=2 (classified counts)
    payload_counts = JobPayload(
        circuits=[CircuitPayload(circuit=qasm)], meas_level=2, shots=100
    )
    dataset_counts = executor.execute(payload_counts)
    res_counts = executor.process_result(dataset_counts, "job-counts")

    assert res_counts["backend"] == "mock"
    assert res_counts["shots"] == 100
    assert "counts" in res_counts
    assert "01" in res_counts["counts"]
    assert "circuit_results" in res_counts
    assert len(res_counts["circuit_results"]) == 1
    assert "hex_counts" not in res_counts

    # Test meas_level=1 (IQ memory)
    payload_iq = JobPayload(
        circuits=[CircuitPayload(circuit=qasm)], meas_level=1, shots=10
    )
    dataset_iq = executor.execute(payload_iq)
    res_iq = executor.process_result(dataset_iq, "job-iq")

    assert res_iq["backend"] == "mock"
    assert "memory" in res_iq
    assert len(res_iq["memory"]) == 10  # shots
    assert len(res_iq["memory"][0]) == 2  # qubits
    assert len(res_iq["memory"][0][0]) == 2  # [real, imag]
    assert "circuit_results" in res_iq
    assert len(res_iq["circuit_results"]) == 1


def test_build_qiskit_result_always_contains_circuit_results():
    """Verify build_qiskit_result produces a consistent shape for single and multi circuit jobs."""
    from qpi_driver.executors.utils.result import build_qiskit_result

    # Single circuit experiment result
    single_exp = [{"counts": {"01": 100}, "shots": 100}]
    single_res = build_qiskit_result(single_exp, "job-1", "backend-test")

    assert single_res["backend"] == "backend-test"
    assert single_res["shots"] == 100
    assert single_res["success"] is True
    assert single_res["circuit_results"] == single_exp
    assert single_res["counts"] == {"01": 100}
    assert "hex_counts" not in single_res

    # Multi circuit experiment results
    multi_exp = [
        {"counts": {"01": 100}, "shots": 100},
        {"counts": {"10": 200}, "shots": 200},
    ]
    multi_res = build_qiskit_result(multi_exp, "job-2", "backend-test")

    assert multi_res["backend"] == "backend-test"
    assert multi_res["shots"] == 100
    assert multi_res["circuit_results"] == multi_exp
    assert multi_res["counts"] == {"01": 100}
    assert "hex_counts" not in multi_res


def test_mock_executor_repeated_measurement_yields_independent_bits():
    """Measuring the same qubit into two different clbits must record both bits independently."""
    from qpi_driver.executors.base import JobPayload

    executor = MockExecutor(name="mock")

    qasm = """OPENQASM 2.0;
include "qelib1.inc";
qreg q[1];
creg c[2];
x q[0];
measure q[0] -> c[0];
measure q[0] -> c[1];"""

    payload = JobPayload(circuits=[CircuitPayload(circuit=qasm)], shots=20)
    dataset = executor.execute(payload)
    result = executor.process_result(dataset, "job-repeated-measurement")
    counts = result["counts"]

    assert sum(counts.values()) == 20
    assert set(counts) == {"00", "01", "10", "11"}
    # q0 is flipped to |1> by the X gate, so both c[0] and c[1] read 1 every shot.
    assert counts["11"] == 20


def test_mock_executor_clbit_remap_positions_bits_by_clbit_index():
    """measure q[0]->c[1]; q[1]->c[0] must place bits by clbit index, not qubit index."""
    from qpi_driver.executors.base import JobPayload

    executor = MockExecutor(name="mock")

    qasm = """OPENQASM 2.0;
include "qelib1.inc";
qreg q[2];
creg c[2];
x q[0];
measure q[0] -> c[1];
measure q[1] -> c[0];"""

    payload = JobPayload(circuits=[CircuitPayload(circuit=qasm)], shots=20)
    dataset = executor.execute(payload)
    result = executor.process_result(dataset, "job-clbit-remap")
    counts = result["counts"]

    assert sum(counts.values()) == 20
    # q0=1 -> c[1] (leftmost); q1=0 -> c[0] (rightmost).
    assert counts["10"] == 20


def test_mock_executor_heterogeneous_clbit_batch():
    """A batch of circuits with different creg widths must not force a shared bit axis."""
    from qpi_driver.executors.base import JobPayload

    executor = MockExecutor(name="mock")

    two_bit = """OPENQASM 2.0;
include "qelib1.inc";
qreg q[2];
creg c[2];
x q[0];
measure q -> c;"""
    three_bit = """OPENQASM 2.0;
include "qelib1.inc";
qreg q[3];
creg c[3];
x q[1];
measure q -> c;"""

    payload = JobPayload(
        circuits=[
            CircuitPayload(circuit=two_bit, shots=30),
            CircuitPayload(circuit=three_bit, shots=50),
        ],
        shots=1024,
    )
    dataset = executor.execute(payload)
    result = executor.process_result(dataset, "job-heterogeneous")

    circuit_results = result["circuit_results"]
    assert len(circuit_results) == 2

    first, second = circuit_results
    # Per-circuit shots reflect the overrides actually used, not payload.shots.
    assert first["shots"] == 30
    assert second["shots"] == 50
    assert sum(first["counts"].values()) == 30
    assert sum(second["counts"].values()) == 50
    # Each circuit keeps its own bit width.
    assert all(len(state) == 2 for state in first["counts"])
    assert all(len(state) == 3 for state in second["counts"])
    # q0=1 -> c[0] (rightmost) for the 2-bit circuit; q1=1 -> c[1] for the 3-bit one.
    assert first["counts"]["01"] == 30
    assert second["counts"]["010"] == 50


def test_mock_executor_partial_measurement_pads_unmeasured_clbit():
    """Measuring only c[1] out of a 2-bit creg must default the unmeasured c[0] to '0'."""
    from qpi_driver.executors.base import JobPayload

    executor = MockExecutor(name="mock")

    qasm = """OPENQASM 2.0;
include "qelib1.inc";
qreg q[2];
creg c[2];
x q[1];
measure q[1] -> c[1];"""

    payload = JobPayload(circuits=[CircuitPayload(circuit=qasm)], shots=20)
    dataset = executor.execute(payload)
    result = executor.process_result(dataset, "job-partial-measurement")
    counts = result["counts"]

    assert sum(counts.values()) == 20
    assert len(counts) == 4
    assert counts["10"] == 20


def test_close_is_a_no_op_by_default():
    """An executor with nothing to release need not implement close()."""
    from qpi_driver.executors import MockExecutor

    MockExecutor(name="mock").close()  # must not raise


def test_the_mock_executor_binds_circuit_parameters():
    """A parameterised circuit is bound before transpiling, not sent as-is."""
    from qpi_driver.executors import MockExecutor
    from qpi_driver.executors.base import CircuitPayload, JobPayload

    qasm = """OPENQASM 3.0;
include "stdgates.inc";
input float[64] theta;
qubit[1] q;
bit[1] c;
rx(theta) q[0];
c[0] = measure q[0];
"""
    payload = JobPayload(
        circuits=[CircuitPayload(circuit=qasm, parameter_values=[[0.0]])],
        id="parameterised",
        shots=8,
    )

    dataset = MockExecutor(name="mock").execute(payload)
    results = MockExecutor(name="mock").process_result(dataset, "parameterised")

    assert results["success"] is True
    assert sum(results["counts"].values()) == 8
