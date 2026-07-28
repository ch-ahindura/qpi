"""The job envelope every executor is handed (RFC 0001 §6).

This is the boundary between QPI-UI's dispatcher and an executor, and the only place
a malformed job is caught — the worker relies on it to turn bad input into a reported
error rather than a confusing failure deep inside a vendor library. Both wire shapes
it accepts are covered, because the server has sent both.
"""

import pytest
from qpi_driver.executors.base import CircuitPayload, JobPayload

QASM = 'OPENQASM 2.0; include "qelib1.inc"; qreg q[1]; creg c[1]; measure q -> c;'


class TestValidation:
    def test_a_blank_id_is_refused(self):
        """The id is what a result is correlated by, so a blank one is unusable."""
        with pytest.raises(ValueError, match="id cannot be empty"):
            JobPayload(circuits=[CircuitPayload(circuit=QASM)], id="   ")

    def test_no_circuits_is_refused(self):
        with pytest.raises(ValueError, match="circuits list cannot be empty"):
            JobPayload(circuits=[])

    def test_a_blank_circuit_is_refused_and_says_which(self):
        """A batch names the offending index; "one of them is empty" is not actionable."""
        with pytest.raises(ValueError, match="Circuit at index 1"):
            JobPayload(
                circuits=[CircuitPayload(circuit=QASM), CircuitPayload(circuit="  ")]
            )

    @pytest.mark.parametrize("level", [-1, 3, 99])
    def test_an_unknown_meas_level_is_refused(self, level):
        with pytest.raises(ValueError, match="meas_level must be 0, 1, or 2"):
            JobPayload(circuits=[CircuitPayload(circuit=QASM)], meas_level=level)

    def test_an_unknown_meas_return_is_refused(self):
        with pytest.raises(ValueError, match="meas_return must be"):
            JobPayload(circuits=[CircuitPayload(circuit=QASM)], meas_return="median")

    def test_the_defaults_are_a_valid_job(self):
        payload = JobPayload(circuits=[CircuitPayload(circuit=QASM)])

        assert payload.shots == 1024
        assert payload.meas_level == 2
        assert payload.meas_return == "single"
        assert payload.id  # a uuid, so results can be correlated
        assert payload.qasm == QASM


class TestFromDict:
    """The wire shapes QPI-UI actually sends."""

    def test_a_list_of_circuit_objects(self):
        payload = JobPayload.from_dict(
            {
                "id": "batch-1",
                "shots": 64,
                "circuits": [
                    {"circuit": QASM, "parameter_values": [[0.5]], "shots": 8},
                    {"qasm": QASM},
                    {"circuit_qasm": QASM},
                ],
            }
        )

        assert payload.id == "batch-1"
        assert payload.shots == 64
        assert len(payload.circuits) == 3
        assert payload.circuits[0].parameter_values == [[0.5]]
        assert payload.circuits[0].shots == 8  # a per-circuit override

    def test_a_list_of_plain_qasm_strings(self):
        payload = JobPayload.from_dict({"circuits": [QASM, QASM]})

        assert [circuit.circuit for circuit in payload.circuits] == [QASM, QASM]

    def test_an_entry_that_is_neither_a_dict_nor_a_string_is_refused(self):
        with pytest.raises(ValueError, match="Invalid circuit entry type"):
            JobPayload.from_dict({"circuits": [42]})

    @pytest.mark.parametrize("key", ["qasm", "circuit_qasm", "circuit"])
    def test_the_single_circuit_shape(self, key):
        """The older shape: one QASM string, under any of three key names."""
        payload = JobPayload.from_dict({key: QASM, "id": "single"})

        assert payload.qasm == QASM
        assert len(payload.circuits) == 1

    def test_a_payload_with_no_circuit_at_all_is_refused(self):
        """What a garbled payload becomes: a clean ValueError the worker reports."""
        with pytest.raises(ValueError, match="No QASM string/circuit provided"):
            JobPayload.from_dict({})

    def test_zero_shots_falls_back_to_the_default(self):
        """`shots: 0` would run nothing, so it is read as "unset"."""
        assert JobPayload.from_dict({"circuit": QASM, "shots": 0}).shots == 1024

    def test_the_acquisition_settings_are_coerced_to_floats(self):
        payload = JobPayload.from_dict(
            {"circuit": QASM, "acq_rotation": "0.25", "acq_threshold": 1}
        )

        assert payload.acq_rotation == 0.25
        assert payload.acq_threshold == 1.0

    def test_absent_acquisition_settings_stay_none(self):
        """None means "the executor decides", which is not the same as zero."""
        payload = JobPayload.from_dict({"circuit": QASM})

        assert payload.acq_rotation is None
        assert payload.acq_threshold is None

    def test_the_measurement_settings_come_through(self):
        payload = JobPayload.from_dict(
            {"circuit": QASM, "meas_level": 1, "meas_return": "avg"}
        )

        assert payload.meas_level == 1
        assert payload.meas_return == "avg"
