"""The routine graph and the walk over it (RFC 0004 §5).

These run with stub routines and a fake backend: the ordering, the failure
handling and the refusal to report success having done nothing are all
properties of the DAG, not of any scheduler.
"""

from typing import Any

import numpy as np
import pytest
import xarray as xr
from qpi_driver.tuners.base import RECALIBRATION_ROOTS, Tuner
from qpi_driver.tuners.base.backend import SchedulerBackend
from qpi_driver.tuners.base.config import CalibrationConfig, RoutineConfig
from qpi_driver.tuners.base.dag import CalibrationDAG
from qpi_driver.tuners.base.routines import (
    CalibrationRoutine,
    CheckOutcome,
    RoutineError,
    linear_setpoints,
    setpoints_of,
)
from qpi_driver.tuners.routines import all_routines, routine_names


class FakeBackend(SchedulerBackend):
    """Records the schedules it was given and hands back a fixed dataset."""

    name = "fake"

    def __init__(self, dataset: xr.Dataset | None = None) -> None:
        self.ran: list[Any] = []
        self._dataset = (
            dataset if dataset is not None else xr.Dataset({"y": ("x", [1.0, 2.0])})
        )

    def new_schedule(self, name: str, repetitions: int = 1) -> Any:
        return {"name": name, "ops": []}

    def run(self, schedule: Any) -> xr.Dataset:
        self.ran.append(schedule)
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


# --- ordering -----------------------------------------------------------------


def test_execution_order_is_topological():
    dag = CalibrationDAG(
        [
            StubRoutine("c", depends_on=("b",)),
            StubRoutine("a"),
            StubRoutine("b", depends_on=("a",)),
        ],
        _config(),
    )
    assert dag.execution_order() == ["a", "b", "c"]


def test_a_cycle_is_reported():
    dag = CalibrationDAG(
        [StubRoutine("a", depends_on=("b",)), StubRoutine("b", depends_on=("a",))],
        _config(),
    )
    with pytest.raises(ValueError, match="cycle detected"):
        dag.execution_order()


def test_a_dependency_on_an_unknown_routine_is_reported():
    """A typo in depends_on would silently drop an edge and reorder the walk."""
    dag = CalibrationDAG([StubRoutine("a", depends_on=("ghost",))], _config())
    with pytest.raises(ValueError, match="depends on unknown routine"):
        dag.execution_order()


def test_a_disabled_routine_is_dropped_but_its_dependents_keep_their_order():
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


def test_partial_order_includes_everything_downstream():
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


def test_a_partial_recalibration_skips_the_readout_bring_up():
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


# --- diagnose (RFC 0005 §8) ----------------------------------------------------
#
# Graph logic, so it belongs here: a fabricated graph with scripted check outcomes
# and no simulator. What is asserted is *which* nodes get recalibrated, because
# that is the entire value of having checks — the alternative is re-running from
# the root and hoping.


def test_with_no_checks_diagnose_reproduces_the_old_behaviour():
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


def test_a_node_whose_check_passes_is_not_recalibrated():
    """The cheapest outcome, and one RFC 0004 could not reach at all."""
    routines = [
        CheckableRoutine("a", verdict=True),
        CheckableRoutine("b", depends_on=("a",), verdict=True),
        CheckableRoutine("c", depends_on=("b",), verdict=True),
    ]
    order, notes = _diagnose(routines, ["b"])
    assert order == []
    assert any("nothing to recalibrate" in note for note in notes)


def test_blame_moves_up_to_the_failing_dependency():
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


def test_blame_stops_where_the_dependency_is_verified_good():
    """`b` is out of spec and `a` is fine, so the fault is `b`'s own."""
    routines = [
        CheckableRoutine("a", verdict=True),
        CheckableRoutine("b", depends_on=("a",), verdict=False),
        CheckableRoutine("c", depends_on=("b",), verdict=False),
    ]
    order, _notes = _diagnose(routines, ["c"])
    assert order == ["b", "c"]
    assert "a" not in order


def test_an_unknown_dependency_does_not_attract_blame():
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


def test_a_check_that_raises_leaves_the_node_unknown():
    """A broken check is not a drifting parameter, and must not read as one."""
    routines = [
        UnevaluableCheck("a"),
        CheckableRoutine("b", depends_on=("a",), verdict=False),
    ]
    order, _notes = _diagnose(routines, ["b"])
    assert order == ["b"], "an unevaluable check on `a` should not blame `a`"


def test_diagnose_checks_each_routine_once_however_many_paths_reach_it():
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


def test_the_worst_target_decides():
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


def test_the_real_graph_reports_a_readout_drift_instead_of_hiding_it():
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


def test_recalibrate_narrows_both_the_targets_and_the_routines():
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
        ["q0"], _config(target_qubits=["q0", "q1"], target_edges=["q0_q1", "q1_q2"])
    )

    ran = {(r.routine_name, r.target) for r in report.routine_results}
    assert report.status == "success"
    # q1 is not the drifted qubit, and q1_q2 does not touch the one that is.
    assert {target for _, target in ran} == {"q0", "q0_q1"}
    # And the readout bring-up is not re-run for a drift the gates can fix.
    assert {name for name, _ in ran} == {"qubit_spectroscopy", "rabi", "cz_chevron"}


def test_the_real_graph_matches_the_rfc_dependency_table():
    dag = CalibrationDAG(all_routines(), _config())
    order = dag.execution_order()

    assert order[0] == "resonator_spectroscopy"
    assert order.index("rabi") < order.index("ramsey")
    assert order.index("ramsey") < order.index("drag")
    assert order.index("drag") < order.index("fine_amplitude")
    assert order.index("fine_amplitude") < order.index("rb")
    assert order.index("cz_chevron") < order.index("conditional_phase")
    assert order.index("conditional_phase") < order.index("interleaved_rb")


def test_every_routine_name_is_unique():
    names = [r.name for r in all_routines()]
    assert len(names) == len(set(names)) == len(routine_names())


# --- the walk -----------------------------------------------------------------


def test_a_clean_walk_reports_success_and_applies_each_routine():
    routines = [StubRoutine("a"), StubRoutine("b", depends_on=("a",))]
    report = CalibrationDAG(routines, _config()).run(
        device=None, backend=FakeBackend(), config=_config()
    )

    assert report.status == "success"
    assert [r.routine_name for r in report.routine_results] == ["a", "b"]
    assert routines[0].applied == [("q0", {"fidelity": 0.999, "value": 1.0})]


def test_running_nothing_is_a_failure_not_a_success():
    """A run that measured nothing must never report success."""
    config = _config(routines={"a": RoutineConfig(enabled=False)})
    report = CalibrationDAG([StubRoutine("a")], config).run(
        device=None, backend=FakeBackend(), config=config
    )

    assert report.status == "failed"
    assert "every routine is disabled" in report.errors[0]


def test_no_target_qubits_is_a_failure():
    config = CalibrationConfig(target_qubits=[])
    report = CalibrationDAG([StubRoutine("a")], config).run(
        device=None, backend=FakeBackend(), config=config
    )
    assert report.status == "failed"
    assert "no target_qubits" in report.errors[0]


def test_edge_routines_with_no_edges_configured_leave_nothing_run():
    config = _config(target_edges=[])
    report = CalibrationDAG([StubRoutine("cz", targets="edges")], config).run(
        device=None, backend=FakeBackend(), config=config
    )
    assert report.status == "failed"
    assert "target edges" in report.errors[0]


def test_one_failing_routine_does_not_abandon_the_rest():
    routines = [FailingRoutine("a"), StubRoutine("b")]
    report = CalibrationDAG(routines, _config()).run(
        device=None, backend=FakeBackend(), config=_config()
    )

    assert report.status == "partial_failure"
    assert [r.routine_name for r in report.routine_results] == ["b"]
    assert "a[q0]: could not fit" in report.errors[0]


def test_every_routine_failing_is_a_failure():
    report = CalibrationDAG([FailingRoutine("a")], _config()).run(
        device=None, backend=FakeBackend(), config=_config()
    )
    assert report.status == "failed"


def test_a_benchmark_is_recorded_as_a_benchmark():
    report = CalibrationDAG([StubRoutine("rb", benchmark=True)], _config()).run(
        device=None, backend=FakeBackend(), config=_config()
    )
    assert [b.protocol for b in report.benchmarks] == ["rb"]
    assert report.fidelities() == {"q0": 0.999}


def test_a_non_benchmark_records_no_fidelity():
    """T1 writes nothing either; only a benchmark has a fidelity to compare."""
    report = CalibrationDAG([StubRoutine("t1")], _config()).run(
        device=None, backend=FakeBackend(), config=_config()
    )
    assert report.benchmarks == []
    assert report.fidelities() == {}


def test_the_only_subset_runs_just_those_routines():
    routines = [StubRoutine("a"), StubRoutine("b")]
    report = CalibrationDAG(routines, _config()).run(
        device=None, backend=FakeBackend(), config=_config(), only=["b"]
    )
    assert [r.routine_name for r in report.routine_results] == ["b"]


def test_a_routine_over_its_timeout_is_failed():
    config = _config()
    config.routine_timeout_s = -1.0  # anything measurable exceeds it
    report = CalibrationDAG([StubRoutine("a")], config).run(
        device=None, backend=FakeBackend(), config=config
    )
    assert report.status == "failed"
    assert "routine_timeout_s" in report.errors[0]


def test_the_report_names_its_backend():
    report = CalibrationDAG([StubRoutine("a")], _config()).run(
        device=None, backend=FakeBackend(), config=_config()
    )
    assert report.backend == "fake"


# --- setpoint helpers ---------------------------------------------------------


def test_linear_setpoints_span_the_range_inclusively():
    assert linear_setpoints(0.0, 1.0, 5) == [0.0, 0.25, 0.5, 0.75, 1.0]
    assert linear_setpoints(3.0, 9.0, 1) == [3.0]


def test_setpoints_fall_back_to_the_default():
    assert setpoints_of(RoutineConfig(), "amps", [1, 2]) == [1, 2]


def test_an_empty_sweep_is_refused():
    """An empty sweep compiles to an empty schedule and measures nothing."""
    with pytest.raises(RoutineError, match="non-empty list"):
        setpoints_of(RoutineConfig(params={"amps": []}), "amps", [1])
    with pytest.raises(RoutineError, match="non-empty list"):
        setpoints_of(RoutineConfig(params={"amps": 5}), "amps", [1])


def test_a_routine_reprs_as_its_name():
    assert repr(StubRoutine("rabi")) == "<StubRoutine 'rabi'>"


def test_the_backend_idle_helper_appends_an_idle():
    class Recording(FakeBackend):
        IdlePulse = staticmethod(lambda duration: ("idle", duration))

    schedule = type(
        "S", (), {"added": [], "add": lambda self, op: self.added.append(op)}
    )()
    Recording().idle(schedule, 1e-6)
    assert schedule.added == [("idle", 1e-6)]


def test_signal_of_handles_a_bare_array():
    from qpi_driver.tuners.fitting import signal_of

    assert list(signal_of(np.array([1.0, 2.0]))) == [1.0, 2.0]
