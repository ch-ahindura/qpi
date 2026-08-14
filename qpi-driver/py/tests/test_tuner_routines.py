"""Every routine compiles against a real scheduler (RFC 0004 §7, tier 2).

These need a scheduler, so they skip on a base install and run under
``make test-py-driver EXECUTOR=quantify`` and ``make test-py-driver EXECUTOR=qblox``. What they prove is that
each routine builds a schedule the backend's compiler accepts — the dummy
cluster returns no real data, so analysis is tier 1's job, not this file's.

The one thing asserted about data here is the opposite of a fit: that a dummy
acquisition makes a routine *fail*, loudly, rather than fit zeros and write them
to the device.
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import xarray as xr
import yaml
import time

import pytest
from qpi_driver.compat.qblox import IS_QBLOX_SCHEDULER_INSTALLED
from qpi_driver.compat.quantify import IS_QUANTIFY_INSTALLED
from qpi_driver.tuners.base.backend import SchedulerBackend
from qpi_driver.tuners.base.config import CalibrationConfig, RoutineConfig
from qpi_driver.tuners.base.routines import MAX_SWEEP_POINTS, RoutineError
from qpi_driver.tuners.routines import ROUTINE_CLASSES, all_routines

FIXTURES = Path(__file__).parent / "fixtures"

#: Measured on a QRM-RF: a `resonator_punchout` of 846 acquisitions compiled to 12700
#: Q1ASM instructions, against the 12288 the module accepts. A frequency sweep is three
#: operations per point — `Reset`, `SetClockFrequency`, `Measure` — which is why the rule of
#: thumb of one group per point is about 30% optimistic.
INSTRUCTIONS_PER_ACQUISITION = 15.01
Q1ASM_CEILING = 12288

# Small sweeps: the point is that each routine compiles, not that it is precise.
SMALL_SWEEPS: dict[str, dict] = {
    "resonator_spectroscopy": {"points": 5, "span": 10e6},
    "resonator_punchout": {"amplitudes": [0.05, 0.1, 0.2], "points": 4, "span": 10e6},
    "qubit_spectroscopy": {"points": 5, "span": 20e6},
    "rabi": {"amplitudes": [0.0, 0.1, 0.2, 0.3, 0.4]},
    "ramsey": {"delays": [4e-9, 1e-6, 2e-6, 4e-6]},
    "t1": {"delays": [0, 10e-6, 20e-6, 40e-6]},
    "t2_echo": {"delays": [0, 10e-6, 20e-6, 40e-6]},
    # Seconds, not a dimensionless amplitude: the DRAG parameter scales a time
    # derivative of the envelope, so a 20 ns gate wants values of order 1e-11.
    # At the dimensionless scale this used to use, the derivative term dwarfs
    # the carrier and the waveform exceeds full scale.
    "drag": {"motzois": [-1e-10, 0.0, 1e-10]},
    "fine_amplitude": {"repetitions": [1, 3, 5]},
    "rb": {"depths": [1, 2, 4], "circuits_per_depth": 2},
    "flux_spectroscopy": {"flux_offsets": [-0.1, 0.1], "points": 3, "span": 20e6},
    "cz_chevron": {"amplitudes": [0.2, 0.3], "durations": [40e-9, 80e-9]},
    "conditional_phase": {"phases": [0.0, 90.0, 180.0, 270.0]},
    "interleaved_rb": {"depths": [1, 2], "circuits_per_depth": 2},
}

#: Every routine that builds a schedule — which is every one but `coupler_anticrossing`.
#: That one sweeps a DC bias, sets instrument state between acquisitions and runs its
#: own loop, so there is no single schedule for this file to compile, and its
#: `build_schedule` raises. It is covered in the loop suite, where a simulated rack can
#: actually hold a current.
#:
#: Named rather than derived from `measures_itself`, which no longer implies it:
#: `qubit_spectroscopy` also runs its own loop, and each pass of it is a schedule this
#: file should still be compiling.
ROUTINE_NAMES = [
    cls.name for cls in ROUTINE_CLASSES if cls.name != "coupler_anticrossing"
]


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


def routine(name):
    return next(r for r in all_routines() if r.name == name)


def _target_for(routine, device):
    """A target of the right kind that this routine actually applies to.

    ``q0_q1`` is a `CompositeSquareEdge` and ``q1_q2`` a `FluxTunableCoupler`, and a
    routine may describe only one of them — `cz_spectroscopy` looks for a CZ *drive
    frequency*, which a DC-flux edge does not have. Picking the first applicable
    target is what lets both kinds be covered without the test asserting that every
    routine fits every edge, which is not true and was never meant to be.
    """
    if routine.targets == "qubits":
        return "q0"
    for edge in ("q0_q1", "q1_q2"):
        if routine.applies_to(device, edge):
            return edge
    return None


def _build(routine, tuner):
    """Build *routine*'s schedule for the right kind of target."""
    target = _target_for(routine, tuner.device)
    assert target is not None, f"{routine.name} applies to no edge on the fixture"
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
    """And compiles it, which is the half that was missing.

    qblox owns compilation inside its `HardwareAgent` rather than in a separate
    compiler, so this used to assert only that a schedule came back — meaning a
    routine whose schedule built but would not compile passed here while the
    equivalent quantify test caught it. The agent exposes `compile`, so both
    backends are now held to the same standard.
    """
    routine = next(r for r in all_routines() if r.name == routine_name)
    schedule = _build(routine, qblox_tuner)

    assert schedule is not None
    assert len(schedule.operations) > 0

    compiled = qblox_tuner._agent.compile(schedule)
    assert compiled is not None


CHECKABLE = [r.name for r in all_routines() if r.has_check]


@pytest.mark.parametrize("routine_name", CHECKABLE)
def test_every_check_schedule_compiles_under_both_schedulers(
    routine_name, quantify_tuner, qblox_tuner
):
    """A check that cannot be built is silent, not failing — so it needs a test here.

    `diagnose` deliberately treats an unevaluable check as *unknown* rather than as
    drift, because a broken check must not trigger a recalibration. The cost of that
    choice is that a check which raises every time reports nothing, forever, and no
    test fails. One did: the first version of `resonator_spectroscopy`'s read its
    tolerance from ``measure.readout_linewidth``, a path no transmon element has, so
    `read_path` raised `ParameterError` on every call and the check was dead on
    arrival.

    Building the check schedule against a real device is what catches that, because
    that is where a check reads the parameters it is judging.
    """
    routine = next(r for r in all_routines() if r.name == routine_name)
    config = RoutineConfig(params=SMALL_SWEEPS.get(routine.name, {}))

    for tuner, compile_with in (
        (quantify_tuner, lambda s: quantify_tuner._compiler.compile(s)),
        (qblox_tuner, lambda s: qblox_tuner._agent.compile(s)),
    ):
        target = _target_for(routine, tuner.device)
        assert target is not None
        schedule = routine.build_check_schedule(
            target, tuner.device, config, tuner.backend
        )
        assert schedule is not None, (
            f"{routine_name} reports has_check but built no check schedule"
        )
        assert compile_with(schedule) is not None


def test_a_dummy_acquisition_fails_the_routine_rather_than_fitting_zeros(
    quantify_tuner,
):
    """The dummy cluster returns no real data, and that must be an error.

    This is the failure mode the whole design turns on: a fit that returns zeros
    on bad data gets written to the device as though it were a measurement.

    ``t1`` is enabled alongside `rabi` to pin the second half of it. It used to fail
    too, for two errors; now that it declares reading the `rxy.amp180` its gate needs
    (RFC 0007 §11.1) it is *skipped*, so the report says once what went wrong and names
    it as the cause. One error and one skip is the stronger claim, and it is the
    difference between this report and the eight-way one the B chip produced.
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
    assert len(report.errors) == 1, report.errors
    assert report.errors[0].startswith("rabi[q0]")
    assert any(
        note.startswith("t1[q0]: skipped") and "rabi" in note for note in report.notes
    ), report.notes


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


def test_a_device_config_changed_under_a_tuner_is_applied_to_the_live_device(
    quantify_tuner,
):
    """Another tuner, or a hand edit, may have moved the chip since startup.

    Without this the tuner calibrates from what it read at startup and then writes
    that back over whatever changed.
    """
    import yaml
    from qpi_driver.tuners.base.device import read_path

    path = quantify_tuner._device_config_path
    config = yaml.safe_load(path.read_text())
    config["q0"]["clock_freqs"]["f01"] = 5.111e9
    path.write_text(yaml.safe_dump(config))

    quantify_tuner._refresh_device_config()

    element = quantify_tuner.device.get_element("q0")
    assert read_path(element, "clock_freqs.f01") == 5.111e9


def test_an_unreadable_device_config_leaves_a_tuner_calibrating_from_memory(
    quantify_tuner,
):
    from qpi_driver.tuners.base.device import read_path

    path = quantify_tuner._device_config_path
    before = read_path(quantify_tuner.device.get_element("q0"), "clock_freqs.f01")
    path.write_text("q0: {clock_freqs: {f01: [unclosed\n")

    quantify_tuner._refresh_device_config()

    element = quantify_tuner.device.get_element("q0")
    assert read_path(element, "clock_freqs.f01") == before


def test_every_dag_entry_point_refreshes_before_it_walks(quantify_tuner, monkeypatch):
    """Raising from the refresh proves each entry point calls it, and calls it first.

    All three are DAG walks over different subsets, so a refresh wired into only
    one of them would leave a partial recalibration reading a stale device.
    """
    from qpi_driver.tuners.base.config import CalibrationConfig

    class _Fired(Exception):
        pass

    def explode(self):
        raise _Fired

    monkeypatch.setattr(type(quantify_tuner), "_refresh_device_config", explode)
    config = CalibrationConfig(target_qubits=["q0"])

    for call in (
        lambda: quantify_tuner.calibrate(config),
        lambda: quantify_tuner.recalibrate(["q0"], config),
        lambda: quantify_tuner.check_fidelity(config),
    ):
        with pytest.raises(_Fired):
            call()


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


@pytest.mark.parametrize("tuner_name", ["quantify", "qblox"])
def test_closing_a_live_tuner_does_not_raise(tuner_name, tmp_path):
    """The worker calls `close()` on shutdown, so it has to survive a live coordinator.

    Its own tuner rather than the module fixture, deliberately. That fixture is
    torn down *after* a test that has already called `Instrument.close_all()`,
    so its `close()` runs against a coordinator that is already gone — the one
    arrangement in which a broken shutdown looks fine.
    """
    if not {
        "quantify": IS_QUANTIFY_INSTALLED,
        "qblox": IS_QBLOX_SCHEDULER_INSTALLED,
    }[tuner_name]:
        pytest.skip(f"the {tuner_name} scheduler is not installed")

    from qpi_driver.compat.quantify import Instrument
    from qpi_driver.tuners import resolve_tuner

    device = tmp_path / "quantify.device.yml"
    device.write_bytes((FIXTURES / "quantify.device.yml").read_bytes())

    Instrument.close_all()
    tuner = resolve_tuner(
        tuner_name,
        quantify_hardware_config=FIXTURES / "quantify.hardware.json",
        quantify_device_config=device,
        is_dummy=True,
        data_dir=tmp_path / "data",
    )
    tuner.close()
    tuner.close()  # and again: a driver failing mid-shutdown closes twice


@pytest.mark.parametrize("tuner_name", ["quantify", "qblox"])
def test_the_two_qubit_routines_write_parameters_the_edge_actually_has(tuner_name):
    """`apply` against a real edge, not a fake shaped like the routine.

    Both two-qubit routines wrote names no device has — `cz.amp` and
    `cz.duration`, which raised, and `cz.phase_correction`, which was skipped by
    a `hasattr` guard that was never true. So a chevron that had measured the
    gate correctly failed at its last step, and the conditional phase applied
    nothing at all.

    Neither tier-2 nor tier-3 could catch it: tier 2 builds and compiles a
    schedule without ever calling `apply`, and tier 3's fake device was given
    the names the routine used. A fake shaped to the code cannot contradict it,
    so this asserts against the real `QuantumDevice`.
    """
    # Its own device rather than the module-scoped tuner's: an earlier test in
    # this file calls `Instrument.close_all()`, which invalidates the shared
    # one's edges. Loading here keeps the test independent of its neighbours.
    if tuner_name == "qblox":
        if not IS_QBLOX_SCHEDULER_INSTALLED:
            pytest.skip("qblox-scheduler is not installed")
        from qpi_driver.compat.qblox import Instrument
        from qpi_driver.executors.qblox.config import load_quantum_device
    else:
        if not IS_QUANTIFY_INSTALLED:
            pytest.skip("quantify-scheduler is not installed")
        from qpi_driver.compat.quantify import Instrument
        from qpi_driver.executors.quantify.config import load_quantum_device

    Instrument.close_all()
    device = load_quantum_device(
        name=f"apply_{tuner_name}", config=FIXTURES / "quantify.device.yml"
    )
    edge = device.get_edge("q0_q1")

    routine(  # noqa: B018 - the call is the assertion
        "cz_chevron"
    ).apply(device, "q0_q1", {"cz_amplitude": 0.377, "cz_duration": 110e-9})

    from qpi_driver.tuners.base.device import phase_correction_names, read_path

    assert read_path(edge, "cz.square_amp") == pytest.approx(0.377)
    assert read_path(edge, "cz.square_duration") == pytest.approx(110e-9)

    names = phase_correction_names(edge)
    assert names is not None, "a CZ edge must expose its two phase corrections"

    routine("conditional_phase").apply(
        device,
        "q0_q1",
        {"parent_phase_correction": 12.0, "child_phase_correction": -34.0},
    )
    parent_name, child_name = names
    assert read_path(edge, f"cz.{parent_name}") == pytest.approx(12.0)
    assert read_path(edge, f"cz.{child_name}") == pytest.approx(-34.0)


@pytest.fixture
def wired_device():
    """The fixture chip as a real device, with its wiring attached.

    Its own rather than the module-scoped tuner's, for the reason the neighbours
    below give: an earlier test in this file calls `Instrument.close_all()`, and
    `has_flux_port` reads the connectivity back off the device.
    """
    if not IS_QUANTIFY_INSTALLED:
        pytest.skip("quantify-scheduler is not installed")
    from qpi_driver.compat.quantify import Instrument
    from qpi_driver.executors.quantify.config import (
        load_quantify_hardware_config,
        load_quantum_device,
    )

    Instrument.close_all()
    device = load_quantum_device(name="wired", config=FIXTURES / "quantify.device.yml")
    device.hardware_config(
        load_quantify_hardware_config(FIXTURES / "quantify.hardware.json")
    )
    return device


class TestTheFluxRoutinesFollowTheWiring:
    """Which routines apply is a property of where the flux line goes.

    The fixture carries both architectures on one chip: ``q0``/``q1`` have their own
    ``:fl`` and join through the DC-flux ``q0_q1``, while ``q2`` has none and joins
    ``q1`` through the parametric coupler ``q1_q2``. A routine that plays flux on a
    port the connectivity does not carry fails deep in the compiler with a `KeyError`
    naming neither the routine nor the reason, so it has to be asked beforehand.
    """

    def test_a_qubit_flux_line_is_seen_and_a_missing_one_is_not(self, wired_device):
        from qpi_driver.tuners.base.device import has_flux_port

        assert has_flux_port(wired_device, "q0")
        assert not has_flux_port(wired_device, "q2")
        assert has_flux_port(wired_device, "q1_q2")

    def test_flux_spectroscopy_declines_a_qubit_with_no_flux_line(self, wired_device):
        assert routine("flux_spectroscopy").applies_to(wired_device, "q0")
        assert not routine("flux_spectroscopy").applies_to(wired_device, "q2")

    def test_cz_chevron_declines_a_parametric_edge(self, wired_device):
        """Its counterpart there is `cz_parametrization`, which sweeps frequency."""
        assert routine("cz_chevron").applies_to(wired_device, "q0_q1")
        assert not routine("cz_chevron").applies_to(wired_device, "q1_q2")

    def test_the_two_cz_calibrations_never_both_apply(self, wired_device):
        """One edge, one gate: whichever of the pair describes it, not both."""
        for edge in ("q0_q1", "q1_q2"):
            chevron = routine("cz_chevron").applies_to(wired_device, edge)
            parametric = routine("cz_parametrization").applies_to(wired_device, edge)
            assert chevron != parametric, edge

    def test_conditional_phase_still_applies_where_cz_chevron_declines(
        self, wired_device
    ):
        """It depends on `cz_chevron`, and `depends_on` only orders the walk.

        A parametric chip calibrates its CZ through `cz_parametrization` instead, and
        the phase correction is measured the same way either way.
        """
        assert routine("conditional_phase").applies_to(wired_device, "q1_q2")


@pytest.mark.parametrize("tuner_name", ["quantify", "qblox"])
def test_spectroscopy_still_applies_to_an_element_with_no_spec_submodule(tuner_name):
    """`spec.amplitude` is opt-in, so a plain transmon must still calibrate.

    The fixture opts every qubit into `CalibratedTransmon`, which means nothing else
    covers the configs that do not — and those are every config written before this
    element existed. A routine that raised here would make the extension a breaking
    change to the device file format rather than an addition to it.
    """
    if tuner_name == "qblox":
        if not IS_QBLOX_SCHEDULER_INSTALLED:
            pytest.skip("qblox-scheduler is not installed")
        from qpi_driver.compat.qblox import BasicTransmonElement, Instrument
    else:
        if not IS_QUANTIFY_INSTALLED:
            pytest.skip("quantify-scheduler is not installed")
        from qpi_driver.compat.quantify import BasicTransmonElement, Instrument

    from qpi_driver.tuners.base.device import (
        read_path,
        spectroscopy_amplitude_path,
    )

    Instrument.close_all()
    plain = BasicTransmonElement("q0")
    assert spectroscopy_amplitude_path(plain) is None

    class _OneElement:
        def get_element(self, _name):
            return plain

    routine("qubit_spectroscopy").apply(
        _OneElement(), "q0", {"clock_freq_01": 5.01e9, "drive_amplitude": 0.02}
    )
    assert read_path(plain, "clock_freqs.f01") == pytest.approx(5.01e9)


class TestALineHasToBeAboveTheNoise:
    """`require_resolved_line` judges the fit, not only the sweep that produced it.

    Both spectroscopy roots write a frequency straight to the device — f01, and the
    readout frequency every other node then reads at — so a Lorentzian centre drawn
    through noise does not merely produce a bad report, it overwrites the last good
    value and breaks the nodes after it. Twice on hardware, costing a run each time.
    """

    #: Reach — the fitted curve's travel over the scatter left around it. The refused
    #: values are the worst of 1800 fits of *pure noise* that had already cleared the
    #: linewidth test; their 99th percentile was 3.4 to 3.6 and their maximum 4.2. The
    #: accepted ones are real lines: 93 on the August 2026 chip's resonator, and 104 to
    #: 139 on the simulated qubit at spans from 4 MHz to 600 MHz.
    MEASURED_REACH = ((4.2, False), (3.5, False), (93.0, True), (127.0, True))

    @pytest.mark.parametrize("reach,accepted", MEASURED_REACH)
    def test_it_accepts_only_a_curve_that_went_somewhere(self, reach, accepted):
        from qpi_driver.tuners.base.routines import require_resolved_line

        # 200 kHz line on a 133 kHz grid: wide enough that only the reach decides.
        fitted = {"linewidth": 200e3, "reach": reach}
        frequencies = [4.7e9 + 133e3 * i for i in range(3)]
        if accepted:
            require_resolved_line(fitted, frequencies)  # noqa: B018 - no raise is it
        else:
            with pytest.raises(RoutineError, match="the scatter left around it"):
                require_resolved_line(fitted, frequencies)

    def test_signal_to_noise_is_no_longer_what_decides(self):
        """It cannot be. `snr` divides a fitted parameter by the residual.

        Over the same 1800 noise fits its 99th percentile was 570 to 2700 and its
        maximum 7048, and 16% to 55% of them cleared the 3.0 floor this used to apply —
        so an optimiser handed noise could always talk its way past. It still ranks one
        drive power against another, which is a comparison and not a threshold.
        """
        from qpi_driver.tuners.base.routines import require_resolved_line

        frequencies = [4.7e9 + 133e3 * i for i in range(3)]
        with pytest.raises(RoutineError, match="the scatter left around it"):
            require_resolved_line(
                {"linewidth": 200e3, "snr": 7048.0, "reach": 2.0}, frequencies
            )

    def test_a_line_narrower_than_the_sweep_is_still_refused(self):
        """The original check, and the opposite shape: sharp fit, coarse sweep.

        Reach cannot catch this one — a fit that latched onto a single bin has a large
        span and tiny residuals, so it scores well. The two tests are complementary.
        """
        from qpi_driver.tuners.base.routines import require_resolved_line

        with pytest.raises(RoutineError, match="narrower than"):
            require_resolved_line(
                {"linewidth": 2379.0, "reach": 500.0},
                [6.827e9 + 400e3 * i for i in range(3)],
            )

    def test_a_fit_that_reports_no_reach_is_judged_on_width_alone(self):
        """Every fit forwards it now, but the guard must not start refusing on absence."""
        from qpi_driver.tuners.base.routines import require_resolved_line

        require_resolved_line(  # noqa: B018 - no raise is the assertion
            {"linewidth": 200e3}, [4.7e9 + 133e3 * i for i in range(3)]
        )


class TestExcitingTheQubitHasToMoveItsResonator:
    """`resonator_spectroscopy_excited` is where a drive that reaches nothing shows up.

    It writes no parameter, so a dead X gate left it reporting a dispersive shift of a
    few hundred Hz and the calibration walking on. Everything after it — discrimination,
    allxy, drag, rb — then measured an idle qubit and fitted its noise, which read as
    several unrelated failures for six runs. The floor is on the shift *as a fraction of
    the linewidth* because that ratio is what decides whether two states are resolvable
    at all; neither number alone says anything.
    """

    LINEWIDTH = 370e3
    GROUND = 6.827e9

    #: Shift as a fraction of the linewidth. The first two are what the chip returned
    #: with its f01 off by an anharmonicity — 1954 Hz and 591 Hz against ~375 kHz. The
    #: third is the simulated chip, which the whole DAG calibrates through.
    MEASURED = ((0.0052, False), (0.0016, False), (0.62, True))

    @pytest.mark.parametrize("fraction,accepted", MEASURED)
    def test_only_a_shift_readout_could_resolve_is_reported(self, fraction, accepted):
        import numpy as np

        node = routine("resonator_spectroscopy_excited")
        # What `build_schedule` records: the sweep, and the ground-state resonance the
        # shift is measured against. Set directly because this test supplies the
        # acquisition rather than running one, and `analyse` reads no device at all now.
        node._frequencies = [self.GROUND - 2e6 + 40e3 * i for i in range(101)]
        node._ground = self.GROUND
        excited = self.GROUND - 2.0 * fraction * self.LINEWIDTH
        detuning = (np.asarray(node._frequencies) - excited) / (self.LINEWIDTH / 2)
        signal = 0.027 - 0.02 / (1.0 + detuning**2)
        signal += np.random.default_rng(0).normal(0.0, 2e-5, signal.size)

        if accepted:
            found = node.analyse(signal, "q0", None, RoutineConfig(params={}))
            assert found["dispersive_shift"] < 0
        else:
            with pytest.raises(RoutineError, match="not exciting this qubit"):
                node.analyse(signal, "q0", None, RoutineConfig(params={}))


@pytest.fixture
def own_quantify_tuner(tmp_path):
    """A tuner of this test's own, for the reason its neighbours give.

    An earlier test in this file calls `Instrument.close_all()`, which invalidates the
    module-scoped tuner's device — and this test walks every routine, so it needs one
    that is readable whatever ran before it.
    """
    if not IS_QUANTIFY_INSTALLED:
        pytest.skip("quantify-scheduler is not installed")
    from qpi_driver.compat.quantify import Instrument
    from qpi_driver.tuners.quantify import QuantifyTuner

    Instrument.close_all()
    device = tmp_path / "quantify.device.yml"
    device.write_bytes((FIXTURES / "quantify.device.yml").read_bytes())
    tuner = QuantifyTuner(
        quantify_hardware_config=FIXTURES / "quantify.hardware.json",
        quantify_device_config=device,
        is_dummy=True,
    )
    yield tuner
    tuner.close()


def test_a_routine_declares_every_parameter_it_reads(own_quantify_tuner, monkeypatch):
    """`reads` is hand-written, so something has to check it against the code.

    RFC 0007 §11 has the DAG decline a node whose input was never produced, and that
    decision is made from `reads` *before* the node runs — so a declaration missing a
    path lets exactly the node this is meant to protect run on an uncalibrated chip.
    Declared rather than derived at run time because the set is static and because the
    two `measure` implementors have no schedule to inspect first; this test is what
    keeps the declaration honest, by instrumenting the single function every device read
    goes through and comparing what was actually asked for.

    A declaration may be *wider* than what one build reads — `qubit_spectroscopy` reads
    `spec.amplitude` only on an element that has one — so this asserts coverage, not
    equality.
    """
    from qpi_driver.tuners.base import device as device_mod
    from qpi_driver.tuners.routines import ef, readout, single_qubit, spectroscopy
    from qpi_driver.tuners.routines import two_qubit

    recorded: set[str] = set()
    original = device_mod.read_path

    def recording(component, dotted):
        recorded.add(dotted)
        return original(component, dotted)

    # Every module that imported `read_path` into its own namespace, plus the module
    # that defines it — patching only the latter would miss every routine.
    for module in (device_mod, ef, readout, single_qubit, spectroscopy, two_qubit):
        if hasattr(module, "read_path"):
            monkeypatch.setattr(module, "read_path", recording)

    undeclared: dict[str, set[str]] = {}
    for name in ROUTINE_NAMES:
        node = routine(name)
        recorded.clear()
        _build(node, own_quantify_tuner)
        missing = recorded - set(node.reads)
        if missing:
            undeclared[name] = missing

    assert not undeclared, "\n".join(
        f"{name} reads {sorted(paths)} without declaring it"
        for name, paths in sorted(undeclared.items())
    )


def test_a_rabi_sweep_can_reach_full_scale_but_does_not_start_there(own_quantify_tuner):
    """Two bounds pull against each other, so the default measures and escalation reaches.

    Reach: a sweep stopping at 0.5 cannot find a pi pulse above it, and `require_in_range`
    will not say so — it checks the fitted value lies *inside* the swept range, which fires
    the same way for a value that is too small. The August 2026 chip's own working
    calibration used `amp180 = 0.5683` against exactly that sweep, so every Rabi run came
    back flat and the amplitude it wrote left X rotating five degrees.

    Accuracy: the fit is a cosine and a strongly driven transmon stops being one. Starting
    at full scale put amp180 6.9% out on the simulated chip where half scale lands within
    1%, and took `rabi_12` off its sqrt(2) ladder entirely.

    So the default is half of full scale, `fit_rabi` raises `OutOfRange` when the pi pulse
    is above the sweep, and `escalating` reaches further — up to full scale, because a
    waveform past that clips. The element does not bound this at all: quantify validates
    `rxy.amp180` in [-10, 10], a sanity range rather than a drive bound.
    """
    from qpi_driver.tuners.base.limits import FULL_SCALE, full_scale

    element = own_quantify_tuner.device.get_element("q0")
    assert full_scale(element, "rxy.amp180") == pytest.approx(FULL_SCALE)

    node = routine("rabi")
    node.build_schedule(
        "q0",
        own_quantify_tuner.device,
        RoutineConfig(params={}),
        own_quantify_tuner.backend,
    )
    assert max(node._amplitudes) == pytest.approx(0.5 * FULL_SCALE), (
        "the default should measure where the cosine model holds"
    )

    # And a pi pulse above that is a request for more amplitude, not a failed fit.
    from qpi_driver.tuners.fitting import fit_rabi
    from qpi_driver.tuners.fitting.core import OutOfRange

    amplitudes = np.linspace(0.0, 0.5, 41)
    # A cosine whose half period is 0.9 — a pi pulse well past the top of this sweep.
    signal = 0.5 - 0.5 * np.cos(2 * np.pi * amplitudes / 1.8)
    with pytest.raises(OutOfRange, match="past the top of the range") as raised:
        fit_rabi(amplitudes, signal)
    assert raised.value.axis == "amplitudes"
    assert raised.value.direction == "wider"


class TestSweepsSizedFromTheMeasuredLinewidth:
    """RFC 0007 §5: a span derived from what was measured, not from a constant.

    The constants were each right for one chip. A 2 MHz operating-point span is 0.6 of the
    simulated chip's measured linewidth and 5.4 of a 370 kHz resonator's, and on the second
    it put the outer setpoints off resonance altogether.
    """

    def _with_linewidth(self, tuner, linewidth):
        from qpi_driver.tuners.base.device import write_path

        write_path(tuner.device.get_element("q0"), "resonator.linewidth", linewidth)

    def _span_of(self, node):
        """The frequency span the node built, however it stored it.

        The operating points keep `(frequency, amplitude)` pairs, since they sweep both;
        the spectroscopy sweeps keep frequencies alone.
        """
        grid = getattr(node, "_frequencies", None)
        if grid is None:
            grid = [frequency for frequency, _amplitude in node._settings]
        return max(grid) - min(grid)

    @pytest.mark.parametrize("linewidth", (370e3, 3.31e6))
    def test_the_operating_point_span_tracks_the_linewidth(
        self, own_quantify_tuner, linewidth
    ):
        from qpi_driver.tuners.routines.readout import SPAN_IN_LINEWIDTHS

        self._with_linewidth(own_quantify_tuner, linewidth)
        node = routine("readout_operating_point")
        node.build_schedule(
            "q0",
            own_quantify_tuner.device,
            RoutineConfig(params={}),
            own_quantify_tuner.backend,
        )
        assert self._span_of(node) == pytest.approx(
            SPAN_IN_LINEWIDTHS * linewidth, rel=1e-6
        )

    @pytest.mark.parametrize("linewidth", (370e3, 3.31e6))
    def test_the_excited_sweep_span_tracks_the_linewidth(
        self, own_quantify_tuner, linewidth
    ):
        from qpi_driver.tuners.routines.spectroscopy import EXCITED_SPAN_IN_LINEWIDTHS

        self._with_linewidth(own_quantify_tuner, linewidth)
        node = routine("resonator_spectroscopy_excited")
        node.build_schedule(
            "q0",
            own_quantify_tuner.device,
            RoutineConfig(params={}),
            own_quantify_tuner.backend,
        )
        assert self._span_of(node) == pytest.approx(
            EXCITED_SPAN_IN_LINEWIDTHS * linewidth, rel=1e-6
        )

    def test_an_unmeasured_resonator_falls_back_rather_than_sweeping_nothing(
        self, own_quantify_tuner
    ):
        """Zero means "not measured", and a zero-wide span would sweep one point."""
        self._with_linewidth(own_quantify_tuner, 0.0)
        node = routine("readout_operating_point")
        node.build_schedule(
            "q0",
            own_quantify_tuner.device,
            RoutineConfig(params={}),
            own_quantify_tuner.backend,
        )
        assert self._span_of(node) > 0.0


class TestAnAnharmonicityHasToBeATransmons:
    """`f12_spectroscopy` is the only node that knows both frequencies, so it is the only
    one that can say whether the line it found is the 1-2 transition or some other line.

    The failure: a device file carried `f12 = 4.8e9` against an f01 near 4.7 GHz, so the
    implied anharmonicity was *positive* on four of five qubits. Nothing objected.
    """

    def test_a_positive_prior_is_refused(self):
        node = routine("f12_spectroscopy")

        class _Device:
            @staticmethod
            def get_element(_name):
                class _Element:
                    class clock_freqs:
                        f01 = 4.7e9

                return _Element

        with pytest.raises(RoutineError, match="not an anharmonicity a transmon has"):
            node.build_schedule(
                "q0",
                _Device,
                RoutineConfig(params={"anharmonicity_prior": 100e6}),
                None,
            )

    @pytest.mark.parametrize("anharmonicity", (100e6, -20e6, -900e6))
    def test_a_fitted_f12_on_the_wrong_line_is_refused(self, anharmonicity):
        node = routine("f12_spectroscopy")
        with pytest.raises(RoutineError, match="not a transmon's anharmonicity"):
            node._require_transmon_anharmonicity(anharmonicity, "q0")

    def test_a_real_anharmonicity_passes(self):
        node = routine("f12_spectroscopy")
        assert node._require_transmon_anharmonicity(-302.5e6, "q0") == pytest.approx(
            -302.5e6
        )


#: Gate constructors a routine plays on its target, as named on the backend.
#:
#: Each resolves its frequency and amplitude off the device element when the schedule is
#: *compiled*, not when it is built — so a routine that plays one depends on
#: `clock_freqs.f01`, and on `rxy.amp180` unless it passes an amplitude itself.
GATE_NAMES = ("Rxy", "X", "Y", "X90", "Y90")

#: Keyword arguments that mean a routine supplied its own drive amplitude, so the
#: element's calibrated `rxy.amp180` is not what the pulse uses.
OWN_AMPLITUDE_KWARGS = ("amp180", "amp", "amplitude")

#: Methods a routine may compose gates in. `measure` is here because the two routines
#: that implement it have no single schedule to inspect.
SCHEDULE_METHODS = (
    "build_schedule",
    "build_check_schedule",
    "measure",
    "_probe_schedule",
    "_search",
    "_confirm",
    "_sequence_schedule",
)


def _gates_played(node) -> tuple[set[str], set[str]]:
    """``(gate names, keyword arguments passed to them)`` across *node*'s schedule code.

    Read off the source rather than off a built schedule, and the reason is the whole
    point of this test: the dependency is not observable at build time. A backend gate
    carries no frequency — quantify resolves that from the element during compilation —
    and compiling cannot derive it either, because `generate_device_config` serialises
    *every* parameter, so an instrumented element reports all of them and distinguishes
    nothing. What is left is the structural fact: this routine plays a gate.
    """
    import ast
    import inspect
    import textwrap

    source = ""
    for name in SCHEDULE_METHODS:
        method = getattr(type(node), name, None)
        if method is None:
            continue
        try:
            source += textwrap.dedent(inspect.getsource(method))
        except (OSError, TypeError):
            continue
    if not source:
        return set(), set()

    gates: set[str] = set()
    keywords: set[str] = set()
    for element in ast.walk(ast.parse(source)):
        if not isinstance(element, ast.Call):
            continue
        if not isinstance(element.func, ast.Attribute):
            continue
        if element.func.attr in GATE_NAMES:
            gates.add(element.func.attr)
            keywords |= {word.arg for word in element.keywords if word.arg}
    return gates, keywords


def test_a_routine_playing_a_gate_declares_the_gate_parameters():
    """RFC 0007 §11.1, the gap that let one fault become eight on the B chip.

    `test_a_routine_declares_every_parameter_it_reads` instruments `read_path`, which
    cannot see a dependency that never passes through it — and a gate's frequency and
    amplitude never do. So `rabi`, `t1`, `t2_echo`, `rb` and sixteen others declared
    nothing, `qubit_spectroscopy` failed on q5, and seven nodes behind it ran on an
    unexcited qubit and fitted their own noise into seven different-looking errors.

    This asserts the rule instead of deriving the values: play a gate, declare
    `clock_freqs.f01`; play one without supplying an amplitude, declare `rxy.amp180`.
    Coarse, and it is what the other test structurally cannot do.

    Edges are included, and only because the ledger now resolves them: a gate on an edge
    is played on its endpoint *qubits*, so `_ParameterLedger.blockers` asks about
    ``("q5", "rxy.amp180")`` as well as ``("q5_q10", "rxy.amp180")``. Before that, the
    declaration would have been true and inert.
    """
    undeclared: dict[str, list[str]] = {}
    for name in ROUTINE_NAMES:
        node = routine(name)
        gates, keywords = _gates_played(node)
        if not gates:
            continue
        missing = []
        if "clock_freqs.f01" not in node.reads:
            missing.append("clock_freqs.f01")
        supplies_own = any(word in keywords for word in OWN_AMPLITUDE_KWARGS)
        produces_it = "rxy.amp180" in node.updates and "rxy.amp180" not in node.reads
        if not supplies_own and not produces_it and "rxy.amp180" not in node.reads:
            missing.append("rxy.amp180")
        if missing:
            undeclared[name] = missing

    assert not undeclared, "\n".join(
        f"{name} plays a gate but does not declare {', '.join(paths)}"
        for name, paths in sorted(undeclared.items())
    )


def test_a_failed_qubit_spectroscopy_blocks_everything_that_needs_a_gate():
    """The graph-level consequence of those declarations, which is the point of them.

    Walks the real routine set in dependency order with `qubit_spectroscopy` failing and
    nothing else run, and asserts the propagation reaches the nodes that reported noise
    on the B chip. Before RFC 0007 §11.1 was fixed this list was empty and all of them
    ran.
    """
    from qpi_driver.tuners.base.dag import CalibrationDAG, _ParameterLedger

    config = CalibrationConfig(target_qubits=["q0"], target_edges=[])
    dag = CalibrationDAG(all_routines(), config)
    ledger = _ParameterLedger()

    skipped = []
    for name in dag.execution_order():
        node = dag.routines[name]
        if node.targets != "qubits":
            continue
        if ledger.blockers(node, "q0"):
            skipped.append(name)
            ledger.unsatisfied(node, "q0")
        elif name == "qubit_spectroscopy":
            ledger.unsatisfied(node, "q0")
        else:
            ledger.produced(node, "q0")

    for name in ("rabi", "t1", "t2_echo", "rb", "resonator_spectroscopy_excited"):
        assert name in skipped, (
            f"{name} would still run on an unmeasured f01: {skipped}"
        )
    # And the readout chain, which is what made the B chip's report eight-way.
    for name in ("readout_discrimination", "readout_fidelity"):
        assert name in skipped, f"{name} would still run: {skipped}"
    # Not everything: a node needing nothing f01 depends on must still run.
    assert "resonator_spectroscopy" not in skipped
    assert "time_of_flight" not in skipped


def test_a_punchout_sweep_reaches_full_readout_scale(own_quantify_tuner):
    """Punch-through is the high-power end, so a grid stopping at half cannot find it.

    This is the §5 hardware bound that got `resonator_punchout` switched off on both
    August 2026 chips, and that RFC 0007 §12 recorded as fixed in phase 3 when phase 3
    had only raised the ceilings in `single_qubit.py` and `ef.py`. `full_scale` had never
    been imported into `spectroscopy.py` at all.

    Unlike `rabi`, there is no accuracy bound pulling the other way: a resonator driven
    hard does not stop being a resonator, and a readout pulse past full scale simply
    clips. So this one goes to the top rather than to half and leaves escalation out of
    it. The B chip made the cost concrete — carrying `output_att: 20` on its readout, a
    grid stopping at 0.5 is around 26 dB short of what the module can emit.
    """
    from qpi_driver.tuners.base.limits import FULL_SCALE, full_scale

    element = own_quantify_tuner.device.get_element("q0")
    assert full_scale(element, "measure.pulse_amp") == pytest.approx(FULL_SCALE)

    node = routine("resonator_punchout")
    node.build_schedule(
        "q0",
        own_quantify_tuner.device,
        RoutineConfig(params={}),
        own_quantify_tuner.backend,
    )
    assert max(node._powers) == pytest.approx(FULL_SCALE)
    # And still starts low enough to have a dressed regime to compare against.
    assert min(node._powers) < 0.05


class TestTheResonatorSweepWidensItself:
    """RFC 0007 §11.2: the root of the graph could not re-centre its own window.

    `resonator_spectroscopy` writes the frequency every other node reads, so a refusal
    here stops the chip rather than one routine — and a resonator a few MHz outside its
    window is the commonest bring-up state there is, since fabrication scatter alone moves
    one by tens of MHz. It was the only escalating-class node with no escalation.

    Hermetic on purpose: the simulator has no resonator physics, so a real walk cannot
    exercise this, and the quantify fixtures are exactly the ones that fail on macOS.
    """

    #: The B chip's own numbers — a 20 MHz window with the line 12.8 MHz below its centre.
    LINEWIDTH_HZ = 3.3e5

    def test_a_centre_outside_the_window_asks_for_a_wider_span(self):
        """Not a flat refusal: the axis and the direction are both knowable here.

        The exact numbers are the B chip's own refusal — a fitted 7.11619 GHz against the
        [7.11899, 7.13899] GHz it had swept — so the factor below is what that run would
        have widened by.
        """
        from qpi_driver.tuners.fitting.core import OutOfRange, require_in_range

        with pytest.raises(OutOfRange) as raised:
            require_in_range(
                7.11619e9,
                7.11899e9,
                7.13899e9,
                what="resonator spectroscopy centre frequency",
                axis="span",
            )

        assert raised.value.axis == "span"
        assert raised.value.direction == "wider"
        # Derived from the excursion and doubled, because the extrapolation says which
        # side the line is on and not how far: 2.56x turns 20 MHz into 51.2 MHz, reaching
        # 25.6 MHz either side, which contains the 12.8 MHz that actually defeated it.
        assert raised.value.factor == pytest.approx(2.56, rel=0.02)

    def test_a_caller_with_no_span_to_widen_still_gets_a_plain_refusal(self):
        """Every other `require_in_range` caller must behave exactly as it did."""
        from qpi_driver.tuners.fitting.core import (
            FitError,
            OutOfRange,
            require_in_range,
        )

        with pytest.raises(FitError) as raised:
            require_in_range(9.0, 0.0, 1.0, what="T1")
        assert not isinstance(raised.value, OutOfRange)

    def test_widening_a_span_scales_its_points_to_hold_the_step(self):
        """A wider span at the same point count steps over the line it went to find."""
        from qpi_driver.tuners.base.routines import MAX_SWEEP_POINTS, _widened
        from qpi_driver.tuners.fitting.core import OutOfRange

        node = routine("resonator_spectroscopy")
        node._span = 20e6
        refusal = OutOfRange("out", axis="span", factor=4.0)

        widened = _widened(node, RoutineConfig(params={}), refusal)

        assert widened.get("span") == pytest.approx(80e6)
        assert widened.get("points") == 51 * 4
        # And it stops before the sequencer does.
        huge = _widened(node, RoutineConfig(params={"points": 400}), refusal)
        assert huge.get("points") == MAX_SWEEP_POINTS

    def test_it_finds_a_resonator_outside_its_first_window(self):
        """The whole point, end to end through `escalating`."""
        configured = 7.12899e9
        truth = configured - 12.8e6
        node = routine("resonator_spectroscopy")
        device = _FakeDevice(configured)
        backend = _DipBackend(node, truth, self.LINEWIDTH_HZ)

        # `points` and not `span`, so escalation stays free to widen the axis it names.
        # 501 over the 20 MHz default is a 40 kHz grid, which a 330 kHz resonator needs:
        # the default 51 points is 400 kHz, and `require_resolved_line` rightly refuses a
        # line thinner than the grid however wide the span gets.
        params = node.measure(
            "q0",
            device,
            RoutineConfig(params={"points": 501}),
            backend,
            None,
            timeout_s=60,
        )

        assert params["readout_frequency"] == pytest.approx(
            truth, abs=self.LINEWIDTH_HZ
        )
        # Two attempts: the 20 MHz default, then the widened one that contains the line.
        assert len(backend.spans) == 2
        assert backend.spans[0] == pytest.approx(20e6, rel=0.01)
        assert backend.spans[1] > 2 * 12.8e6, "the widened sweep must reach the line"

    def test_an_operator_who_set_the_span_is_not_overruled(self):
        """RFC 0007 §7: a named axis is a statement about the chip, not a default."""
        from qpi_driver.tuners.fitting.core import OutOfRange

        configured = 7.12899e9
        node = routine("resonator_spectroscopy")
        device = _FakeDevice(configured)
        backend = _DipBackend(node, configured - 12.8e6, self.LINEWIDTH_HZ)

        with pytest.raises(OutOfRange):
            node.measure(
                "q0",
                device,
                RoutineConfig(params={"span": 20e6, "points": 501}),
                backend,
                None,
                timeout_s=60,
            )
        assert len(backend.spans) == 1, "it should not have widened a stated sweep"


def _dip(frequencies: np.ndarray, centre: float, linewidth: float) -> np.ndarray:
    """A Lorentzian dip on a flat baseline, with enough scatter to be a real fit."""
    detuning = (frequencies - centre) / (linewidth / 2.0)
    signal = 1.0 - 0.9 / (1.0 + detuning**2)
    return signal + np.random.default_rng(0).normal(0.0, 0.002, frequencies.size)


class _FakeElement:
    def __init__(self, readout: float) -> None:
        self.name = "q0"
        self.clock_freqs = SimpleNamespace(readout=readout)


class _FakeDevice:
    """The least a spectroscopy routine needs: one element with a readout clock.

    No ``hardware_config``, so `addressable_band` returns ``None`` and nothing is band
    clamped — which is what a test about span arithmetic wants.
    """

    def __init__(self, readout: float) -> None:
        self._element = _FakeElement(readout)

    def get_element(self, name: str) -> _FakeElement:
        return self._element


class _DipBackend(SchedulerBackend):
    """Returns a resonator dip evaluated wherever *node* actually swept.

    Reads the setpoints back off the routine rather than off the schedule, because the
    schedule is the backend's own opaque object here and the frequencies are the only
    thing this needs to answer.
    """

    name = "dip"
    Reset = staticmethod(lambda *a, **k: ("reset", a, k))
    Measure = staticmethod(lambda *a, **k: ("measure", a, k))
    SetClockFrequency = staticmethod(lambda *a, **k: ("clock", a, k))
    BinMode = SimpleNamespace(AVERAGE="average", APPEND="append")

    def __init__(self, node: Any, centre: float, linewidth: float) -> None:
        self._node = node
        self._centre = centre
        self._linewidth = linewidth
        #: The span of each attempt, so a test can see escalation happen.
        self.spans: list[float] = []

    def new_schedule(self, name: str, repetitions: int = 1) -> Any:
        return SimpleNamespace(ops=[], add=lambda op: None)

    def run(self, schedule: Any, timeout_s: float = 0.0) -> xr.Dataset:
        frequencies = np.asarray(self._node._frequencies, dtype=float)
        self.spans.append(float(frequencies[-1] - frequencies[0]))
        return xr.Dataset(
            {"y": ("x", _dip(frequencies, self._centre, self._linewidth))}
        )


class TestAllXYRefusesAResponseItCannotNormalise:
    """The contrast between AllXY's own reference plateaus is its denominator.

    When that contrast is noise the normalisation divides by noise, which does not fail
    quietly — it manufactures a full-scale response. On the August 2026 B chip, whose qubit
    was never excited, `allxy` reported an rms deviation of 9.65 from a response ranging
    -22 to +12.5, and `allxy_check` turned the same data into a *fidelity of 0.533* and
    offered it to the drift check. Both reported success.
    """

    def test_it_refuses_a_response_that_is_only_noise(self):
        from qpi_driver.tuners.routines.single_qubit import normalised_allxy

        rng = np.random.default_rng(0)

        with pytest.raises(
            RoutineError, match="below the 3x a responding qubit clears"
        ):
            normalised_allxy(rng.normal(0.005, 0.0004, 21))

    def test_it_accepts_a_gate_that_is_merely_badly_calibrated(self):
        """The floor is about whether the qubit responds, not whether the gate is good."""
        from qpi_driver.tuners.routines.single_qubit import (
            ALLXY_IDEAL,
            normalised_allxy,
        )

        ideal = np.asarray(ALLXY_IDEAL)
        rng = np.random.default_rng(1)
        measured = (ideal + rng.normal(0, 0.08, 21)) * 0.01

        normalised = normalised_allxy(measured)

        rms = float(np.sqrt(np.mean((normalised - ideal) ** 2)))
        assert 0.02 < rms < 0.3, "a bad gate must still be measurable"

    def test_it_carries_the_sign_of_an_inverted_readout(self):
        """`resonator_spectroscopy` sits on the ground-state resonance, where |1> reflects
        less — so half of all chains produce a descending response."""
        from qpi_driver.tuners.routines.single_qubit import (
            ALLXY_IDEAL,
            normalised_allxy,
        )

        ideal = np.asarray(ALLXY_IDEAL)
        rng = np.random.default_rng(2)
        descending = (1.0 - ideal) * 0.01 + rng.normal(0, 0.0001, 21)

        normalised = normalised_allxy(descending)

        assert float(np.sqrt(np.mean((normalised - ideal) ** 2))) < 0.05

    def test_the_check_and_the_calibration_normalise_the_same_way(self):
        """`allxy_check` used min-max, which inverts on half of all readout chains."""
        import inspect

        from qpi_driver.tuners.routines import benchmarks

        source = inspect.getsource(benchmarks.AllXYCheck.analyse)
        assert "normalised_allxy" in source
        assert "np.min" not in source and "np.max" not in source


class TestTheConfirmingSweepHoldsItsStep:
    """RFC 0007: a power-broadened search must not make the confirming sweep too coarse.

    The B chip's search found q5's line at 5319999360 Hz — within one 2 MHz step of the
    5.318 GHz its VNA reported, so the location was right. Then the confirming sweep refused
    it at all three drive powers, each for "linewidth below the sweep step". The line was
    0.8 MHz wide and the step was 1.2 MHz.

    The span is sized from the width the search measured, and the search ran at a drive
    power that broadened a 0.8 MHz line to 32 MHz across. With `CONFIRM_POINTS` fixed at 41,
    a 48 MHz span *is* a 1.2 MHz step — so the routine located the line and then swept too
    coarsely to see it.
    """

    B_CHIP = {
        "span": 20e6,
        "points": 151,
        "drive_amps": [0.1, 0.2, 0.4],
        "search_span": 1860e6,
        "search_points": 931,
    }

    def test_a_broadened_search_still_leaves_a_step_that_resolves_the_line(self):
        node = routine("qubit_spectroscopy")
        width = 32e6

        span = node._confirm_span(RoutineConfig(params=self.B_CHIP), width)
        points = node._confirm_points(
            RoutineConfig(params=self.B_CHIP), _NoElements(), "q5", width
        )

        step = span / (points - 1)
        assert span == pytest.approx(48e6), "the span still follows the measured width"
        assert step < 0.8e6, (
            f"a 0.8 MHz line needs a finer step than {step / 1e3:.0f} kHz"
        )
        # And within the sequencer's reach: one schedule covers every drive power.
        assert points * len(self.B_CHIP["drive_amps"]) <= MAX_SWEEP_POINTS

    def test_it_never_sweeps_fewer_points_than_it_used_to(self):
        """The floor matters: a chip whose narrow pass is coarse must not lose resolution."""
        node = routine("qubit_spectroscopy")
        coarse = dict(self.B_CHIP, span=40e6, points=11)

        points = node._confirm_points(
            RoutineConfig(params=coarse), _NoElements(), "q5", 1e6
        )

        assert points == node.CONFIRM_POINTS

    def test_it_holds_the_step_the_operator_asked_for(self):
        node = routine("qubit_spectroscopy")
        config = RoutineConfig(params=self.B_CHIP)
        width = 4e6

        span = node._confirm_span(config, width)
        points = node._confirm_points(config, _NoElements(), "q5", width)

        # 20 MHz over 151 points is 133 kHz, and that is what the confirm sweep uses.
        assert span / (points - 1) == pytest.approx(20e6 / 150, rel=0.05)


class _NoElements:
    """A device with no elements, for the sizing arithmetic that never touches one.

    `_confirm_points` asks `_drive_amplitudes` how many powers it will sweep, and that
    reads the element only when the config names none — which these configs all do.
    """

    def get_element(self, name: str) -> Any:
        raise AssertionError("the config names drive_amps, so no element is needed")


class TestYamlsQuietFloatTrap:
    """PyYAML reads an exponent as a number only with a decimal point *and* a signed one.

    So `5.318e+9` is a float while `5.318e9`, `20e6` and `20.0e6` are strings, and the four
    forms are indistinguishable in a hand-written file. Nothing downstream objects loudly: a
    qcodes frequency parameter accepts the string and stores it, and `_current_clock` calls
    `float` on the way into a sweep, so the fault surfaces only where a schedule is compiled
    and the string reaches something that wanted Hz.

    `calibration.example.yml` shipped every time axis in the string form, which is how this
    was found.
    """

    def test_a_sweep_axis_written_the_natural_way_arrives_as_numbers(self):
        from qpi_driver.tuners.base.routines import setpoints_of

        config = RoutineConfig(params=yaml.safe_load("delays: [4e-9, 1.0e-6, 2.0e+0]"))

        delays = setpoints_of(config, "delays", [])

        assert delays == [4e-9, 1.0e-6, 2.0]
        assert all(isinstance(d, float) for d in delays)

    def test_counts_stay_integers(self):
        """`depths` and `repetitions` index APIs that want an int, not a quantity."""
        from qpi_driver.tuners.base.routines import setpoints_of

        config = RoutineConfig(params={"depths": [1, 2, 4]})

        assert setpoints_of(config, "depths", []) == [1, 2, 4]
        assert all(isinstance(d, int) for d in setpoints_of(config, "depths", []))

    def test_a_setpoint_that_is_not_a_number_is_refused(self):
        from qpi_driver.tuners.base.routines import setpoints_of

        with pytest.raises(RoutineError, match="is not a number"):
            setpoints_of(RoutineConfig(params={"delays": ["soon"]}), "delays", [])

    def test_a_device_config_frequency_written_the_natural_way_loads_as_a_number(self):
        from qpi_driver.tuners.utils.persistence import _numeric

        assert _numeric("5.318e9") == pytest.approx(5.318e9)
        assert isinstance(_numeric("5.318e9"), float)
        assert _numeric(7.183e9) == pytest.approx(7.183e9)
        # And a parameter that is genuinely a string is left for its own validator.
        assert _numeric("BasicTransmonElement") == "BasicTransmonElement"

    def test_the_example_config_is_numbers_on_the_page(self):
        """It is what operators copy, so its own spelling has to be the right one."""
        example = yaml.safe_load(
            (Path(__file__).parent.parent / "calibration.example.yml").read_text()
        )

        offenders = {}
        for name, params in (example["routines"] or {}).items():
            for key, value in (params or {}).items():
                values = value if isinstance(value, list) else [value]
                if any(isinstance(v, str) for v in values):
                    offenders[f"{name}.{key}"] = value
        assert not offenders, f"write these with a signed exponent: {offenders}"


class TestTheSearchDrivesAsHardAsTheConfirmPass:
    """The widening pass must not probe weaker than the pass it exists to feed.

    `SEARCH_AMPLITUDE` was a constant equal to `DEFAULT_AMPLITUDES`' ceiling, so the
    docstring's "the strongest this routine would try anyway" held only until either moved.
    An operator raising `drive_amps` to 0.3 left the search at 0.08 — 3.75x weaker than the
    confirm pass, and backwards, because the search is the one that has to see a line at all
    while the confirm pass is where a gentle power belongs.
    """

    def test_the_search_power_follows_the_configured_drive_amps(self):
        node = routine("qubit_spectroscopy")
        config = RoutineConfig(params={"drive_amps": [0.01, 0.03, 0.1, 0.3]})

        assert max(node._drive_amplitudes(config, _NoElements(), "q5")) == 0.3

    def test_an_operator_can_still_name_the_search_power(self):
        config = RoutineConfig(params={"drive_amps": [0.3], "search_amp": 0.05})

        assert float(config.get("search_amp", 999)) == 0.05

    def test_the_default_ladder_is_geometric(self):
        """Geometric because the scale is unknown; a linear ladder sits in one decade.

        Deliberately *not* asserting a ceiling. Raising it was tried and reverted: a B-chip
        transition needs more than 0.08 to clear the 5x floor, and a ladder reaching 0.3 puts
        the simulated chip's f01 1.76 MHz out against a 1 MHz tolerance. The right ceiling
        belongs to the chip and its drive chain, so it lives in `drive_amps`.
        """
        rungs = routine("qubit_spectroscopy").DEFAULT_AMPLITUDES

        ratios = [b / a for a, b in zip(rungs, rungs[1:])]
        assert max(ratios) - min(ratios) < 0.5, f"not geometric: {ratios}"
        assert max(rungs) / min(rungs) >= 10, "too narrow to bracket an unknown scale"

    def test_the_ladder_fits_the_sequencer_at_a_raised_point_count(self):
        """`drive_amps` multiplies the acquisition count, and the ceiling is real.

        Against the sequencer's own limit, not `MAX_SWEEP_POINTS` — that caps the *points*
        an escalating sweep may reach, and the drive ladder multiplies on top of it. A
        frequency sweep measured 15.0 Q1ASM instructions per acquisition on a QRM-RF against
        a 12288 ceiling, so the real bound is about 818 acquisitions.

        Five rungs at 151 points is 755, which fits at 92% — close enough that an operator
        adding a sixth power, or more points, will trip the warning that quantify only logs.
        """
        node = routine("qubit_spectroscopy")

        acquisitions = len(node.DEFAULT_AMPLITUDES) * 151

        assert acquisitions * INSTRUCTIONS_PER_ACQUISITION <= Q1ASM_CEILING


class TestEscalationStopsAtFullScale:
    """A widened amplitude sweep must not ask the AWG for more than it has.

    `rabi` starts at half scale so escalation can reach the rest, and said exactly that in
    a comment while nothing enforced it. On the B chip a 4x widening of 0-0.5 asked for
    0-2.0 and the compiler refused the 21st setpoint:

        awg_gain_0 is set to 1.0495151796199138. Parameter must be in the range
        -1.0 <= awg_gain_0 <= 1.0 for Pulse Rxy(180, 0, 'q5')

    Which names a pulse rather than the routine, and is a compile failure rather than a
    fit refusal — so it says nothing about where the pi pulse actually is.
    """

    def _rabi_at(self, top: float):
        from qpi_driver.tuners.base.routines import linear_setpoints

        node = routine("rabi")
        node._amplitudes = linear_setpoints(0.0, top, 41)
        node._amplitudes_ceiling = 1.0
        return node

    def test_widening_clamps_to_the_ceiling(self):
        from qpi_driver.tuners.base.routines import _widened
        from qpi_driver.tuners.fitting.core import OutOfRange

        node = self._rabi_at(0.5)
        refusal = OutOfRange("above the sweep", axis="amplitudes", factor=4.0)

        widened = _widened(node, RoutineConfig(params={}), refusal)

        assert max(widened.get("amplitudes")) == pytest.approx(1.0)
        assert len(widened.get("amplitudes")) == 41

    def test_a_sweep_already_at_the_ceiling_stops_rather_than_repeating(self):
        """Re-running an identical sweep to get an identical refusal wastes a chip's time."""
        from qpi_driver.tuners.base.routines import _widened
        from qpi_driver.tuners.fitting.core import OutOfRange

        node = self._rabi_at(1.0)
        config = RoutineConfig(params={})

        assert (
            _widened(
                node,
                config,
                refusal := OutOfRange("above the sweep", axis="amplitudes", factor=4.0),
            )
            is config
        ), refusal

    def test_an_axis_with_no_ceiling_is_unbounded(self):
        """Coherence delays have no hardware ceiling — only the routine timeout."""
        from qpi_driver.tuners.base.routines import _widened, linear_setpoints
        from qpi_driver.tuners.fitting.core import OutOfRange

        node = routine("t1")
        node._delays = linear_setpoints(0.0, 80e-6, 21)

        widened = _widened(
            node,
            RoutineConfig(params={}),
            OutOfRange("no decay", axis="delays", factor=4.0),
        )

        assert max(widened.get("delays")) == pytest.approx(320e-6)


class TestARefusalKeepsItsFit:
    """A guard rejecting a fit is when that fit most wants looking at.

    Three consecutive runs of the B chip refused `rabi_12` on the sqrt(2) ladder, and none
    of them could say whether the sweep behind the refusal was a real oscillation or a
    harmonic of a non-sinusoidal readout — because raising discarded it. The report kept
    the sentence and lost the trace.
    """

    def _report(self):
        from qpi_driver.tuners.base.report import CalibrationReport

        return CalibrationReport(timestamp="now", duration_s=0.0, mode="full")

    def _refuse(self, report, exc):
        from qpi_driver.tuners.base.dag import _record_refused_fit

        _record_refused_fit(report, routine("rabi_12"), "q5", exc, time.monotonic())
        return report

    def test_the_refused_sweep_reaches_the_report(self):
        sweep = {"x": [0.0, 0.1], "measured": [0.2, 0.3], "fitted": [0.2, 0.3]}
        report = self._refuse(self._report(), RoutineError("off the ladder", fit=sweep))

        assert [r.fit for r in report.routine_results] == [sweep]

    def test_it_claims_no_measurement(self):
        """Nothing was applied and nothing was written, and empty parameters say so."""
        report = self._refuse(
            self._report(), RoutineError("off the ladder", fit={"x": [1.0]})
        )
        result = report.routine_results[0]

        assert result.failed
        assert result.parameters == {}

    def test_a_refusal_with_nothing_fitted_adds_nothing(self):
        """A guard that fires before anything was fitted has no trace to give, and an
        empty result would read as a routine that ran."""
        report = self._refuse(self._report(), RoutineError("no line in the sweep"))

        assert report.routine_results == []

    def test_both_error_hierarchies_can_carry_one(self):
        """`RoutineError` and `FitError` are unrelated, which is why the DAG recovers the
        fit duck-typed rather than by type."""
        from qpi_driver.tuners.fitting.core import FitError

        assert RoutineError("x", fit={"a": 1}).fit == {"a": 1}
        assert FitError("x", fit={"a": 1}).fit == {"a": 1}
        assert RoutineError("x").fit is None


class TestTheEfPiPulseIsHeldToTheLadder:
    """A transmon's 1-2 matrix element is sqrt(2) times its 0-1 one, so the amplitude is not
    free: at the same duration the same rotation needs ``amp180 / sqrt(2)``.

    `fit_rabi` fits a cosine and halves its period, and a partial rotation is still a cosine
    — driven too weakly it finds a longer period and reports a *smaller* amplitude with no
    sign anything is wrong. On the August 2026 B chip that wrote `ef_amp180` of 0.0677
    against an `amp180` of 0.5757, six times below the ladder, and every EF node after it
    measured a qubit still in |1>: the second-excited sweep put |2> *closer* to |0> than |1>
    is, which no transmon does, and `three_state_discrimination` was the only node to refuse.
    """

    B_CHIP_AMP180 = 0.5757070085511985
    B_CHIP_EF = 0.06766417047411832
    #: What `rxy` plays on that chip, against the 20 ns the ef pulse defaults to.
    B_CHIP_RXY_DURATION = 56e-9

    def _device(self, amp180: float, duration: float = 20e-9):
        element = SimpleNamespace(
            rxy=SimpleNamespace(amp180=amp180, duration=duration), name="q5"
        )
        return SimpleNamespace(get_element=lambda name: element)

    def test_the_b_chip_s_ef_pulse_is_refused(self):
        from qpi_driver.tuners.routines.ef import _require_ef_ladder

        with pytest.raises(RoutineError, match="sqrt.2. ladder allows"):
            _require_ef_ladder(
                self._device(self.B_CHIP_AMP180),
                "q5",
                self.B_CHIP_EF,
                20e-9,
                span=0.05,
            )

    def test_a_pulse_on_the_ladder_is_accepted(self):
        from qpi_driver.tuners.routines.ef import EF_ENVELOPE_AREA, _require_ef_ladder

        _require_ef_ladder(  # noqa: B018
            self._device(0.4), "q5", 0.4 * EF_ENVELOPE_AREA / 2**0.5, 20e-9
        )

    def test_a_shorter_ef_pulse_needs_proportionally_more_amplitude(self):
        """Rotation follows area, so the bound has to carry the durations too.

        The B chip's `rxy` is 56 ns against an ef pulse of 20, a factor of 2.8 that is
        larger than the whole window the bound allows — so without this a chip whose ef
        pulse was exactly right would be refused, and the message would blame the drive.
        """
        from qpi_driver.tuners.routines.ef import EF_ENVELOPE_AREA, _require_ef_ladder

        on_the_ladder = 0.4 * (56 / 20) * EF_ENVELOPE_AREA / 2**0.5
        _require_ef_ladder(  # noqa: B018
            self._device(0.4, duration=56e-9), "q5", on_the_ladder, 20e-9
        )
        # And what the duration-blind bound would have accepted is now refused.
        with pytest.raises(RoutineError, match="not the same length"):
            _require_ef_ladder(
                self._device(0.4, duration=56e-9),
                "q5",
                0.4 * EF_ENVELOPE_AREA / 2**0.5,
                20e-9,
            )

    def test_the_envelopes_are_not_the_same_shape(self):
        """`rxy` is a Gaussian and the ef pulse is a square, so equal amplitudes are not
        equal rotations, and the bound has to carry the area ratio.

        It moves the *centre* by 1.6x and does not by itself change any verdict, since 1.6
        sits inside the factor of two the bound allows — so this asserts the arithmetic
        rather than a refusal. What it buys is that the bound is centred on the pulse the
        routine actually plays, which is what makes the factor of two a real margin instead
        of most of it being spent on a known systematic.
        """
        from qpi_driver.tuners.routines.ef import EF_ENVELOPE_AREA, _require_ef_ladder

        assert EF_ENVELOPE_AREA == pytest.approx(0.6267, rel=0.01)
        # The sqrt(2)-only prediction is 1.6x high, which is inside the window either way.
        _require_ef_ladder(self._device(0.4), "q5", 0.4 / 2**0.5, 20e-9)  # noqa: B018

    def test_a_resolved_oscillation_is_accepted_however_far_off_the_ladder(self):
        """The failure this guard exists for has a signature, and it is the opposite one.

        A drive too weak to turn a pi leaves the cosine's half period longer than the
        sweep, so the fit extrapolates an arc — *less* than one oscillation, never more.
        The August 2026 B chip's `rabi_12` sweep holds three and a half of them, evenly
        spaced with a flat envelope, and the ladder refused it three runs running.
        """
        from qpi_driver.tuners.routines.ef import _require_ef_ladder

        _require_ef_ladder(  # noqa: B018
            self._device(0.5622, duration=56e-9), "q5", 0.06751, 56e-9, span=0.5
        )

    def test_a_partial_rotation_this_far_off_the_ladder_is_still_refused(self):
        """Same amplitude and same ladder violation; only the sweep is different."""
        from qpi_driver.tuners.routines.ef import _require_ef_ladder

        with pytest.raises(RoutineError, match="of an oscillation"):
            _require_ef_ladder(
                self._device(0.5622, duration=56e-9), "q5", 0.06751, 56e-9, span=0.05
            )

    @pytest.mark.parametrize("factor", (0.55, 1.9))
    def test_the_bound_is_generous_enough_for_a_differing_duration(self, factor):
        """The EF pulse need not be the same length as the 0-1 one, so this is a factor of
        two either way rather than the 10% the relation itself holds to."""
        from qpi_driver.tuners.routines.ef import _require_ef_ladder

        from qpi_driver.tuners.routines.ef import EF_ENVELOPE_AREA

        _require_ef_ladder(  # noqa: B018
            self._device(0.4), "q5", factor * 0.4 * EF_ENVELOPE_AREA / 2**0.5, 20e-9
        )

    def test_no_amp180_to_compare_against_is_not_evidence(self):
        """`rabi` may be disabled or skipped, and refusing then would be the wrong reason."""
        from qpi_driver.tuners.routines.ef import _require_ef_ladder

        _require_ef_ladder(self._device(0.0), "q5", 0.0677, 20e-9)  # noqa: B018
        _require_ef_ladder(  # noqa: B018
            SimpleNamespace(get_element=lambda n: None), "q5", 0.0677, 20e-9
        )

    def test_an_unreadable_rxy_duration_is_not_evidence_either(self):
        """The ratio needs both lengths, and half of one is not a bound."""
        from qpi_driver.tuners.routines.ef import _require_ef_ladder

        no_duration = SimpleNamespace(
            get_element=lambda n: SimpleNamespace(
                rxy=SimpleNamespace(amp180=0.5757), name="q5"
            )
        )
        _require_ef_ladder(no_duration, "q5", 0.0677, 20e-9)  # noqa: B018


class TestRamseyRefinesUntilTheResidualIsUnresolvable:
    """RFC 0007 §11.5: one pass lands near the answer rather than on it.

    `analyse` corrects f01 by ``current_f01 - detuning``, and the detuning was measured with
    the *old* f01 in the drive — so a megahertz of error means the fringe was fitted a
    megahertz off resonance. Each pass starts from where the last left the device, so the
    residual falls geometrically.

    The B chip's single pass moved f01 by 1.032 MHz and left an AllXY whose entire error was
    in the equator block, which reads as either a residual detuning or a pi/2 amplitude
    error. Iterating measures the first directly, which is what settles the ambiguity.
    """

    def _ramsey_with(self, delays):
        node = routine("ramsey")
        node._delays = list(delays)
        return node

    def test_the_floor_comes_from_the_window_the_operator_swept(self):
        """A fringe over a window T is resolved to about 1/(2 pi T); below that is noise."""
        node = self._ramsey_with([4e-9, 24e-6])

        floor = node._detuning_floor(RoutineConfig(params={}))

        assert floor == pytest.approx(1.0 / (2 * np.pi * (24e-6 - 4e-9)), rel=1e-6)
        # A shorter window resolves less, so it stops sooner.
        assert (
            self._ramsey_with([0.0, 6e-6])._detuning_floor(RoutineConfig(params={}))
            > floor
        )

    def test_no_delays_yet_means_no_floor_rather_than_a_crash(self):
        node = routine("ramsey")
        node._delays = []

        assert node._detuning_floor(RoutineConfig(params={})) == 0.0

    def test_it_refines_until_the_detuning_is_under_the_floor(self):
        """Each pass returns a smaller residual, and the loop stops when one is small."""
        node = self._ramsey_with([4e-9, 24e-6])
        residuals = iter([1.032e6, 4.1e4, 1.2e3])
        applied: list[float] = []

        node.escalating = lambda *a, **k: {  # type: ignore[method-assign]
            "detuning": next(residuals),
            "clock_freq_01": 5.318e9,
        }
        node.apply = lambda device, target, params: applied.append(  # type: ignore[method-assign]
            params["detuning"]
        )

        result = node.measure("q5", None, RoutineConfig(params={}), None)

        assert result["detuning"] == pytest.approx(1.2e3), (
            "it should keep the last, best pass"
        )
        # Applied between passes, which is the mechanism: the next drive uses the correction.
        assert applied == [pytest.approx(1.032e6), pytest.approx(4.1e4)]

    def test_it_stops_when_the_residual_stops_falling(self):
        """Another pass would be measuring noise, so keep the better of the two."""
        node = self._ramsey_with([4e-9, 24e-6])
        residuals = iter([5.0e4, 6.0e4, 7.0e4])
        node.escalating = lambda *a, **k: {  # type: ignore[method-assign]
            "detuning": next(residuals),
            "clock_freq_01": 5.318e9,
        }
        node.apply = lambda *a, **k: None  # type: ignore[method-assign]

        result = node.measure("q5", None, RoutineConfig(params={}), None)

        assert result["detuning"] == pytest.approx(5.0e4), "the first was the best"

    def test_a_first_pass_already_on_resonance_costs_nothing(self):
        node = self._ramsey_with([4e-9, 24e-6])
        passes = []

        def once(*a, **k):
            passes.append(1)
            return {"detuning": 500.0, "clock_freq_01": 5.318e9}

        node.escalating = once  # type: ignore[method-assign]

        result = node.measure("q5", None, RoutineConfig(params={}), None)

        assert len(passes) == 1, "500 Hz is under the 6.6 kHz this window resolves"
        assert result["detuning"] == 500.0


class TestASweepThatIsTheWrongSizeIsResized:
    """Escalation in both directions, on the four nodes the B chip's last run refused.

    Each of these had found its answer and thrown it away because the window was wrong,
    which is RFC 0007's whole subject. Three wanted more reach; one wanted less.
    """

    def test_drag_asks_for_a_wider_beta_sweep_rather_than_failing(self):
        """The B chip fitted -0.4803 against a swept +/-0.2 and refused, leaving every
        node downstream running on an uncorrected pulse. `drag_12` already widened."""
        from qpi_driver.tuners.fitting import fit_drag
        from qpi_driver.tuners.fitting.core import OutOfRange

        betas = np.linspace(-0.2, 0.2, 31)
        with pytest.raises(OutOfRange) as raised:
            fit_drag(betas, 0.00899 * (betas + 0.4803), axis="motzois")

        assert raised.value.axis == "motzois"
        assert raised.value.direction == "wider"

    def test_drag_escalates_where_it_used_only_to_raise(self):
        assert routine("drag").measures_itself

    def test_an_amplified_rotation_that_overran_asks_to_be_shortened(self):
        """The one refusal that wants a *smaller* sweep. The B chip's pi/2 turned 1.51
        rad by its thirteenth pulse, past where sin(n*d) is still n*d."""
        from qpi_driver.tuners.fitting import fit_fine_amplitude
        from qpi_driver.tuners.fitting.core import OutOfRange

        counts = np.array([1.0, 5.0, 9.0, 13.0])
        # The chip's own trace rather than a clean sine, which flattens and drags the
        # fitted slope back under the bound — the case that does not need catching.
        demodulated = np.array([0.16425, 0.77211, -0.30209, -1.02863])
        with pytest.raises(OutOfRange) as raised:
            fit_fine_amplitude(
                counts,
                0.5 + 0.5 * demodulated,
                0.284,
                ground=0.0,
                excited=1.0,
                turn=np.pi / 2,
                pre_rotation=0.0,
            )

        assert raised.value.axis == "repetitions"
        assert raised.value.direction == "shorter"

    def test_the_generic_widening_declines_to_shorten(self):
        """Every sweep that asks to be shortened is a repetition ladder, and a stretch
        breaks one: halving [1, 5, 9, 13] would give [1, 3, 5, 7], no longer 4k+1."""
        from qpi_driver.tuners.base.routines import _widened
        from qpi_driver.tuners.fitting.core import OutOfRange

        node = routine("fine_amplitude_90")
        node._repetitions = [1, 5, 9, 13]
        config = RoutineConfig(params={})
        refusal = OutOfRange("x", axis="repetitions", direction="shorter", factor=0.66)

        assert _widened(node, config, refusal) is config

    def test_the_ladder_is_rebuilt_rather_than_interpolated(self):
        from qpi_driver.tuners.routines.single_qubit import _shortened

        assert _shortened([1, 5, 9, 13], 0.66, 4) == [1, 5]
        assert _shortened(list(range(1, 26)), 0.43, 1) == list(range(1, 11))
        # Never below two points, which is what the two-parameter fit needs.
        assert _shortened([1, 5, 9, 13], 0.01, 4) == [1, 5]

    def test_a_coherence_time_past_its_window_asks_for_longer_delays(self):
        """The B chip fitted 2.12 ms of T2 over a 100 us window — on a chip whose T1 was
        56 us — and refused un-escalatably, because this guard named no axis."""
        from qpi_driver.tuners.fitting.core import OutOfRange, require_in_range

        with pytest.raises(OutOfRange) as raised:
            require_in_range(2.12285e-3, 0.0, 1.0e-3, what="T2", axis="delays")

        assert raised.value.axis == "delays"
        assert raised.value.direction == "wider"
        # 100 us widened by this reaches past the 2.12 ms it could not contain.
        assert 100e-6 * raised.value.factor * 10 > 2.12285e-3

    def test_rb_averages_harder_when_the_decay_is_lost_in_its_own_scatter(self):
        """The axis is the circuit count, not the depths: what the guard compares is the
        decay's span against the scatter around it, and scatter is what averaging buys
        down. The chip's own refusal was 0.6214 against 0.3231."""
        from qpi_driver.tuners.base.routines import (
            MAX_CIRCUITS_PER_DEPTH,
            _widened,
        )
        from qpi_driver.tuners.fitting.core import OutOfRange

        node = routine("rb")
        node._circuits_per_depth = 10
        refusal = OutOfRange("x", axis="circuits_per_depth", factor=4.0)

        assert _widened(node, RoutineConfig(params={}), refusal).get(
            "circuits_per_depth"
        ) == min(40, MAX_CIRCUITS_PER_DEPTH)

    def test_rb_stops_at_the_ceiling_rather_than_running_forever(self):
        """RB is the most expensive node in the graph and the cost is linear here."""
        from qpi_driver.tuners.base.routines import (
            MAX_CIRCUITS_PER_DEPTH,
            _widened,
        )
        from qpi_driver.tuners.fitting.core import OutOfRange

        node = routine("rb")
        node._circuits_per_depth = MAX_CIRCUITS_PER_DEPTH
        config = RoutineConfig(params={})
        refusal = OutOfRange("x", axis="circuits_per_depth", factor=4.0)

        # Unchanged, which is how `escalating` knows to re-raise instead of re-running.
        assert _widened(node, config, refusal) is config

    def test_a_benchmark_that_measures_itself_still_reaches_the_report(self):
        """`add_benchmarks_from` was only on the branch for routines that do *not* run
        their own loop, so giving `rb` an escalation silently emptied
        `report.benchmarks` while leaving it in `routine_results` — it looked like it had
        run, and the drift check compared against nothing.
        """
        from qpi_driver.tuners.base.report import CalibrationReport

        report = CalibrationReport(timestamp="now", duration_s=0.0, mode="full")
        report.add_benchmarks_from(
            "rb", "q0", {"fidelity": 0.994, "error_per_gate": 0.006}
        )

        assert report.fidelities() == {"q0": 0.994}
        # And the routines that take this path are the ones that used to lose it.
        assert routine("rb").is_benchmark and routine("rb").measures_itself
        assert (
            routine("interleaved_rb").is_benchmark
            and routine("interleaved_rb").measures_itself
        )

    def test_every_node_the_b_chip_refused_now_resizes_itself(self):
        """The five failures of its last run, as one statement."""
        assert routine("t2_echo").measures_itself
        assert routine("drag").measures_itself
        assert routine("fine_amplitude").measures_itself
        assert routine("fine_amplitude_90").measures_itself
        assert routine("rb").measures_itself
        assert routine("interleaved_rb").measures_itself

    def test_both_fine_amplitude_nodes_resize_themselves(self):
        assert routine("fine_amplitude").measures_itself
        assert routine("fine_amplitude_90").measures_itself
