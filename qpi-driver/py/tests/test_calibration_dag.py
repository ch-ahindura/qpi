"""The routine graph and the walk over it (RFC 0004 §5).

These run with stub routines and a fake backend: the ordering, the failure
handling and the refusal to report success having done nothing are all
properties of the DAG, not of any scheduler.
"""

import json
import logging
from typing import Any

import numpy as np
import pytest
import xarray as xr
from qpi_driver.tuners.base import RECALIBRATION_ROOTS, Tuner
from qpi_driver.tuners.base.backend import SchedulerBackend
from qpi_driver.tuners.base.provenance import Provenance, ProvenanceStore
from qpi_driver.tuners.base.config import (
    DEFAULT_ROUTINE_TIMEOUT_S,
    CalibrationConfig,
    RoutineConfig,
)
from qpi_driver.tuners.base.dag import CalibrationDAG, _human_duration
from qpi_driver.tuners.base.report import (
    MAX_FIT_PAYLOAD_BYTES,
    CalibrationReport,
    RoutineResult,
)
from qpi_driver.tuners.base.routines import (
    CalibrationRoutine,
    CheckOutcome,
    RoutineError,
    linear_setpoints,
    setpoints_of,
)
from qpi_driver.tuners.fitting.core import OutOfRange
from qpi_driver.tuners.routines import ROUTINE_CLASSES, all_routines, routine_names


class FakeBackend(SchedulerBackend):
    """Records the schedules it was given and hands back a fixed dataset."""

    name = "fake"

    def __init__(self, dataset: xr.Dataset | None = None) -> None:
        self.ran: list[Any] = []
        self.timeouts: list[float] = []
        self._dataset = (
            dataset if dataset is not None else xr.Dataset({"y": ("x", [1.0, 2.0])})
        )

    def new_schedule(self, name: str, repetitions: int = 1) -> Any:
        return {"name": name, "ops": []}

    def run(
        self, schedule: Any, timeout_s: float = DEFAULT_ROUTINE_TIMEOUT_S
    ) -> xr.Dataset:
        self.ran.append(schedule)
        self.timeouts.append(timeout_s)
        return self._dataset


class StubRoutine(CalibrationRoutine):
    """A routine that succeeds, recording where it ran."""

    def __init__(self, name, depends_on=(), targets="qubits", benchmark=False):
        self.name = name
        self.depends_on = depends_on
        self.targets = targets
        self.benchmark = benchmark
        self.applied: list[tuple[str, dict]] = []

    def build_schedule(self, target, device, config, backend):
        return backend.new_schedule(self.name)

    def analyse(self, dataset, target, device, config):
        return {"fidelity": 0.999, "value": 1.0}

    def apply(self, device, target, params):
        self.applied.append((target, params))


class FailingRoutine(StubRoutine):
    def analyse(self, dataset, target, device, config):
        raise RoutineError("could not fit")


class SelfMeasuringRoutine(StubRoutine):
    """Runs its own acquisition loop, as `coupler_anticrossing` does."""

    def measure(
        self,
        target,
        device,
        config,
        backend,
        bias=None,
        timeout_s=DEFAULT_ROUTINE_TIMEOUT_S,
    ):
        backend.run(backend.new_schedule(self.name), timeout_s=timeout_s)
        return {"value": 1.0}


class CheckableRoutine(StubRoutine):
    """A stub whose check outcome is scripted rather than measured.

    *verdict* is ``True`` (in spec), ``False`` (out of spec), or ``None`` — no
    check at all, which is the default for every real routine and is the case the
    recursion has to treat as *unknown* rather than as failure.
    """

    def __init__(self, name, depends_on=(), verdict=None, **kwargs):
        super().__init__(name, depends_on=depends_on, **kwargs)
        self.verdict = verdict
        self.checked: list[str] = []

    @property
    def has_check(self) -> bool:
        return self.verdict is not None

    def build_check_schedule(self, target, device, config, backend):
        if self.verdict is None:
            return None
        self.checked.append(target)
        return backend.new_schedule(f"{self.name}_check")

    def analyse_check(self, dataset, target, device, config):
        return CheckOutcome(
            passed=bool(self.verdict), margin=0.5 if self.verdict else 2.0
        )


class UnevaluableCheck(CheckableRoutine):
    """A check that raises. Not evidence of drift — the node stays unknown."""

    def __init__(self, name, depends_on=()):
        super().__init__(name, depends_on=depends_on, verdict=True)

    def analyse_check(self, dataset, target, device, config):
        raise RoutineError("the check itself could not be evaluated")


def _diagnose(routines, seeds, config=None):
    dag = CalibrationDAG(routines, config or _config())
    order, notes = dag.diagnose(
        seeds, device=None, backend=FakeBackend(), config=config or _config()
    )
    return order, notes


def _config(**kwargs) -> CalibrationConfig:
    kwargs.setdefault("target_qubits", ["q0"])
    return CalibrationConfig(**kwargs)


class TestOrdering:
    """The DAG's topological execution order, and what a cycle or an unknown dependency reports."""

    def test_execution_order_is_topological(self):
        dag = CalibrationDAG(
            [
                StubRoutine("c", depends_on=("b",)),
                StubRoutine("a"),
                StubRoutine("b", depends_on=("a",)),
            ],
            _config(),
        )
        assert dag.execution_order() == ["a", "b", "c"]

    def test_a_cycle_is_reported(self):
        dag = CalibrationDAG(
            [StubRoutine("a", depends_on=("b",)), StubRoutine("b", depends_on=("a",))],
            _config(),
        )
        with pytest.raises(ValueError, match="cycle detected"):
            dag.execution_order()

    def test_a_dependency_on_an_unknown_routine_is_reported(self):
        """A typo in depends_on would silently drop an edge and reorder the walk."""
        dag = CalibrationDAG([StubRoutine("a", depends_on=("ghost",))], _config())
        with pytest.raises(ValueError, match="depends on unknown routine"):
            dag.execution_order()

    def test_a_disabled_routine_is_dropped_but_its_dependents_keep_their_order(self):
        config = _config(routines={"b": RoutineConfig(enabled=False)})
        dag = CalibrationDAG(
            [
                StubRoutine("a"),
                StubRoutine("b", depends_on=("a",)),
                StubRoutine("c", depends_on=("b",)),
            ],
            config,
        )
        assert dag.execution_order() == ["a", "c"]

    def test_partial_order_includes_everything_downstream(self):
        """Recalibrating a routine invalidates what was tuned from it."""
        dag = CalibrationDAG(
            [
                StubRoutine("a"),
                StubRoutine("b", depends_on=("a",)),
                StubRoutine("c", depends_on=("b",)),
            ],
            _config(),
        )
        assert dag.partial_order(["b"]) == ["b", "c"]
        assert dag.partial_order(["a"]) == ["a", "b", "c"]

    def test_a_partial_recalibration_skips_the_readout_bring_up(self):
        """A partial run has to be cheaper than a full one, or there is no reason for it.

        The saving is the readout chain: `resonator_spectroscopy` and
        `resonator_punchout` are a bring-up step, not a drift one, and they are most
        of the cost. Everything from `qubit_spectroscopy` down still runs, because
        re-finding a frequency invalidates the gates tuned against it.
        """
        dag = CalibrationDAG(all_routines(), _config())
        order = dag.partial_order(list(RECALIBRATION_ROOTS))

        assert "resonator_spectroscopy" not in order
        assert "resonator_punchout" not in order
        assert order[0] == "qubit_spectroscopy"
        assert {"rabi", "ramsey", "drag", "fine_amplitude", "rb"} <= set(order)
        assert set(order) < set(dag.execution_order())


class TestDiagnose:
    """RFC 0005 §8's diagnose: which nodes recalibrate, given scripted check outcomes.

    Graph logic, so it belongs here: a fabricated graph with scripted check outcomes
    and no simulator. What is asserted is *which* nodes get recalibrated, because
    that is the entire value of having checks — the alternative is re-running from
    the root and hoping.
    """

    def test_with_no_checks_diagnose_reproduces_the_old_behaviour(self):
        """The additive property, and the reason this could ship without a flag.

        No routine had a check before this, so every node's state is unknown, no
        dependency can be blamed, and blame stops at the seed — which is `partial_order`
        of the seed, exactly what `recalibrate` did in RFC 0004.
        """
        routines = [
            CheckableRoutine("a"),
            CheckableRoutine("b", depends_on=("a",)),
            CheckableRoutine("c", depends_on=("b",)),
        ]
        order, _notes = _diagnose(routines, ["b"])
        assert order == ["b", "c"]

    def test_a_node_whose_check_passes_is_not_recalibrated(self):
        """The cheapest outcome, and one RFC 0004 could not reach at all."""
        routines = [
            CheckableRoutine("a", verdict=True),
            CheckableRoutine("b", depends_on=("a",), verdict=True),
            CheckableRoutine("c", depends_on=("b",), verdict=True),
        ]
        order, notes = _diagnose(routines, ["b"])
        assert order == []
        assert any("nothing to recalibrate" in note for note in notes)

    def test_blame_moves_up_to_the_failing_dependency(self):
        """The whole point of the recursion.

        `c` is out of spec, but so is `a` two levels above it. Recalibrating `c` would
        measure the wrong thing twice, because its input is wrong. Blame stops at `a`,
        and everything downstream of `a` follows.
        """
        routines = [
            CheckableRoutine("a", verdict=False),
            CheckableRoutine("b", depends_on=("a",), verdict=False),
            CheckableRoutine("c", depends_on=("b",), verdict=False),
        ]
        order, _notes = _diagnose(routines, ["c"])
        assert order == ["a", "b", "c"]

    def test_blame_stops_where_the_dependency_is_verified_good(self):
        """`b` is out of spec and `a` is fine, so the fault is `b`'s own."""
        routines = [
            CheckableRoutine("a", verdict=True),
            CheckableRoutine("b", depends_on=("a",), verdict=False),
            CheckableRoutine("c", depends_on=("b",), verdict=False),
        ]
        order, _notes = _diagnose(routines, ["c"])
        assert order == ["b", "c"]
        assert "a" not in order

    def test_an_unknown_dependency_does_not_attract_blame(self):
        """The asymmetry that keeps this from recalibrating everything.

        `b` failed and `a` has no check. Unknown is not failure: with no evidence
        against `a` there is no reason to re-run the bring-up, so `b` is blamed. Were
        unknown treated as failed, every diagnose would walk to the root and a partial
        recalibration would cost more than a full one.
        """
        routines = [
            CheckableRoutine("a", verdict=None),
            CheckableRoutine("b", depends_on=("a",), verdict=False),
        ]
        order, _notes = _diagnose(routines, ["b"])
        assert order == ["b"]

    def test_a_check_that_raises_leaves_the_node_unknown(self):
        """A broken check is not a drifting parameter, and must not read as one."""
        routines = [
            UnevaluableCheck("a"),
            CheckableRoutine("b", depends_on=("a",), verdict=False),
        ]
        order, _notes = _diagnose(routines, ["b"])
        assert order == ["b"], "an unevaluable check on `a` should not blame `a`"

    def test_diagnose_checks_each_routine_once_however_many_paths_reach_it(self):
        """A diamond reaches `a` twice. Checks cost instrument time, so they are cached."""
        root = CheckableRoutine("a", verdict=False)
        routines = [
            root,
            CheckableRoutine("b", depends_on=("a",), verdict=False),
            CheckableRoutine("c", depends_on=("a",), verdict=False),
            CheckableRoutine("d", depends_on=("b", "c"), verdict=False),
        ]
        order, _notes = _diagnose(routines, ["d"])
        assert order == ["a", "b", "c", "d"]
        assert root.checked == ["q0"], f"`a` was checked {len(root.checked)} times"

    def test_the_worst_target_decides(self):
        """Drift on one qubit of several is drift. A mean would hide it."""

        class PerTarget(CheckableRoutine):
            def analyse_check(self, dataset, target, device, config):
                passed = target != "q1"
                return CheckOutcome(passed=passed, margin=0.1 if passed else 3.0)

        config = _config(target_qubits=["q0", "q1", "q2"])
        dag = CalibrationDAG([PerTarget("a", verdict=True)], config)
        outcome = dag.check("a", config.target_qubits, None, FakeBackend(), config)

        assert outcome is not None and not outcome.passed
        assert "q1" in outcome.detail

    def test_the_real_graph_reports_a_readout_drift_instead_of_hiding_it(self):
        """The behaviour change that motivated all of this.

        RFC 0004 excluded the readout chain from a partial run, so a drifted readout
        frequency had no symptom other than every downstream fit getting worse.
        `resonator_spectroscopy` has a check now, so when it fails the blame walks up
        into it and the bring-up runs after all — which is more expensive, and correct.
        """
        routines = all_routines()
        by_name = {r.name: r for r in routines}
        assert by_name["resonator_spectroscopy"].has_check
        assert by_name["rabi"].has_check

        # Script the two real checks: readout has moved, and the pi pulse is off with it.
        scripted = [
            CheckableRoutine(
                r.name,
                depends_on=r.depends_on,
                targets=r.targets,
                verdict=False if r.name in ("resonator_spectroscopy", "rabi") else None,
            )
            for r in routines
        ]
        order, notes = _diagnose(scripted, list(RECALIBRATION_ROOTS))

        assert "resonator_spectroscopy" in order, (
            "a failing readout check should pull the bring-up back into the run"
        )
        assert order.index("resonator_spectroscopy") < order.index("rabi")
        assert any("resonator_spectroscopy" in note for note in notes)

    def test_recalibrate_narrows_both_the_targets_and_the_routines(self):
        """The entry point, not the helper: `partial_order` was written and left unwired."""

        class StubTuner(Tuner):
            """The real `Tuner`, over a graph shaped like the real one."""

            def __init__(self):
                super().__init__(name="stub")
                self._backend = FakeBackend()

            @property
            def backend(self):
                return self._backend

            @property
            def device(self):
                return None

            def routines(self):
                return [
                    StubRoutine("resonator_spectroscopy"),
                    StubRoutine(
                        "qubit_spectroscopy", depends_on=("resonator_spectroscopy",)
                    ),
                    StubRoutine("rabi", depends_on=("qubit_spectroscopy",)),
                    StubRoutine("cz_chevron", depends_on=("rabi",), targets="edges"),
                ]

        report = StubTuner().recalibrate(
            ["q0"],
            # q2 is targeted because ``q1_q2`` is: an edge is calibrated *through* its two
            # qubits, so a config naming one without the other is refused outright — see
            # `CalibrationConfig.validate_targets`.
            _config(target_qubits=["q0", "q1", "q2"], target_edges=["q0_q1", "q1_q2"]),
        )

        ran = {(r.routine_name, r.target) for r in report.routine_results}
        assert report.status == "success"
        # q0 drifted. `q0_q1` touches it, so it comes in — and q1 with it, because an edge
        # brings both its ends. `q1_q2` touches neither and stays out, which is the
        # narrowing this test is about.
        assert {target for _, target in ran} == {"q0", "q1", "q0_q1"}
        # And the readout bring-up is not re-run for a drift the gates can fix.
        assert {name for name, _ in ran} == {"qubit_spectroscopy", "rabi", "cz_chevron"}

    def test_the_real_graph_matches_the_rfc_dependency_table(self):
        dag = CalibrationDAG(all_routines(), _config())
        order = dag.execution_order()

        assert order[0] == "resonator_spectroscopy"
        assert order.index("rabi") < order.index("ramsey")
        assert order.index("ramsey") < order.index("drag")
        assert order.index("drag") < order.index("fine_amplitude")
        assert order.index("fine_amplitude") < order.index("rb")
        assert order.index("cz_chevron") < order.index("conditional_phase")
        assert order.index("conditional_phase") < order.index("interleaved_rb")

    def test_every_routine_name_is_unique(self):
        names = [r.name for r in all_routines()]
        assert len(names) == len(set(names)) == len(routine_names())


class TestTheWalk:
    """Walking the DAG end to end: a clean run, failures, timeouts, benchmarks, and the report's own backend name."""

    def test_a_clean_walk_reports_success_and_applies_each_routine(self):
        routines = [StubRoutine("a"), StubRoutine("b", depends_on=("a",))]
        report = CalibrationDAG(routines, _config()).run(
            device=None, backend=FakeBackend(), config=_config()
        )

        assert report.status == "success"
        assert [r.routine_name for r in report.routine_results] == ["a", "b"]
        assert routines[0].applied == [("q0", {"fidelity": 0.999, "value": 1.0})]

    def test_running_nothing_is_a_failure_not_a_success(self):
        """A run that measured nothing must never report success."""
        config = _config(routines={"a": RoutineConfig(enabled=False)})
        report = CalibrationDAG([StubRoutine("a")], config).run(
            device=None, backend=FakeBackend(), config=config
        )

        assert report.status == "failed"
        assert "every routine is disabled" in report.errors[0]

    def test_no_target_qubits_is_a_failure(self):
        config = CalibrationConfig(target_qubits=[])
        report = CalibrationDAG([StubRoutine("a")], config).run(
            device=None, backend=FakeBackend(), config=config
        )
        assert report.status == "failed"
        assert "no target_qubits" in report.errors[0]

    def test_edge_routines_with_no_edges_configured_leave_nothing_run(self):
        config = _config(target_edges=[])
        report = CalibrationDAG([StubRoutine("cz", targets="edges")], config).run(
            device=None, backend=FakeBackend(), config=config
        )
        assert report.status == "failed"
        assert "target edges" in report.errors[0]

    def test_one_failing_routine_does_not_abandon_the_rest(self):
        routines = [FailingRoutine("a"), StubRoutine("b")]
        report = CalibrationDAG(routines, _config()).run(
            device=None, backend=FakeBackend(), config=_config()
        )

        assert report.status == "partial_failure"
        assert [r.routine_name for r in report.routine_results] == ["b"]
        assert "a[q0]: could not fit" in report.errors[0]

    def test_every_routine_failing_is_a_failure(self):
        report = CalibrationDAG([FailingRoutine("a")], _config()).run(
            device=None, backend=FakeBackend(), config=_config()
        )
        assert report.status == "failed"

    def test_a_benchmark_is_recorded_as_a_benchmark(self):
        report = CalibrationDAG([StubRoutine("rb", benchmark=True)], _config()).run(
            device=None, backend=FakeBackend(), config=_config()
        )
        assert [b.protocol for b in report.benchmarks] == ["rb"]
        assert report.fidelities() == {"q0": 0.999}

    def test_a_non_benchmark_records_no_fidelity(self):
        """T1 writes nothing either; only a benchmark has a fidelity to compare."""
        report = CalibrationDAG([StubRoutine("t1")], _config()).run(
            device=None, backend=FakeBackend(), config=_config()
        )
        assert report.benchmarks == []
        assert report.fidelities() == {}

    def test_the_only_subset_runs_just_those_routines(self):
        routines = [StubRoutine("a"), StubRoutine("b")]
        report = CalibrationDAG(routines, _config()).run(
            device=None, backend=FakeBackend(), config=_config(), only=["b"]
        )
        assert [r.routine_name for r in report.routine_results] == ["b"]

    def test_a_routine_over_its_timeout_is_failed(self):
        config = _config()
        config.routine_timeout_s = -1.0  # anything measurable exceeds it
        report = CalibrationDAG([StubRoutine("a")], config).run(
            device=None, backend=FakeBackend(), config=config
        )
        assert report.status == "failed"
        assert "routine_timeout_s" in report.errors[0]

    def test_the_timeout_reaches_the_backend_rather_than_only_the_stopwatch(self):
        """Checked after the fact it can report a hang; passed down it can end one."""
        config = _config()
        config.routine_timeout_s = 1800.0
        backend = FakeBackend()
        CalibrationDAG([StubRoutine("a")], config).run(
            device=None, backend=backend, config=config
        )
        assert backend.timeouts == [1800.0]

    def test_a_self_measuring_routine_gets_the_configured_ceiling_too(self):
        """Its loop makes its own `backend.run` calls, which the DAG never sees."""
        config = _config()
        config.routine_timeout_s = 1800.0
        backend = FakeBackend()
        CalibrationDAG([SelfMeasuringRoutine("a")], config).run(
            device=None, backend=backend, config=config
        )
        assert backend.timeouts == [1800.0]

    def test_the_report_names_its_backend(self):
        report = CalibrationDAG([StubRoutine("a")], _config()).run(
            device=None, backend=FakeBackend(), config=_config()
        )
        assert report.backend == "fake"


class TestProgressReporting:
    """What a walk says about itself while it runs.

    A full DAG is hours in which the journal is the only view of where it has got
    to, so the position, the target and the outcome are all load-bearing.
    """

    def test_each_routine_reports_its_position_target_and_outcome(self, caplog):
        routines = [StubRoutine("a"), FailingRoutine("b", depends_on=("a",))]
        with caplog.at_level(logging.INFO, logger="qpi_driver.tuners.base.dag"):
            CalibrationDAG(routines, _config()).run(
                device=None, backend=FakeBackend(), config=_config()
            )

        messages = [record.getMessage() for record in caplog.records]
        assert (
            "starting full calibration: 2 routine(s) over 1 qubit(s), 0 edge(s)"
            in messages
        )
        assert "[1/2] a running on q0" in messages
        assert any(m.startswith("[1/2] a q0 ok in ") for m in messages)
        assert any(m.startswith("[2/2] b q0 FAILED in ") for m in messages)
        assert any(
            m.startswith("full calibration partial_failure in ")
            and m.endswith(": 1 succeeded, 1 failed, 0 skipped")
            for m in messages
        )

    def test_a_routine_with_no_targets_says_it_was_skipped(self, caplog):
        config = _config(target_edges=[])
        with caplog.at_level(logging.INFO, logger="qpi_driver.tuners.base.dag"):
            CalibrationDAG([StubRoutine("cz", targets="edges")], config).run(
                device=None, backend=FakeBackend(), config=config
            )

        assert "[1/1] cz skipped: no edges it applies to" in [
            record.getMessage() for record in caplog.records
        ]

    def test_a_check_reports_its_verdict_as_it_is_measured(self, caplog):
        """`diagnose` runs schedules before the walk starts; those need a voice too."""
        routines = [CheckableRoutine("a", verdict=False)]
        with caplog.at_level(logging.INFO, logger="qpi_driver.tuners.base.dag"):
            CalibrationDAG(routines, _config()).check(
                "a", ["q0"], None, FakeBackend(), _config()
            )

        assert any(
            m.startswith("check a on q0: FAILED (margin ")
            for m in (record.getMessage() for record in caplog.records)
        )


class TestTheProgressSink:
    """What a walk tells whoever is watching it, and what it does when they break."""

    def test_every_target_reports_its_position_and_the_running_totals(self):
        updates: list[dict] = []
        routines = [StubRoutine("a"), FailingRoutine("b", depends_on=("a",))]
        config = _config(target_qubits=["q0", "q1"])

        CalibrationDAG(routines, config).run(
            device=None,
            backend=FakeBackend(),
            config=config,
            on_progress=updates.append,
        )

        # The plan is the first thing said, and the only one that is not a position.
        assert "plan" in updates.pop(0)
        assert [(u["step"], u["routine"], u["target"]) for u in updates] == [
            (1, "a", "q0"),
            (1, "a", "q1"),
            (2, "b", "q0"),
            (2, "b", "q1"),
        ]
        assert all(u["total"] == 2 for u in updates)
        # The counts are the report's own as it stands, so a watcher sees them climb.
        assert [(u["succeeded"], u["failed"]) for u in updates] == [
            (1, 0),
            (2, 0),
            (2, 1),
            (2, 2),
        ]

    def test_a_sink_that_raises_does_not_end_the_walk(self):
        """A calibration outlives whoever is watching it."""

        def explode(update):
            raise RuntimeError("the dashboard went away")

        report = CalibrationDAG([StubRoutine("a")], _config()).run(
            device=None,
            backend=FakeBackend(),
            config=_config(),
            on_progress=explode,
        )

        assert report.status == "success"


class TestThePlan:
    """The graph a run publishes for the dashboard to draw (RFC 0006 §5.1)."""

    def test_a_node_carries_what_the_dashboard_draws_it_from(self):
        routines = [
            StubRoutine("a"),
            CheckableRoutine("b", depends_on=("a",), verdict=True),
        ]
        routines[1].updates = ("rxy.amp180",)
        config = _config(target_qubits=["q0", "q1"])
        dag = CalibrationDAG(routines, config)

        nodes = dag.plan(dag.execution_order(), config)["nodes"]

        assert nodes[1] == {
            "name": "b",
            "depends_on": ["a"],
            "targets": ["q0", "q1"],
            "kind": "qubits",
            "planned": True,
            "is_benchmark": False,
            "has_check": True,
            "updates": ["rxy.amp180"],
        }

    def test_an_excluded_routine_is_still_sent_marked_unplanned(self):
        """Which parts of the graph a partial run is *not* touching is most of the value."""
        routines = [StubRoutine("a"), StubRoutine("b", depends_on=("a",))]
        config = _config()
        dag = CalibrationDAG(routines, config)

        nodes = dag.plan(["a"], config)["nodes"]

        assert [(n["name"], n["planned"]) for n in nodes] == [("a", True), ("b", False)]

    def test_a_declined_target_is_absent_from_the_node_it_declined(self):
        class Fussy(StubRoutine):
            def applies_to(self, device, target):
                return target != "q1"

        config = _config(target_qubits=["q0", "q1", "q2"])
        dag = CalibrationDAG([Fussy("a")], config)

        assert dag.plan(["a"], config, device=object())["nodes"][0]["targets"] == [
            "q0",
            "q2",
        ]

    def test_it_arrives_before_the_first_progress_event(self):
        updates: list[dict] = []
        config = _config()

        CalibrationDAG([StubRoutine("a")], config).run(
            device=None,
            backend=FakeBackend(),
            config=config,
            on_progress=updates.append,
        )

        assert "plan" in updates[0]
        assert updates[0]["target_qubits"] == ["q0"]
        assert not any("plan" in u for u in updates[1:])

    def test_a_run_with_nothing_to_walk_publishes_no_plan(self):
        """There is no walk to draw, and the report says why."""
        updates: list[dict] = []
        config = _config(target_qubits=[])

        CalibrationDAG([StubRoutine("a")], config).run(
            device=None,
            backend=FakeBackend(),
            config=config,
            on_progress=updates.append,
        )

        assert updates == []

    def test_the_real_graph_is_thirty_three_nodes_with_one_root(self):
        config = _config()
        dag = CalibrationDAG(all_routines(), config)
        order = dag.execution_order()

        nodes = dag.plan(order, config)["nodes"]

        assert len(nodes) == len(ROUTINE_CLASSES)
        assert [n["name"] for n in nodes if not n["depends_on"]] == [
            "resonator_spectroscopy"
        ]
        assert all(n["planned"] for n in nodes)
        assert {n["name"] for n in nodes if n["is_benchmark"]} == {
            "readout_fidelity",
            "rb",
            "interleaved_rb",
            "allxy_check",
        }
        assert {n["name"] for n in nodes if n["kind"] == "edges"} == {
            "coupler_anticrossing",
            "cz_spectroscopy",
            "cz_parametrization",
            "cz_chevron",
            "conditional_phase",
            "interleaved_rb",
        }


class TestTheFitOnAResult:
    """Where a fit summary goes, and what the cap does with too many (RFC 0006 §7)."""

    def test_it_lands_on_the_result_not_in_the_parameters(self):
        """`parameters` is what gets written to a device; a sweep is not a parameter."""

        class Fitting(StubRoutine):
            def analyse(self, dataset, target, device, config):
                return {"amp180": 0.2, "fit": {"x": [1.0], "measured": [2.0]}}

        routine = Fitting("a")
        config = _config()
        report = CalibrationDAG([routine], config).run(
            device=None, backend=FakeBackend(), config=config
        )

        result = report.routine_results[0]
        assert result.fit == {"x": [1.0], "measured": [2.0]}
        assert result.parameters == {"amp180": 0.2}
        # And `apply` never sees it either, so it cannot reach the device file.
        assert routine.applied == [("q0", {"amp180": 0.2})]

    def test_a_benchmark_does_not_carry_it_into_raw_data_as_well(self):
        class FittingBenchmark(StubRoutine):
            def analyse(self, dataset, target, device, config):
                return {"fidelity": 0.999, "depths": [1, 2], "fit": {"x": [1.0]}}

        config = _config()
        report = CalibrationDAG([FittingBenchmark("a", benchmark=True)], config).run(
            device=None, backend=FakeBackend(), config=config
        )

        assert report.routine_results[0].fit == {"x": [1.0]}
        assert "fit" not in report.benchmarks[0].raw_data

    def test_a_routine_with_no_summary_simply_has_none(self):
        config = _config()
        report = CalibrationDAG([StubRoutine("a")], config).run(
            device=None, backend=FakeBackend(), config=config
        )
        assert report.routine_results[0].fit is None
        assert "fit" not in report.to_event_payload()["routine_results"][0]

    def test_a_report_over_the_cap_still_saves_without_its_traces(self):
        """A report that will not save is worse than a report with no chart in it."""
        big = {"x": [float(i) for i in range(200)], "measured": [0.5] * 200}
        report = CalibrationReport(
            timestamp="t", duration_s=1.0, mode="full", backend="stub"
        )
        # Enough of them to clear the two-megabyte cap several times over.
        for i in range(1000):
            report.add_routine(
                RoutineResult(
                    routine_name=f"r{i}",
                    target="q0",
                    parameters={"value": 1.0},
                    timestamp="t",
                    duration_s=1.0,
                    fit=dict(big),
                )
            )

        payload = report.to_event_payload()

        assert all(r["fit"] == {"dropped": True} for r in payload["routine_results"])
        assert len(json.dumps(payload)) < MAX_FIT_PAYLOAD_BYTES
        # The parameters are the record of what the chip was, and they are untouched.
        assert payload["routine_results"][0]["parameters"] == {"value": 1.0}

    def test_a_report_inside_the_cap_keeps_every_trace(self):
        report = CalibrationReport(
            timestamp="t", duration_s=1.0, mode="full", backend="stub"
        )
        report.add_routine(
            RoutineResult(
                routine_name="rabi",
                target="q0",
                parameters={},
                timestamp="t",
                duration_s=1.0,
                fit={"x": [1.0, 2.0], "measured": [1.0, 0.5]},
            )
        )
        assert report.to_event_payload()["routine_results"][0]["fit"] == {
            "x": [1.0, 2.0],
            "measured": [1.0, 0.5],
        }


class TestHumanDuration:
    def test_it_scales_from_seconds_to_hours(self):
        assert _human_duration(12.44) == "12.4s"
        assert _human_duration(59.9) == "59.9s"
        assert _human_duration(60) == "1m00s"
        assert _human_duration(192) == "3m12s"
        assert _human_duration(8040) == "2h14m"


class TestSetpointHelpers:
    """Small standalone helpers: setpoints, a routine's repr, the backend's idle, and signal_of."""

    def test_linear_setpoints_span_the_range_inclusively(self):
        assert linear_setpoints(0.0, 1.0, 5) == [0.0, 0.25, 0.5, 0.75, 1.0]
        assert linear_setpoints(3.0, 9.0, 1) == [3.0]

    def test_setpoints_fall_back_to_the_default(self):
        assert setpoints_of(RoutineConfig(), "amps", [1, 2]) == [1, 2]

    def test_an_empty_sweep_is_refused(self):
        """An empty sweep compiles to an empty schedule and measures nothing."""
        with pytest.raises(RoutineError, match="non-empty list"):
            setpoints_of(RoutineConfig(params={"amps": []}), "amps", [1])
        with pytest.raises(RoutineError, match="non-empty list"):
            setpoints_of(RoutineConfig(params={"amps": 5}), "amps", [1])

    def test_a_routine_reprs_as_its_name(self):
        assert repr(StubRoutine("rabi")) == "<StubRoutine 'rabi'>"

    def test_the_backend_idle_helper_appends_an_idle(self):
        class Recording(FakeBackend):
            IdlePulse = staticmethod(lambda duration: ("idle", duration))

        schedule = type(
            "S", (), {"added": [], "add": lambda self, op: self.added.append(op)}
        )()
        Recording().idle(schedule, 1e-6)
        assert schedule.added == [("idle", 1e-6)]

    def test_signal_of_handles_a_bare_array(self):
        from qpi_driver.tuners.fitting import signal_of

        assert list(signal_of(np.array([1.0, 2.0]))) == [1.0, 2.0]


class Producer(StubRoutine):
    """A routine that writes named parameters, so a ledger has something to record."""

    def __init__(self, name, depends_on=(), updates=(), reads=(), targets="qubits"):
        super().__init__(name, depends_on=depends_on, targets=targets)
        self.updates = updates
        self.reads = reads


class FailingProducer(Producer):
    def analyse(self, dataset, target, device, config):
        raise RoutineError("could not fit")


class TestANodeWhoseInputWasNeverProducedIsSkipped:
    """RFC 0007 §11: block on an unsatisfied *parameter*, not on a failed neighbour.

    The failure this exists for: on an August 2026 chip `qubit_spectroscopy` failed and
    six nodes behind it measured a qubit still in |0>, each reporting a confident number
    fitted from its noise. Six failures with six different-looking causes, none naming
    the one that mattered — and before that run's guards existed, those six wrote the
    noise to the device file.
    """

    def _run(self, routines):
        return CalibrationDAG(routines, _config()).run(
            device=None, backend=FakeBackend(), config=_config()
        )

    def test_a_reader_is_skipped_when_its_parameter_was_not_produced(self):
        routines = [
            FailingProducer("root", updates=("clock_freqs.f01",)),
            Producer("reader", depends_on=("root",), reads=("clock_freqs.f01",)),
        ]
        report = self._run(routines)

        assert [r.routine_name for r in report.routine_results] == []
        # Skipped, not failed: one error for the node that actually broke.
        assert len(report.errors) == 1 and "root" in report.errors[0]
        assert any(
            "reader[q0]: skipped" in note and "clock_freqs.f01" in note
            for note in report.notes
        ), report.notes

    def test_a_failed_refiner_blocks_nothing(self):
        """Seven parameters have two writers. The second failing leaves the first's.

        `ramsey` refines the `clock_freqs.f01` that `qubit_spectroscopy` produced, so a
        failed `ramsey` must not skip the graph behind it — node-level propagation would
        have skipped five nodes here for nothing.
        """
        routines = [
            Producer("producer", updates=("clock_freqs.f01",)),
            FailingProducer(
                "refiner",
                depends_on=("producer",),
                updates=("clock_freqs.f01",),
                reads=("clock_freqs.f01",),
            ),
            Producer("reader", depends_on=("refiner",), reads=("clock_freqs.f01",)),
        ]
        report = self._run(routines)

        assert [r.routine_name for r in report.routine_results] == [
            "producer",
            "reader",
        ]
        assert not [n for n in report.notes if "skipped" in n]

    def test_a_disabled_producer_does_not_block_its_readers(self):
        """It never ran, so it never failed. An operator may switch a node off.

        `time_of_flight` is disabled on the August 2026 chip and `measure.acq_delay`
        stays at the value its config carries; blocking here would take the whole graph
        beneath it down.
        """
        config = _config(routines={"producer": RoutineConfig(enabled=False)})
        routines = [
            Producer("producer", updates=("measure.acq_delay",)),
            Producer("reader", depends_on=("producer",), reads=("measure.acq_delay",)),
        ]
        report = CalibrationDAG(routines, config).run(
            device=None, backend=FakeBackend(), config=config
        )

        assert [r.routine_name for r in report.routine_results] == ["reader"]
        assert report.status == "success"

    def test_a_reader_of_a_parameter_no_node_produces_still_runs(self):
        """`measure.integration_time` and `r12.ef_duration` have no producer at all.

        They come from the config, so "nothing produced it in this walk" must not mean
        "it is missing" — otherwise seven EF nodes would refuse on every chip.
        """
        routines = [Producer("reader", reads=("r12.ef_duration",))]
        report = self._run(routines)

        assert [r.routine_name for r in report.routine_results] == ["reader"]

    def test_a_skip_names_the_failure_that_started_it_not_its_neighbour(self):
        """A chain of skips blames the root, as `diagnose` blames the deepest ancestor."""
        routines = [
            FailingProducer("root", updates=("clock_freqs.f01",)),
            Producer(
                "middle",
                depends_on=("root",),
                reads=("clock_freqs.f01",),
                updates=("rxy.amp180",),
            ),
            Producer("leaf", depends_on=("middle",), reads=("rxy.amp180",)),
        ]
        report = self._run(routines)

        leaf = next(n for n in report.notes if n.startswith("leaf[q0]"))
        assert "root failed" in leaf, leaf
        assert "middle" not in leaf, leaf

    def test_a_failure_on_one_qubit_does_not_skip_another(self):
        """The ledger is keyed on the target too — q1 is a different chip site."""

        class FailsOnQ0(Producer):
            def analyse(self, dataset, target, device, config):
                if target == "q0":
                    raise RoutineError("could not fit")
                return {"value": 1.0}

        config = _config(target_qubits=["q0", "q1"])
        routines = [
            FailsOnQ0("root", updates=("clock_freqs.f01",)),
            Producer("reader", depends_on=("root",), reads=("clock_freqs.f01",)),
        ]
        report = CalibrationDAG(routines, config).run(
            device=None, backend=FakeBackend(), config=config
        )

        ran = [(r.routine_name, r.target) for r in report.routine_results]
        assert ("reader", "q1") in ran
        assert ("reader", "q0") not in ran


class TestAWindowTooShortIsWidenedRatherThanFailed:
    """RFC 0007 §6.1: a guard that knows which axis was wrong is an instruction.

    "the decay was never seen in this window" means lengthen the delays. Before this it
    meant the node failed and an operator read the prose.
    """

    class Coherence(StubRoutine):
        """Refuses until its delays reach *needs*, then reports."""

        def __init__(self, name, needs):
            super().__init__(name)
            self.needs = needs
            self.attempts: list[float] = []

        def build_schedule(self, target, device, config, backend):
            self._delays = setpoints_of(
                config, "delays", linear_setpoints(0.0, 1e-5, 41)
            )
            return backend.new_schedule(self.name)

        def analyse(self, dataset, target, device, config):
            extent = max(self._delays)
            self.attempts.append(extent)
            if extent < self.needs:
                raise OutOfRange(
                    "the decay was never seen in this window", axis="delays"
                )
            return {"t1": extent / 3.0}

        def measure(self, target, device, config, backend, bias=None, timeout_s=300.0):
            return self.escalating(target, device, config, backend, timeout_s)

    def _run(self, routines, config=None):
        config = config or _config()
        return CalibrationDAG(routines, config).run(
            device=None, backend=FakeBackend(), config=config
        )

    def test_it_widens_until_the_decay_fits_in_the_window(self):
        node = self.Coherence("t1", needs=1.5e-4)
        report = self._run([node])

        assert report.status == "success"
        # 10 us, then 40, then 160: fourfold each time, and it stops as soon as it fits.
        assert node.attempts == pytest.approx([1e-5, 4e-5, 1.6e-4])

    def test_a_chip_with_no_decay_still_fails_and_says_why(self):
        node = self.Coherence("t1", needs=1.0)
        report = self._run([node])

        assert report.status == "failed"
        assert len(node.attempts) == node.MAX_ESCALATIONS + 1
        assert "never seen in this window" in report.errors[0]

    def test_an_operator_who_named_the_delays_is_not_overruled(self):
        """Their setpoints are a statement about their chip; widening would overrule it."""
        node = self.Coherence("t1", needs=1.5e-4)
        config = _config(routines={"t1": RoutineConfig(params={"delays": [0.0, 1e-5]})})
        report = self._run([node], config)

        assert report.status == "failed"
        assert node.attempts == pytest.approx([1e-5])


class TestWhatTheWalkSaysAboutPriors:
    """RFC 0008 phase 4: the pre-walk check RFC 0007 §11 withdrew, now decidable.

    §11 wanted to report a read parameter whose only producer is switched off, and could
    not: two paths have no producer anywhere in the graph and are hand-supplied on every
    chip, so the rule fired on them every run. Provenance splits the three cases, and only
    the middle one is worth saying anything about.
    """

    def _run(self, routines, config=None, provenance=None):
        config = config or _config()
        return CalibrationDAG(routines, config).run(
            device=None,
            backend=FakeBackend(),
            config=config,
            provenance=provenance,
        )

    def test_it_reports_a_parameter_whose_producer_is_switched_off(self):
        config = _config(routines={"producer": RoutineConfig(enabled=False)})
        routines = [
            Producer("producer", updates=("clock_freqs.f01",)),
            Producer("reader", depends_on=("producer",), reads=("clock_freqs.f01",)),
        ]

        report = self._run(routines, config)

        note = next(n for n in report.notes if "clock_freqs.f01" in n)
        assert "nothing has ever measured" in note
        # And it names the way out, which is the point of reporting rather than refusing.
        assert "producer" in note
        assert report.status == "success"

    def test_it_says_nothing_about_a_parameter_no_routine_produces(self):
        """The reason §11's version was withdrawn: this fires on every chip, forever."""
        routines = [Producer("reader", reads=("r12.ef_duration",))]

        report = self._run(routines)

        assert report.notes == []

    def test_a_prior_this_run_will_measure_is_reported_as_ordinary(self):
        routines = [
            Producer(
                "producer", updates=("clock_freqs.f01",), reads=("clock_freqs.f01",)
            )
        ]

        report = self._run(routines)

        assert report.notes == ["q0: clock_freqs.f01 not measured before this run"]

    def test_an_earlier_run_having_measured_it_silences_the_note(self):
        config = _config(routines={"producer": RoutineConfig(enabled=False)})
        routines = [
            Producer("producer", updates=("clock_freqs.f01",)),
            Producer("reader", depends_on=("producer",), reads=("clock_freqs.f01",)),
        ]
        store = ProvenanceStore()
        store.record(
            "q0",
            "clock_freqs.f01",
            Provenance(routine="producer", at="2026-08-01T00:00:00Z"),
        )

        report = self._run(routines, config, provenance=store)

        assert report.notes == []

    def test_a_result_carries_the_priors_it_was_derived_from(self):
        routines = [
            Producer("producer", updates=("clock_freqs.f01",)),
            Producer(
                "reader",
                depends_on=("producer",),
                reads=("clock_freqs.f01", "r12.ef_duration"),
            ),
        ]

        report = self._run(routines)

        by_name = {r.routine_name: r for r in report.routine_results}
        # `producer` measured f01 before `reader` ran, so only the hand-supplied path is
        # left — a prior with no producer is still a prior, whatever the notes say of it.
        assert by_name["reader"].priors == ("r12.ef_duration",)
        assert by_name["producer"].priors == ()

    def test_a_skipped_node_says_how_old_what_it_left_standing_is(self):
        """RFC 0007 §11 keeps a skipped node's stale values; this says how stale."""
        routines = [
            FailingProducer("root", updates=("clock_freqs.f01",)),
            Producer(
                "reader",
                depends_on=("root",),
                reads=("clock_freqs.f01",),
                updates=("rxy.amp180",),
            ),
        ]
        store = ProvenanceStore()
        store.record(
            "q0", "rxy.amp180", Provenance(routine="rabi", at="2026-08-01T09:00:00Z")
        )

        report = self._run(routines, provenance=store)

        note = next(n for n in report.notes if "not reconfirmed" in n)
        assert (
            "rxy.amp180 still holds what rabi measured at 2026-08-01T09:00:00Z" in note
        )

    def test_a_skipped_node_that_never_measured_anything_says_nothing_extra(self):
        """No provenance means no staleness to report — the prior notes already said it."""
        routines = [
            FailingProducer("root", updates=("clock_freqs.f01",)),
            Producer(
                "reader",
                depends_on=("root",),
                reads=("clock_freqs.f01",),
                updates=("rxy.amp180",),
            ),
        ]

        report = self._run(routines)

        assert not [n for n in report.notes if "not reconfirmed" in n]

    def test_no_sidecar_leaves_the_walk_exactly_as_it_was(self):
        """Every parameter a prior, and nothing blocked for it — RFC 0008 §3."""
        routines = [
            Producer("producer", updates=("clock_freqs.f01",)),
            Producer("reader", depends_on=("producer",), reads=("clock_freqs.f01",)),
        ]

        report = self._run(routines, provenance=None)

        assert report.status == "success"
        assert [r.routine_name for r in report.routine_results] == [
            "producer",
            "reader",
        ]
