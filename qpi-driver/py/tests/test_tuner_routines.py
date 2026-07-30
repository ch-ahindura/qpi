"""Every routine compiles against a real scheduler (RFC 0004 §7, tier 2).

These need a scheduler, so they skip on a base install and run under
``make test-py-quantify`` and ``make test-py-qblox``. What they prove is that
each routine builds a schedule the backend's compiler accepts — the dummy
cluster returns no real data, so analysis is tier 1's job, not this file's.

The one thing asserted about data here is the opposite of a fit: that a dummy
acquisition makes a routine *fail*, loudly, rather than fit zeros and write them
to the device.
"""

from pathlib import Path

import pytest
from qpi_driver.compat.qblox import IS_QBLOX_SCHEDULER_INSTALLED
from qpi_driver.compat.quantify import IS_QUANTIFY_INSTALLED
from qpi_driver.tuners.base.config import CalibrationConfig, RoutineConfig
from qpi_driver.tuners.routines import ROUTINE_CLASSES, all_routines

FIXTURES = Path(__file__).parent / "fixtures"

# Small sweeps: the point is that each routine compiles, not that it is precise.
SMALL_SWEEPS: dict[str, dict] = {
    "resonator_spectroscopy": {"points": 5, "span": 10e6},
    "resonator_punchout": {"amplitudes": [0.05, 0.1, 0.2], "points": 4, "span": 10e6},
    "qubit_spectroscopy": {"points": 5, "span": 20e6},
    "rabi": {"amplitudes": [0.0, 0.1, 0.2, 0.3, 0.4]},
    "ramsey": {"delays": [4e-9, 1e-6, 2e-6, 4e-6]},
    "t1": {"delays": [0, 10e-6, 20e-6, 40e-6]},
    "t2_echo": {"delays": [0, 10e-6, 20e-6, 40e-6]},
    "drag": {"motzois": [-0.5, 0.0, 0.5]},
    "fine_amplitude": {"repetitions": [1, 3, 5]},
    "rb": {"depths": [1, 2, 4], "circuits_per_depth": 2},
    "flux_spectroscopy": {"flux_offsets": [-0.1, 0.1], "points": 3, "span": 20e6},
    "cz_chevron": {"amplitudes": [0.2, 0.3], "durations": [40e-9, 80e-9]},
    "conditional_phase": {"phases": [0.0, 90.0, 180.0, 270.0]},
    "interleaved_rb": {"depths": [1, 2], "circuits_per_depth": 2},
}

ROUTINE_NAMES = [cls.name for cls in ROUTINE_CLASSES]


@pytest.fixture(scope="module")
def quantify_tuner(tmp_path_factory):
    if not IS_QUANTIFY_INSTALLED:
        pytest.skip("quantify-scheduler is not installed")
    from qpi_driver.compat.quantify import Instrument
    from qpi_driver.tuners.quantify import QuantifyTuner

    Instrument.close_all()
    device = tmp_path_factory.mktemp("quantify") / "quantify.device.yml"
    device.write_bytes((FIXTURES / "quantify.device.yml").read_bytes())

    tuner = QuantifyTuner(
        quantify_hardware_config=FIXTURES / "quantify.hardware.json",
        quantify_device_config=device,
        is_dummy=True,
    )
    yield tuner
    tuner.close()


@pytest.fixture(scope="module")
def qblox_tuner(tmp_path_factory):
    if not IS_QBLOX_SCHEDULER_INSTALLED:
        pytest.skip("qblox-scheduler is not installed")
    from qpi_driver.compat.qblox import Instrument
    from qpi_driver.tuners.qblox import QbloxTuner

    Instrument.close_all()
    directory = tmp_path_factory.mktemp("qblox")
    device = directory / "quantify.device.yml"
    device.write_bytes((FIXTURES / "quantify.device.yml").read_bytes())

    tuner = QbloxTuner(
        quantify_hardware_config=FIXTURES / "quantify.hardware.json",
        quantify_device_config=device,
        is_dummy=True,
        data_dir=directory / "data",
    )
    yield tuner
    tuner.close()


def _build(routine, tuner):
    """Build *routine*'s schedule for the right kind of target."""
    target = "q0" if routine.targets == "qubits" else "q0_q1"
    config = RoutineConfig(params=SMALL_SWEEPS.get(routine.name, {}))
    return routine.build_schedule(target, tuner.device, config, tuner.backend)


@pytest.mark.parametrize("routine_name", ROUTINE_NAMES)
def test_every_routine_compiles_under_quantify(routine_name, quantify_tuner):
    routine = next(r for r in all_routines() if r.name == routine_name)
    schedule = _build(routine, quantify_tuner)

    compiled = quantify_tuner._compiler.compile(schedule)
    assert compiled is not None
    assert len(schedule.operations) > 0


@pytest.mark.parametrize("routine_name", ROUTINE_NAMES)
def test_every_routine_builds_a_schedule_under_qblox(routine_name, qblox_tuner):
    """qblox compiles inside its HardwareAgent, so building is what is asserted here."""
    routine = next(r for r in all_routines() if r.name == routine_name)
    schedule = _build(routine, qblox_tuner)

    assert schedule is not None
    assert len(schedule.operations) > 0


def test_a_dummy_acquisition_fails_the_routine_rather_than_fitting_zeros(
    quantify_tuner,
):
    """The dummy cluster returns no real data, and that must be an error.

    This is the failure mode the whole design turns on: a fit that returns zeros
    on bad data gets written to the device as though it were a measurement.
    """
    config = CalibrationConfig(
        target_qubits=["q0"],
        target_edges=[],
        routines={
            name: RoutineConfig(
                enabled=name in {"rabi", "t1"}, params=SMALL_SWEEPS.get(name, {})
            )
            for name in ROUTINE_NAMES
        },
    )
    report = quantify_tuner.calibrate(config)

    assert report.status == "failed"
    assert report.routine_results == []
    assert len(report.errors) == 2


def test_a_failed_calibration_does_not_touch_the_device_config(quantify_tuner):
    """Overwriting a good config with half-applied parameters is how a QPU is lost."""
    before = quantify_tuner._device_config_path.read_text()

    config = CalibrationConfig(
        target_qubits=["q0"],
        routines={
            name: RoutineConfig(
                enabled=name == "rabi", params=SMALL_SWEEPS.get(name, {})
            )
            for name in ROUTINE_NAMES
        },
    )
    report = quantify_tuner.calibrate(config)

    assert report.status == "failed"
    assert quantify_tuner._device_config_path.read_text() == before


def test_a_write_back_round_trips_through_the_real_loader(quantify_tuner):
    """What a tuner writes, the process driver beside it has to be able to read."""
    from qpi_driver.compat.quantify import Instrument
    from qpi_driver.executors.quantify.config import load_quantum_device
    from qpi_driver.tuners.utils.persistence import save_device_config

    path = quantify_tuner._device_config_path
    quantify_tuner.device.get_element("q0").rxy.amp180(0.1234)
    save_device_config(quantify_tuner.device, path)

    assert "!!python" not in path.read_text()

    Instrument.close_all()
    reloaded = load_quantum_device(name="reloaded", config=path)
    assert reloaded.get_element("q0").rxy.amp180() == 0.1234
    assert reloaded.edges() == ["q0_q1", "q1_q2"]
