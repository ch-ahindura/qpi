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
from qpi_driver.tuners.fitting.core import signal_of
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
        routine = _routine("t1")
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
