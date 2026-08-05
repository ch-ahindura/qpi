"""``calibration.yml``: what it says, and what silence means (RFC 0004 §6.6)."""

import pytest
from qpi_driver.tuners.base.config import (
    DEFAULT_ROUTINE_TIMEOUT_S,
    CalibrationConfig,
    ConfigError,
    RoutineConfig,
)


def test_a_routine_the_file_does_not_mention_is_enabled():
    """The default must be "run it".

    Defaulting to disabled makes an empty file mean "calibrate nothing", and a
    run that does nothing raises nothing, so it reports success — which is the
    worst answer a calibration can give.
    """
    config = CalibrationConfig.from_dict({"target_qubits": ["q0"]})
    assert config.is_enabled("rabi") is True
    assert config.get_routine("rabi").enabled is True


def test_a_routine_can_be_turned_off_explicitly():
    config = CalibrationConfig.from_dict(
        {"routines": {"cz_chevron": {"enabled": False}}}
    )
    assert config.is_enabled("cz_chevron") is False
    assert config.is_enabled("rabi") is True


def test_parameters_are_read_flat_beside_enabled():
    config = CalibrationConfig.from_dict(
        {"routines": {"rabi": {"enabled": True, "amplitudes": [0.1, 0.2]}}}
    )
    routine = config.get_routine("rabi")
    assert routine.get("amplitudes") == [0.1, 0.2]
    assert "enabled" not in routine.params


def test_a_routine_with_no_settings_at_all_is_accepted():
    config = CalibrationConfig.from_dict({"routines": {"allxy": None}})
    assert config.is_enabled("allxy") is True


def test_an_unknown_routine_name_is_an_error():
    """A typo would otherwise enable nothing and leave the real routine at default."""
    config = CalibrationConfig.from_dict(
        {"routines": {"rabi_oscillations": {"enabled": True}}}
    )
    with pytest.raises(ConfigError, match="rabi_oscillations"):
        config.validate_against({"rabi", "t1"})


def test_validation_lists_what_was_available():
    config = CalibrationConfig.from_dict({"routines": {"nope": {}}})
    with pytest.raises(ConfigError, match="Known routines: rabi, t1"):
        config.validate_against({"rabi", "t1"})


def test_targets_and_timeout_are_read():
    config = CalibrationConfig.from_dict(
        {
            "target_qubits": ["q0", "q1"],
            "target_edges": ["q0_q1"],
            "routine_timeout_s": 120,
        }
    )
    assert config.target_qubits == ["q0", "q1"]
    assert config.target_edges == ["q0_q1"]
    assert config.routine_timeout_s == 120


def test_the_timeout_has_a_default():
    assert CalibrationConfig().routine_timeout_s == DEFAULT_ROUTINE_TIMEOUT_S


def test_monitoring_defaults_are_filled_in():
    monitoring = CalibrationConfig.from_dict({}).monitoring
    assert monitoring.rb_depths == [1, 10, 50]
    assert monitoring.shots == 512


def test_a_non_mapping_config_is_rejected():
    with pytest.raises(ConfigError, match="must be a mapping"):
        CalibrationConfig.from_dict(["not", "a", "mapping"])


def test_a_non_mapping_routines_block_is_rejected():
    with pytest.raises(ConfigError, match="'routines' must be a mapping"):
        CalibrationConfig.from_dict({"routines": ["rabi"]})


def test_a_non_mapping_routine_is_rejected():
    with pytest.raises(ConfigError, match="must be a mapping of settings"):
        CalibrationConfig.from_dict({"routines": {"rabi": [1, 2]}})


def test_yaml_is_read_from_a_path(tmp_path):
    path = tmp_path / "cal.yml"
    path.write_text("target_qubits: [q0]\nroutines:\n  rabi:\n    amplitudes: [0.1]\n")
    config = CalibrationConfig.from_yaml(path)
    assert config.target_qubits == ["q0"]
    assert config.get_routine("rabi").get("amplitudes") == [0.1]


def test_an_empty_yaml_file_is_an_error(tmp_path):
    path = tmp_path / "cal.yml"
    path.write_text("")
    with pytest.raises(ConfigError, match="is empty"):
        CalibrationConfig.from_yaml(path)


def test_routine_config_lookup_helpers():
    routine = RoutineConfig(params={"span": 1e6})
    assert routine["span"] == 1e6
    assert routine.get("missing", 7) == 7
    assert "span" in routine
    assert "missing" not in routine


def test_the_shipped_example_config_is_valid():
    """The example is the first thing an operator copies, so it must parse."""
    from pathlib import Path

    from qpi_driver.tuners.routines import routine_names

    example = Path(__file__).parent.parent / "calibration.example.yml"
    config = CalibrationConfig.from_yaml(example)
    config.validate_against(routine_names())
    assert config.target_qubits


class TestTargets:
    """``target_qubits``/``target_edges`` validation."""

    def test_an_edge_over_an_uncalibrated_qubit_is_refused(self):
        """The wrong answer here is a calibrated-looking gate, not an exception.

        A CZ is measured *through* its qubits: the chevron prepares ``|11>`` with a pi
        pulse on each and reads one back. Over a qubit whose frequency and pi pulse are
        whatever the file happened to say, it still fits a curve and still writes an
        amplitude and a duration — and the gate does not work. Startup is the only cheap
        moment to catch that.
        """
        config = CalibrationConfig(target_qubits=["q0", "q1"], target_edges=["q1_q2"])
        with pytest.raises(ConfigError, match="q1_q2 needs q2"):
            config.validate_targets()

    def test_an_edge_whose_qubits_are_both_targeted_is_fine(self):
        CalibrationConfig(
            target_qubits=["q0", "q1", "q2"], target_edges=["q0_q1", "q1_q2"]
        ).validate_targets()

    def test_an_edge_that_is_not_a_pair_is_refused(self):
        config = CalibrationConfig(target_qubits=["q0"], target_edges=["coupler"])
        with pytest.raises(ConfigError, match="'<parent>_<child>' pair"):
            config.validate_targets()

    def test_no_edges_is_not_an_error(self):
        """A single-qubit-only calibration is an ordinary thing to ask for."""
        CalibrationConfig(target_qubits=["q0"]).validate_targets()


class TestCouplerBiasOnHardwareThatMayNotHaveOne:
    """A coupler's bias source, when the routine also has to run without one."""

    def test_a_recorder_cannot_be_used_to_measure_a_parking_current(self):
        """`RecordingBias` applies nothing, so sweeping against it measures nothing.

        The dangerous shape, and the reason this is an error rather than a warning: every
        bias point returns the same qubit frequency, the sweep is flat, and the fit reports
        a crossing with complete confidence. That number then goes to the device. A routine
        that measures the chip has to refuse a source that cannot touch it.
        """
        from qpi_driver.executors.utils.coupler_bias import RecordingBias
        from qpi_driver.tuners.base.routines import RoutineError
        from qpi_driver.tuners.routines import all_routines

        routine = next(r for r in all_routines() if r.name == "coupler_anticrossing")
        with pytest.raises(RoutineError, match="hold a parking current"):
            routine.measure("q0_q1", None, None, None, RecordingBias())
        with pytest.raises(RoutineError, match="hold a parking current"):
            routine.measure("q0_q1", None, None, None, None)

    def test_the_sources_that_do_hold_a_current_say_so(self):
        """The flag is what separates a rack from a notebook, and both are BiasSource."""
        from qpi_driver.executors.utils.coupler_bias import (
            QcmBias,
            RecordingBias,
            SpiRackBias,
        )

        assert RecordingBias.holds_current is False
        assert QcmBias.holds_current is True
        assert SpiRackBias.holds_current is True
