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

import pytest
from qpi_driver.compat.qblox import IS_QBLOX_SCHEDULER_INSTALLED
from qpi_driver.compat.quantify import IS_QUANTIFY_INSTALLED
from qpi_driver.tuners.base.config import CalibrationConfig, RoutineConfig
from qpi_driver.tuners.base.routines import RoutineError
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
    """`_require_resolved_line` judges the fit, not only the sweep that produced it.

    Both spectroscopy roots write a frequency straight to the device — f01, and the
    readout frequency every other node then reads at — so a Lorentzian centre drawn
    through noise does not merely produce a bad report, it overwrites the last good
    value and breaks the nodes after it. Twice on hardware, costing a run each time.
    """

    #: What the chip actually returned. The first reproduced across runs; the second was
    #: taken through a starved readout; the third was 5 MHz from both of its neighbours,
    #: from data flat to 0.7%, and overwrote f01 with it — which put `ramsey_12`'s
    #: detuning 1.5 MHz out. The simulated chip, for scale, returns 127.
    MEASURED_SNR = ((3.55, True), (1.56, False), (1.32, False))

    @pytest.mark.parametrize("snr,accepted", MEASURED_SNR)
    def test_it_accepts_only_the_fit_that_reproduced(self, snr, accepted):
        from qpi_driver.tuners.base.routines import require_resolved_line

        # 200 kHz line on a 133 kHz grid: wide enough that only the snr decides.
        fitted = {"linewidth": 200e3, "snr": snr}
        frequencies = [4.7e9 + 133e3 * i for i in range(3)]
        if accepted:
            require_resolved_line(fitted, frequencies)  # noqa: B018 - no raise is it
        else:
            with pytest.raises(RoutineError, match="above the residual scatter"):
                require_resolved_line(fitted, frequencies)

    def test_a_line_narrower_than_the_sweep_is_still_refused(self):
        """The original check, and the opposite shape: sharp fit, coarse sweep."""
        from qpi_driver.tuners.base.routines import require_resolved_line

        with pytest.raises(RoutineError, match="narrower than"):
            require_resolved_line(
                {"linewidth": 2379.0, "snr": 50.0},
                [6.827e9 + 400e3 * i for i in range(3)],
            )

    def test_a_fit_that_reports_no_snr_is_judged_on_width_alone(self):
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
