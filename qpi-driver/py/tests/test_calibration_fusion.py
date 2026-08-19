"""One schedule over several targets, and the dataset it returns (RFC 0009 §6)."""

import numpy as np
import pytest
import xarray as xr

from qpi_driver.tuners.base.config import (
    CalibrationConfig,
    ParallelConfig,
    RoutineConfig,
)
from qpi_driver.tuners.base.dag import CalibrationDAG
from qpi_driver.tuners.base.fusion import (
    add_after,
    add_together,
    channel_of,
    channels_of,
)
from qpi_driver.tuners.base.routines import CalibrationRoutine, RoutineError
from qpi_driver.tuners.fitting.core import OutOfRange, signal_of
from qpi_driver.tuners.routines import ROUTINE_CLASSES
from tests.utils.simulation import FakeDevice, FakeElement, StubBackend
from qpi_driver.tuners.base.sweep import Sweep


def _sweeps(targets):
    """One `Sweep` per target, as the walk hands them over."""
    return {target: Sweep(target) for target in targets}


def _routine(name):
    return next(cls() for cls in ROUTINE_CLASSES if cls().name == name)


def _grouped(*qubits):
    """A device and backend a group schedule can be built against, without the simulator.

    `SimulatedTuner` would do it and needs `scqubits`, which lives in the ``sim`` extra —
    and `test-py-driver` installs one executor extra and never ``sim``, so these tests
    failed on the import under every leg of that matrix. Nothing here runs physics: they
    check which acquisition channel each target lands on and that the resets coincide.
    """
    return (
        FakeDevice(
            {
                qubit: FakeElement(
                    name=qubit,
                    clock_freqs={"f01": 5.0e9, "f12": 4.75e9, "readout": 7.1e9},
                    rxy={"amp180": 0.18, "motzoi": 0.0},
                    measure={"pulse_amp": 0.25},
                )
                for qubit in qubits
            },
            {},
        ),
        StubBackend(),
    )


def _dataset(channels):
    """An acquisition dataset keyed by integer channel, as a cluster returns it.

    Positional, not keyword: the keys are integers, which `**` cannot carry.
    """
    return xr.Dataset(
        {
            channel: (("acq_index",), np.asarray(values))
            for channel, values in channels.items()
        }
    )


class TestAligningOneSchedule:
    """`schedule.add` appends, so a fused schedule has to say otherwise."""

    def test_operations_added_together_share_a_start(self):
        backend = StubBackend()
        schedule = backend.new_schedule("t")

        add_together(schedule, [backend.Reset(t) for t in ("q0", "q1", "q2")])

        assert schedule.starts_of("Reset") == [0.0, 0.0, 0.0]

    def test_appending_instead_would_serialise_them(self):
        """The failure this exists to prevent: a group as long as the sequential run."""
        backend = StubBackend()
        schedule = backend.new_schedule("t")

        for target in ("q0", "q1", "q2"):
            schedule.add(backend.Reset(target))

        assert len(set(schedule.starts_of("Reset"))) == 3

    def test_a_following_stage_starts_after_the_longest_of_the_last(self):
        backend = StubBackend()
        schedule = backend.new_schedule("t")

        anchor = add_together(
            schedule,
            [
                backend.Reset("q0", duration=1e-6),
                backend.Reset("q1", duration=5e-6),
            ],
        )
        add_after(schedule, [backend.Rxy(qubit="q0"), backend.Rxy(qubit="q1")], anchor)

        # 5 us, not 1: referencing the shorter one would overlap the longer.
        assert schedule.starts_of("Rxy") == [5e-6, 5e-6]

    def test_adding_nothing_together_anchors_nothing(self):
        backend = StubBackend()
        assert add_together(backend.new_schedule("t"), []) is None

    def test_adding_nothing_after_keeps_the_anchor(self):
        backend = StubBackend()
        schedule = backend.new_schedule("t")
        anchor = add_together(schedule, [backend.Reset("q0")])

        assert add_after(schedule, [], anchor) is anchor


class TestSlicingTheAcquisitionApart:
    def test_each_channel_is_read_on_its_own(self):
        dataset = _dataset({0: [1.0, 2.0], 1: [3.0, 4.0]})

        assert list(channel_of(dataset, 1).data_vars) == [1]

    def test_an_unfused_single_variable_dataset_reads_as_channel_zero(self):
        """A group of one must go down exactly the path it did before fusion existed."""
        dataset = xr.Dataset({"magnitude": (("acq_index",), [1.0, 2.0])})

        assert channel_of(dataset, 0) is dataset

    def test_a_missing_channel_is_an_error_and_not_someone_elses_data(self):
        dataset = _dataset({0: [1.0], 1: [2.0]})

        with pytest.raises(KeyError, match="no channel 5"):
            channel_of(dataset, 5)

    def test_targets_are_sliced_by_their_position_in_the_group(self):
        dataset = _dataset({0: [1.0], 1: [2.0], 2: [3.0]})

        sliced = channels_of(dataset, ["q0", "q2", "q4"])

        assert list(sliced) == ["q0", "q2", "q4"]
        assert float(sliced["q2"][1].values[0]) == 2.0

    def test_a_target_with_no_channel_is_left_out_rather_than_misfed(self):
        dataset = _dataset({0: [1.0]})

        assert list(channels_of(dataset, ["q0", "q1"])) == ["q0"]


class TestTheHook:
    def test_an_unconverted_routine_declines_a_group(self):
        """A local class rather than a named routine: this used to point at whichever
        routine happened to be unconverted, and broke each time one was converted."""

        class Unconverted(CalibrationRoutine):
            name = "unconverted"

            def build_schedule(self, target, device, config, backend, sweep):
                return backend.new_schedule(self.name)

            def analyse(self, dataset, target, device, config, sweep):
                return {}

        routine = Unconverted()
        assert not routine.fusable

        with pytest.raises(RoutineError, match="cannot measure 3 targets"):
            routine.build_group_schedule(
                ["q0", "q1", "q2"],
                None,
                RoutineConfig(),
                StubBackend(),
                _sweeps(["q0", "q1", "q2"]),
            )

    def test_an_unconverted_routine_still_builds_for_one_target(self):
        """The default has to reproduce today's behaviour exactly."""
        routine = _routine("allxy")
        device, backend = _grouped("q0")

        schedule = routine.build_group_schedule(
            ["q0"], device, RoutineConfig(), backend, _sweeps(["q0"])
        )

        assert schedule is not None

    def test_a_converted_routine_measures_every_target_on_its_own_channel(self):
        routine = _routine("allxy")
        device, backend = _grouped("q0", "q1", "q2")

        schedule = routine.build_group_schedule(
            ["q0", "q2"], device, RoutineConfig(), backend, _sweeps(["q0", "q2"])
        )

        channels = [
            op.kwargs["acq_channel"]
            for op in schedule.operations
            if op.kind == "Measure"
        ]
        # Two per pair, one channel each, and never both on zero.
        assert set(channels) == {0, 1}

    def test_a_converted_routines_pulses_coincide(self):
        routine = _routine("allxy")
        device, backend = _grouped("q0", "q1")

        schedule = routine.build_group_schedule(
            ["q0", "q1"], device, RoutineConfig(), backend, _sweeps(["q0", "q1"])
        )

        resets = schedule.starts_of("Reset")
        # Pairwise equal within each pair of the 21: every target resets together.
        assert resets[0] == resets[1]


class FusableProbe(CalibrationRoutine):
    """A routine whose fit is whatever its own channel says.

    Every target's correct answer differs, which is what makes a demultiplexing
    mistake visible: wired to channel zero, all three would report the same number
    and every same-answer test ever written would still pass.
    """

    name = "probe"
    targets = "qubits"

    def build_group_schedule(self, targets, device, config, backend, sweeps):
        schedule = backend.new_schedule(self.name)
        add_together(
            schedule,
            [
                backend.Measure(target, acq_channel=channel)
                for channel, target in enumerate(targets)
            ],
        )
        return schedule

    def build_schedule(self, target, device, config, backend, sweep):
        return self.build_group_schedule(
            [target], device, config, backend, {target: sweep}
        )

    def analyse(self, dataset, target, device, config, sweep):
        return {"value": float(signal_of(dataset)[0])}


class ChannelBackend(StubBackend):
    """Returns one value per channel, and counts the acquisitions it was asked for."""

    def __init__(self, per_channel):
        self.per_channel = per_channel
        self.runs = []

    def run(self, schedule, timeout_s=None):
        self.runs.append(schedule)
        return _dataset({c: [v] for c, v in enumerate(self.per_channel)})


class TestAFusedWalk:
    """The walk's own plumbing: one acquisition, one fit per target."""

    def _walk(self, backend, **parallel):
        config = CalibrationConfig(
            target_qubits=["q0", "q1", "q2"],
            parallel=ParallelConfig(**parallel),
        )
        report = CalibrationDAG([FusableProbe()], config).run(
            device=None, backend=backend, config=config
        )
        return report, {r.target: r.parameters["value"] for r in report.routine_results}

    def test_every_target_is_fitted_from_its_own_channel(self):
        """The adversarial one. Channel zero for all three would give 10, 10, 10."""
        backend = ChannelBackend([10.0, 20.0, 30.0])

        report, values = self._walk(backend, enabled=True, qubit_spacing=1)

        assert report.status == "success", report.errors
        assert values == {"q0": 10.0, "q1": 20.0, "q2": 30.0}

    def test_one_acquisition_serves_the_whole_group(self):
        backend = ChannelBackend([10.0, 20.0, 30.0])

        self._walk(backend, enabled=True, qubit_spacing=1)

        assert len(backend.runs) == 1

    def test_a_walk_that_did_not_ask_runs_one_acquisition_per_target(self):
        backend = ChannelBackend([10.0, 20.0, 30.0])

        report, values = self._walk(backend)

        assert len(backend.runs) == 3
        # Each ran alone, so each read channel zero — its own.
        assert values == {"q0": 10.0, "q1": 10.0, "q2": 10.0}

    def test_a_target_with_no_channel_fails_and_the_rest_of_the_group_lands(self):
        backend = ChannelBackend([10.0, 20.0])

        report, values = self._walk(backend, enabled=True, qubit_spacing=1)

        assert values == {"q0": 10.0, "q1": 20.0}
        assert report.status == "partial_failure"
        assert any("q2" in error and "no channel" in error for error in report.errors)

    def test_a_failed_acquisition_is_recorded_against_every_target_in_the_group(self):
        class Broken(ChannelBackend):
            def run(self, schedule, timeout_s=None):
                raise RuntimeError("the cluster went away")

        report, values = self._walk(Broken([]), enabled=True, qubit_spacing=1)

        assert values == {}
        assert len(report.errors) == 3
        assert all("the cluster went away" in error for error in report.errors)

    def test_the_walk_names_every_target_in_flight(self):
        backend = ChannelBackend([10.0, 20.0, 30.0])
        config = CalibrationConfig(
            target_qubits=["q0", "q1", "q2"],
            parallel=ParallelConfig(enabled=True, qubit_spacing=1),
        )
        updates: list[dict] = []

        CalibrationDAG([FusableProbe()], config).run(
            device=None,
            backend=backend,
            config=config,
            on_progress=updates.append,
        )

        starts = [u["running"] for u in updates if "running" in u]
        assert starts == [["q0", "q1", "q2"]]


class TestEscalationOverAGroup:
    """RFC 0009 §6.5 — one acquisition for the group, re-fused only for the refused."""

    def _grouped(self, *qubits):
        """The `_grouped` device, with a backend that answers and counts."""
        device, _stub = _grouped(*qubits)
        return device, ChannelBackend([1.0] * len(qubits))

    def _node(self, refuse_until):
        """A fusable routine whose fit refuses *refuse_until* on the named targets."""

        class Widening(FusableProbe):
            name = "widening"
            attempts: list[tuple[str, float]] = []

            def build_group_schedule(self, targets, device, config, backend, sweeps):
                # A real setpoint list, because that is what `_widened` stretches.
                reach = [float(r) for r in config.get("reach", [0.0, 1.0])]
                for target in targets:
                    sweeps[target]["reach"] = reach
                return super().build_group_schedule(
                    targets, device, config, backend, sweeps
                )

            def analyse(self, dataset, target, device, config, sweep):
                reach = max(sweep["reach"])
                self.attempts.append((target, reach))
                if reach < refuse_until.get(target, 0.0):
                    raise OutOfRange("too short", axis="reach", factor=4.0)
                return {"reach": reach}

        node = Widening()
        node.attempts = []
        return node

    def test_a_group_that_all_fits_is_one_acquisition(self):
        node = self._node({})
        device, backend = self._grouped("q0", "q1", "q2")
        targets = ["q0", "q1", "q2"]

        results = node.escalating_group(
            targets, device, RoutineConfig(), backend, 60.0, _sweeps(targets)
        )

        assert set(results) == set(targets)
        assert len(backend.runs) == 1, (
            "nothing refused, so nothing should be re-measured"
        )

    def test_only_the_refused_target_is_measured_again(self):
        node = self._node({"q1": 3.0})
        device, backend = self._grouped("q0", "q1", "q2")
        targets = ["q0", "q1", "q2"]

        results = node.escalating_group(
            targets, device, RoutineConfig(), backend, 60.0, _sweeps(targets)
        )

        assert results["q1"]["reach"] == pytest.approx(4.0)
        # q0 and q2 fitted on the first pass and were not swept over q1's wider range.
        assert [t for t, _ in node.attempts if t == "q0"] == ["q0"]
        assert [t for t, _ in node.attempts if t == "q2"] == ["q2"]
        assert len(backend.runs) == 2

    def test_two_targets_refusing_the_same_axis_are_refused_together(self):
        node = self._node({"q0": 3.0, "q2": 3.0})
        device, backend = self._grouped("q0", "q1", "q2")
        targets = ["q0", "q1", "q2"]

        node.escalating_group(
            targets, device, RoutineConfig(), backend, 60.0, _sweeps(targets)
        )

        # One widened acquisition for both, not one each.
        assert len(backend.runs) == 2

    def test_a_target_that_never_fits_carries_its_refusal_and_the_rest_land(self):
        node = self._node({"q1": 1e9})
        device, backend = self._grouped("q0", "q1")
        targets = ["q0", "q1"]

        results = node.escalating_group(
            targets, device, RoutineConfig(), backend, 60.0, _sweeps(targets)
        )

        assert isinstance(results["q1"], OutOfRange)
        assert results["q0"]["reach"] == pytest.approx(1.0)

    def test_an_axis_the_operator_named_is_left_alone(self):
        """RFC 0007 §7 — a named axis is a statement about the chip."""
        node = self._node({"q0": 3.0})
        device, backend = self._grouped("q0")
        targets = ["q0"]

        results = node.escalating_group(
            targets,
            device,
            RoutineConfig(params={"reach": [0.0, 1.0]}),
            backend,
            60.0,
            _sweeps(targets),
        )

        assert isinstance(results["q0"], OutOfRange)
        assert len(backend.runs) == 1, "it must not widen an axis the operator set"


class TestAGridDerivedPerTargetSplitsTheGroup:
    """RFC 0009 D7 — one schedule cannot hold two different sweeps of the same axis."""

    def _device(self, **t1s):
        return FakeDevice(
            {
                qubit: FakeElement(
                    name=qubit,
                    clock_freqs={"f01": 5.0e9, "f12": 4.75e9, "readout": 7.1e9},
                    rxy={"amp180": 0.18, "motzoi": 0.0},
                    measure={"pulse_amp": 0.25},
                    coherence={"t1": t1},
                )
                for qubit, t1 in t1s.items()
            },
            {},
        )

    def test_targets_whose_windows_agree_stay_one_group(self):
        node = _routine("t2_echo")
        device = self._device(q0=40e-6, q1=40e-6)

        assert node.compatible_groups(["q0", "q1"], device, RoutineConfig()) == [
            ["q0", "q1"]
        ]

    def test_targets_whose_windows_differ_are_split(self):
        """An idle is dead time on every port at once, so there is no per-target time
        axis — a qubit with twice the T1 wants twice the window and cannot share one."""
        node = _routine("t2_echo")
        device = self._device(q0=40e-6, q1=90e-6, q2=40e-6)

        groups = node.compatible_groups(["q0", "q1", "q2"], device, RoutineConfig())

        assert sorted(sorted(g) for g in groups) == [["q0", "q2"], ["q1"]]

    def test_a_stated_window_puts_every_target_back_together(self):
        """An operator who names the delays has made one statement about the whole chip."""
        node = _routine("t2_echo")
        device = self._device(q0=40e-6, q1=90e-6)
        config = RoutineConfig(params={"delays": [0.0, 10e-6, 20e-6]})

        assert node.compatible_groups(["q0", "q1"], device, config) == [["q0", "q1"]]

    def test_a_routine_whose_sweep_is_fixed_never_splits(self):
        node = _routine("allxy")

        assert node.compatible_groups(["q0", "q1"], None, RoutineConfig()) == [
            ["q0", "q1"]
        ]


def test_no_routine_keeps_per_target_state_on_itself():
    """RFC 0009 §6.6 — a value derived from one target and read back in `analyse` is the
    bug the `Sweep` exists to prevent, and a grep for it is the only thing that scales.

    Found five that a narrower search missed: `_current_amp180`, `_current_amp90`,
    `_current_f01`, `_current_f12` and `_f01` all name a digit, so a pattern of
    `[a-z_]+` walked straight past them.
    """
    import pathlib
    import re

    import qpi_driver.tuners.routines as package

    root = pathlib.Path(package.__file__).parent
    assignment = re.compile(r"^\s+self\._([a-z_0-9]+)\s*=", re.M)
    offenders = {
        module.name: sorted(set(assignment.findall(module.read_text())))
        for module in sorted(root.glob("*.py"))
        if assignment.search(module.read_text())
    }

    assert not offenders, (
        "these keep per-target state on the routine, which a fused group shares: "
        f"{offenders}"
    )


class TestARoutineWithItsOwnLoopIsNotBypassed:
    """A fusable schedule must not cost a routine its `measure` (RFC 0009 §6.5).

    `ramsey` is the case: its loop refines f01 across several passes, applying to the
    device between them. Giving it a group schedule made it `fusable`, and the walk would
    then have taken the fused path — one acquisition, one fit, no refinement at all.
    """

    class _OwnLoop(FusableProbe):
        name = "own_loop"

        def measure(
            self, target, device, config, backend, sweep, bias=None, timeout_s=300.0
        ):
            sweep["looped"] = True
            return {"value": 1.0}

    def test_the_walk_runs_it_one_target_at_a_time(self):
        node = self._OwnLoop()
        assert node.fusable and node.measures_itself and not node.measures_group

        config = CalibrationConfig(
            target_qubits=["q0", "q1", "q2"],
            parallel=ParallelConfig(enabled=True, qubit_spacing=1),
        )
        backend = ChannelBackend([1.0, 2.0, 3.0])
        report = CalibrationDAG([node], config).run(
            device=None, backend=backend, config=config
        )

        assert report.status == "success"
        # Its own loop ran for each target, and no fused acquisition was taken.
        assert len(report.routine_results) == 3
        assert backend.runs == []

    def test_it_groups_once_it_has_a_group_loop(self):
        """The guard is about the loop being absent, not about the routine measuring itself."""
        node = _routine("t1")
        assert node.measures_itself and node.measures_group


def test_an_operation_carrying_its_duration_directly_anchors_the_group():
    """`add_together` returns the longest operation so the next stage cannot start early,
    and an operation may state its duration as an attribute rather than in kwargs."""
    from types import SimpleNamespace

    from qpi_driver.tuners.base.fusion import _duration_of

    assert _duration_of(SimpleNamespace(duration=4e-7)) == pytest.approx(4e-7)
    assert _duration_of(SimpleNamespace(kwargs={"duration": 2e-7})) == pytest.approx(
        2e-7
    )
    # Neither, which is every gate whose length the device config decides.
    assert _duration_of(SimpleNamespace(kwargs={})) == 0.0


class TestTheAcceptanceMeasurement:
    """RFC 0009 §5.6 — what turns `qubit_spacing` from a guess into a setting.

    Gambetta et al.'s measurement: benchmark each qubit alone, benchmark them together,
    and the difference in average gate fidelity is the addressability.
    """

    class _Benchmark(FusableProbe):
        """A benchmark whose fidelity is lower when it is measured in company."""

        name = "penalised"
        benchmark = True
        alone = 0.999
        together = 0.990

        def analyse(self, dataset, target, device, config, sweep):
            fused = len(sweep.get("group", ())) > 1
            return {"fidelity": self.together if fused else self.alone}

        def build_group_schedule(self, targets, device, config, backend, sweeps):
            for target in targets:
                sweeps[target]["group"] = list(targets)
            return super().build_group_schedule(
                targets, device, config, backend, sweeps
            )

    def _walk(self, **parallel):
        config = CalibrationConfig(
            target_qubits=["q0", "q1", "q2"],
            parallel=ParallelConfig(enabled=True, qubit_spacing=1, **parallel),
        )
        backend = ChannelBackend([1.0, 1.0, 1.0])
        report = CalibrationDAG([self._Benchmark()], config).run(
            device=None, backend=backend, config=config
        )
        return report, backend

    def test_it_is_absent_unless_the_run_asks_for_it(self):
        """It doubles what benchmarking costs, so it cannot be the default."""
        report, backend = self._walk()

        assert [b.parallel_penalty for b in report.benchmarks] == [None] * 3
        assert len(backend.runs) == 1, "one grouped acquisition and no control pass"

    def test_it_reports_what_company_cost_each_target(self):
        report, backend = self._walk(measure_penalty=True)

        penalty = self._Benchmark.alone - self._Benchmark.together
        assert [b.parallel_penalty for b in report.benchmarks] == [
            pytest.approx(penalty)
        ] * 3
        # The grouped pass, then one isolated acquisition per target.
        assert len(backend.runs) == 4

    def test_the_reported_fidelity_stays_the_grouped_one(self):
        """The fused numbers describe how the chip will actually be driven; the isolated
        pass is a control, and its results are dropped."""
        report, _backend = self._walk(measure_penalty=True)

        assert [b.fidelity for b in report.benchmarks] == [
            pytest.approx(self._Benchmark.together)
        ] * 3
        # Three benchmarks, not six: the control pass added no rows of its own.
        assert len(report.benchmarks) == 3
        assert len(report.routine_results) == 3

    def test_a_routine_that_is_not_a_benchmark_is_left_alone(self):
        """There is no fidelity to difference, so there is nothing to measure."""
        config = CalibrationConfig(
            target_qubits=["q0", "q1"],
            parallel=ParallelConfig(
                enabled=True, qubit_spacing=1, measure_penalty=True
            ),
        )
        backend = ChannelBackend([1.0, 2.0])
        CalibrationDAG([FusableProbe()], config).run(
            device=None, backend=backend, config=config
        )

        assert len(backend.runs) == 1


class TestNothingIsReadBeforeItIsDriven:
    """A fused schedule's readout must follow every pulse it is meant to measure.

    The bug this exists for: a raw pulse — an EF drive, say — is added by a helper that
    *appends*, so a group's pulses played one after another instead of together, and the
    readout stayed anchored to whatever came before them. It then preceded some targets'
    pulses entirely. Nothing raised: each target's own sequence was in order, so only the
    timings show it.
    """

    def _timings(self, name, *targets):
        node = _routine(name)
        device, _stub = _grouped(*targets)
        backend = StubBackend()
        schedule = node.build_group_schedule(
            list(targets),
            device,
            RoutineConfig(params={"shots": 8}),
            backend,
            _sweeps(targets),
        )
        return schedule

    @pytest.mark.parametrize("name", ["rabi_12", "ef_ladder"])
    def test_every_readout_follows_every_drive_pulse(self, name):
        schedule = self._timings(name, "q0", "q1")

        pulses = [
            p
            for p in schedule.placed
            if getattr(p.operation, "kind", "") in ("SquarePulse", "DRAGPulse")
        ]
        readouts_placed = [
            p for p in schedule.placed if getattr(p.operation, "kind", "") == "Measure"
        ]
        assert pulses and readouts_placed

        # Every pulse ends before the readout that comes after it starts. Compared
        # against the *earliest* readout following each pulse, since the sweep repeats.
        for pulse in pulses:
            after = [r.start for r in readouts_placed if r.start >= pulse.start]
            assert after, f"a {name} pulse at {pulse.start} has no readout after it"
            assert min(after) >= pulse.end - 1e-15, (
                f"{name} reads at {min(after)} a pulse that ends at {pulse.end}"
            )

    @pytest.mark.parametrize("name", ["rabi_12", "ef_ladder"])
    def test_the_drive_pulses_of_a_group_coincide(self, name):
        schedule = self._timings(name, "q0", "q1")

        starts = [
            p.start
            for p in schedule.placed
            if getattr(p.operation, "kind", "") in ("SquarePulse", "DRAGPulse")
        ]
        # Two targets per setpoint, so the starts come in equal pairs.
        assert len(starts) % 2 == 0
        assert all(
            starts[i] == pytest.approx(starts[i + 1]) for i in range(0, len(starts), 2)
        ), "a group's pulses should start together, not queue"


class TestChunkingSurvivesFusion:
    """A routine that splits its sweep across schedules must still split it in a group.

    The bug this exists for: the fused path called `build_group_schedule` and `run`
    directly, so it went straight past `acquire` — where the split lives. `rb` chunks on
    the shipped defaults (ten circuits over the shipped depths is 1270 Cliffords against
    the 1000 one schedule holds), so every grouped RB would have built a program too long
    to assemble, which is the failure the chunking exists to prevent.
    """

    def test_rb_still_chunks_when_it_is_grouped(self):
        from qpi_driver.tuners.routines.benchmarks import (
            DEFAULT_RB_CIRCUITS,
            DEFAULT_RB_DEPTHS,
            MAX_RB_CLIFFORDS,
        )

        node = _routine("rb")
        per_schedule = max(1, MAX_RB_CLIFFORDS // sum(DEFAULT_RB_DEPTHS))
        assert DEFAULT_RB_CIRCUITS > per_schedule, (
            "this test is only meaningful while the defaults chunk"
        )

        device, _stub = _grouped("q0", "q1")
        # Enough acquisitions per channel for a chunk to unpack: the references plus one
        # per depth per circuit.
        wide = per_schedule * len(DEFAULT_RB_DEPTHS) + 2
        backend = _WideChannelBackend(channels=2, per_channel=wide)
        targets = ["q0", "q1"]
        node.acquire_group(
            targets, device, RoutineConfig(), backend, 60.0, _sweeps(targets)
        )

        assert len(backend.runs) > 1, "a grouped RB must split its circuits too"

    def test_a_chunking_routine_is_not_fused_without_a_group_acquire(self):
        """The guard that makes an unconverted one safe rather than silently unchunked."""

        class Unconverted(FusableProbe):
            name = "unconverted"

            def acquire(self, target, device, config, backend, timeout_s, sweep):
                return super().acquire(
                    target, device, config, backend, timeout_s, sweep
                )

        assert Unconverted().chunks_acquisition
        assert not _routine("rb").chunks_acquisition


class _WideChannelBackend(StubBackend):
    """Answers every channel with *per_channel* points, and counts the acquisitions.

    `ChannelBackend` returns one point per channel, which is enough for a probe whose fit
    reads a single value and not enough for a chunked sweep to unpack.
    """

    def __init__(self, channels: int, per_channel: int):
        self.channels = channels
        self.per_channel = per_channel
        self.runs: list = []

    def run(self, schedule, timeout_s=None):
        self.runs.append(schedule)
        return _dataset(
            {c: list(range(self.per_channel)) for c in range(self.channels)}
        )
