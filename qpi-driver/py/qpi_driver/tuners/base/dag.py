"""The routine dependency graph, and the walk that runs it (RFC 0004 §5).

A full calibration is a topological walk of the enabled routines. Ordering comes
entirely from each routine's ``depends_on``; nothing else encodes the sequence.
"""

import logging
import time
from collections import defaultdict, deque
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from qpi_driver.tuners.base.backend import SchedulerBackend
from qpi_driver.tuners.base.config import CalibrationConfig
from qpi_driver.tuners.base.report import CalibrationReport, RoutineResult
from qpi_driver.tuners.base.routines import (
    CalibrationRoutine,
    CheckOutcome,
    RoutineError,
)

log = logging.getLogger(__name__)

#: Called once per routine-and-target as the walk proceeds, with the keys the
#: ``CalibrationProgress`` event carries — and once before any of them with a
#: ``plan`` key instead, which is what tells the two apart (RFC 0006 §5.1).
#: Reporting is best-effort — see :meth:`CalibrationDAG.run` — so a sink may
#: raise without ending a calibration.
ProgressSink = Callable[[dict[str, Any]], None]


def _worse_than(candidate: CheckOutcome, incumbent: CheckOutcome) -> bool:
    """Whether *candidate* is the more alarming of two check outcomes.

    A failure beats a pass whatever the margins say; between two of a kind, the
    larger margin is the worse one — furthest outside if both failed, closest to
    the limit if both passed.
    """
    if candidate.passed != incumbent.passed:
        return incumbent.passed
    return candidate.margin > incumbent.margin


class CalibrationDAG:
    """The enabled routines, ordered by their dependencies."""

    def __init__(
        self,
        routines: list[CalibrationRoutine],
        config: CalibrationConfig,
        bias: Any = None,
    ) -> None:
        self.routines = {r.name: r for r in routines}
        self.config = config
        #: Something that can park a coupler at a DC current, or ``None``. Handed to a
        #: routine that measures itself — see `CalibrationRoutine.measure`. The graph
        #: does not otherwise know the bias exists: it is not a pulse, and every other
        #: node's sweep is.
        self.bias = bias

    def execution_order(self) -> list[str]:
        """Enabled routine names in dependency order (Kahn's algorithm).

        A disabled routine is dropped from the result but still relayed through,
        so its dependents keep their relative order rather than being stranded.

        Raises:
            ValueError: if the dependencies contain a cycle, or if a routine
                depends on a name no routine answers to — a typo in
                ``depends_on`` would otherwise silently drop an edge and produce
                an order that looks fine and calibrates in the wrong sequence.
        """
        for name, routine in self.routines.items():
            unknown = set(routine.depends_on) - set(self.routines)
            if unknown:
                raise ValueError(
                    f"routine {name!r} depends on unknown routine(s): "
                    f"{', '.join(sorted(unknown))}"
                )

        in_degree = {name: 0 for name in self.routines}
        adjacency: dict[str, list[str]] = defaultdict(list)
        for name, routine in self.routines.items():
            for dependency in routine.depends_on:
                adjacency[dependency].append(name)
                in_degree[name] += 1

        queue = deque(sorted(name for name, deg in in_degree.items() if deg == 0))
        visited: list[str] = []
        while queue:
            node = queue.popleft()
            visited.append(node)
            for neighbour in adjacency[node]:
                in_degree[neighbour] -= 1
                if in_degree[neighbour] == 0:
                    queue.append(neighbour)

        if len(visited) < len(self.routines):
            stuck = sorted(set(self.routines) - set(visited))
            raise ValueError(
                f"cycle detected in calibration dependencies among: {', '.join(stuck)}"
            )

        return [name for name in visited if self.config.is_enabled(name)]

    def partial_order(self, targets: list[str]) -> list[str]:
        """*targets* and everything downstream of them, in dependency order.

        Recalibrating a routine invalidates whatever was calibrated from it, so
        a partial run is the requested routines plus their dependents — not the
        requested routines alone.
        """
        adjacency: dict[str, list[str]] = defaultdict(list)
        for name, routine in self.routines.items():
            for dependency in routine.depends_on:
                adjacency[dependency].append(name)

        affected: set[str] = set()
        queue = deque(targets)
        while queue:
            node = queue.popleft()
            if node in affected or node not in self.routines:
                continue
            affected.add(node)
            queue.extend(adjacency[node])

        return [name for name in self.execution_order() if name in affected]

    def check(
        self,
        name: str,
        targets: list[str],
        device: Any,
        backend: SchedulerBackend,
        config: CalibrationConfig,
    ) -> CheckOutcome | None:
        """Whether routine *name*'s parameters still hold over all of *targets*.

        ``None`` means *unknown* — the routine has no check, or the check could
        not be evaluated. Unknown is not failure, and the difference is the whole
        point: `diagnose` may not blame a node it has no evidence against, or a
        graph with no checks at all would recalibrate from the root every time.

        The worst target wins. A parameter that has drifted on one qubit of five
        is drift, and reporting the mean would hide it.
        """
        routine = self.routines.get(name)
        if routine is None or not routine.has_check:
            return None

        routine_config = config.get_routine(name)
        worst: CheckOutcome | None = None
        for target in targets:
            try:
                schedule = routine.build_check_schedule(
                    target, device, routine_config, backend
                )
                if schedule is None:
                    continue
                dataset = backend.run(schedule, timeout_s=config.routine_timeout_s)
                outcome = routine.analyse_check(dataset, target, device, routine_config)
            except Exception:  # noqa: BLE001 - an unevaluable check is not drift
                log.warning(
                    "check for %s on %s could not be evaluated; treating as unknown",
                    name,
                    target,
                    exc_info=True,
                )
                continue
            # Diagnosis runs schedules of its own before the walk begins, so without
            # this a partial recalibration is silent for however long that takes.
            log.info(
                "check %s on %s: %s (margin %.2f)",
                name,
                target,
                "passed" if outcome.passed else "FAILED",
                outcome.margin,
            )
            outcome.detail = f"{target}: {outcome.detail}"
            if worst is None or _worse_than(outcome, worst):
                worst = outcome
        return worst

    def diagnose(
        self,
        seeds: list[str],
        device: Any,
        backend: SchedulerBackend,
        config: CalibrationConfig,
    ) -> tuple[list[str], list[str]]:
        """Which routines to recalibrate, given *seeds* as the suspected ones.

        The recursion, following Kelly et al. (arXiv:1803.03226): a node whose
        check passes is verified and needs nothing. A node whose check fails is a
        candidate — but if one of its *dependencies* also fails, the dependency is
        the better suspect and blame moves up. Recalibrating a node whose input is
        wrong measures the wrong thing twice.

        Blame stops at a node whose failing dependencies are none, which is the
        shallowest node with evidence against it and no upstream excuse.

        Returns ``(order, notes)`` — the routines to run, in dependency order and
        including everything downstream of them, and human-readable notes on what
        the checks found, for the report.
        """
        cache: dict[str, CheckOutcome | None] = {}
        notes: list[str] = []

        def outcome_for(name: str) -> CheckOutcome | None:
            if name not in cache:
                targets = self._targets_for(name, config, device)
                cache[name] = (
                    self.check(name, targets, device, backend, config)
                    if targets
                    else None
                )
                result = cache[name]
                if result is not None:
                    notes.append(
                        f"{name}: {'passed' if result.passed else 'FAILED'} "
                        f"(margin {result.margin:.2f}) {result.detail}".strip()
                    )
            return cache[name]

        def failed(name: str) -> bool:
            result = outcome_for(name)
            return result is not None and not result.passed

        blamed: set[str] = set()

        def blame(name: str, seen: frozenset[str]) -> set[str]:
            if name in seen or name not in self.routines:
                return set()
            result = outcome_for(name)
            if result is not None and result.passed:
                return set()  # verified good
            upstream: set[str] = set()
            for dependency in self.routines[name].depends_on:
                if failed(dependency):
                    upstream |= blame(dependency, seen | {name})
            return upstream or {name}

        for seed in seeds:
            blamed |= blame(seed, frozenset())

        if not blamed:
            notes.append("every check passed: nothing to recalibrate")
            return [], notes
        return self.partial_order(sorted(blamed)), notes

    def plan(
        self, order: list[str], config: CalibrationConfig, device: Any = None
    ) -> dict[str, Any]:
        """The graph this run is about to walk, for the dashboard to draw (RFC 0006 §5.1).

        Only the driver can compute this: the shape the dashboard must draw is the
        *enabled subset* for this run, which depends on ``calibration.yml``, on the
        mode, and for a partial run on what `diagnose` blamed.

        Every routine is described, whether or not it is in *order* — a routine
        excluded by being disabled or outside a partial's subset is sent with
        ``planned: false``, because seeing which parts of the graph a partial run is
        *not* touching is most of the value of drawing it.

        Nodes come in walk order, the excluded ones after, so a drawing that orders
        a layer by this sequence matches the walk.
        """
        planned = set(order)
        return {
            "nodes": [
                {
                    "name": name,
                    "depends_on": list(self.routines[name].depends_on),
                    "targets": self._targets_for(name, config, device),
                    "kind": self.routines[name].targets,
                    "planned": name in planned,
                    "is_benchmark": self.routines[name].is_benchmark,
                    "has_check": self.routines[name].has_check,
                    "updates": list(self.routines[name].updates),
                }
                for name in order
                + [name for name in self.routines if name not in planned]
            ]
        }

    def _targets_for(
        self, name: str, config: CalibrationConfig, device: Any = None
    ) -> list[str]:
        routine = self.routines.get(name)
        if routine is None:
            return []
        targets = (
            config.target_qubits if routine.targets == "qubits" else config.target_edges
        )
        if device is None:
            return list(targets)
        return [target for target in targets if _applies(routine, device, target, name)]

    def run(
        self,
        device: Any,
        backend: SchedulerBackend,
        config: CalibrationConfig,
        mode: str = "full",
        only: list[str] | None = None,
        on_progress: ProgressSink | None = None,
    ) -> CalibrationReport:
        """Walk the graph, running each routine over each of its targets.

        One routine's failure does not abandon the rest: it is recorded against
        that routine and the walk continues, ending in ``partial_failure``. What
        does end the walk is having nothing to run — an empty order, or a
        routine set with no targets — which is reported as ``failed`` rather
        than as a success that measured nothing.

        *on_progress* is told the plan once, before anything runs, and then where
        the walk has got to after each target — the first so the dashboard can draw
        the graph, the rest so it can colour it during the hours before a report
        exists. A sink that raises is logged and the walk carries on: nobody loses a
        calibration because the thing watching it went away.
        """
        report = CalibrationReport(
            timestamp=utc_timestamp(), duration_s=0.0, mode=mode, backend=backend.name
        )
        started = time.monotonic()

        order = only if only is not None else self.execution_order()
        if not order:
            report.status = "failed"
            report.errors.append(
                "no routines to run: every routine is disabled in calibration.yml"
            )
            report.duration_s = time.monotonic() - started
            return report

        if not config.target_qubits:
            report.status = "failed"
            report.errors.append("no target_qubits configured in calibration.yml")
            report.duration_s = time.monotonic() - started
            return report

        log.info(
            "starting %s calibration: %d routine(s) over %d qubit(s), %d edge(s)",
            mode,
            len(order),
            len(config.target_qubits),
            len(config.target_edges),
        )

        # Before the first target, so the dashboard has a graph to colour rather
        # than a graph that appears one routine late.
        _report_progress(
            on_progress,
            {
                "plan": self.plan(order, config, device),
                "target_qubits": list(config.target_qubits),
            },
        )

        ran_any = False
        for position, routine_name in enumerate(order, start=1):
            routine = self.routines[routine_name]
            routine_config = config.get_routine(routine_name)
            targets = self._targets_for(routine_name, config, device)
            # `[n/total] name` on every line of a walk that runs for hours: the
            # journal is the only place an operator can see where it has got to.
            label = f"[{position}/{len(order)}] {routine_name}"
            if not targets:
                log.info("%s skipped: no %s it applies to", label, routine.targets)
                continue

            log.info("%s running on %s", label, ", ".join(targets))
            for target in targets:
                ran_any = True
                target_started = time.monotonic()
                succeeded = self._run_one(
                    routine, target, device, backend, routine_config, config, report
                )
                log.info(
                    "%s %s %s in %s",
                    label,
                    target,
                    "ok" if succeeded else "FAILED",
                    _human_duration(time.monotonic() - target_started),
                )
                _report_progress(
                    on_progress,
                    {
                        "step": position,
                        "total": len(order),
                        "routine": routine_name,
                        "target": target,
                        "succeeded": len(report.routine_results),
                        "failed": len(report.errors),
                        "elapsed_s": round(time.monotonic() - started, 1),
                    },
                )

        if not ran_any:
            report.status = "failed"
            report.errors.append(
                "no routines ran: the enabled routines target edges, and none are configured"
            )
        elif report.errors:
            report.status = (
                "failed" if not report.routine_results else "partial_failure"
            )

        report.duration_s = time.monotonic() - started
        log.info(
            "%s calibration %s in %s: %d succeeded, %d failed",
            mode,
            report.status,
            _human_duration(report.duration_s),
            len(report.routine_results),
            len(report.errors),
        )
        return report

    def _run_one(
        self,
        routine: CalibrationRoutine,
        target: str,
        device: Any,
        backend: SchedulerBackend,
        routine_config: Any,
        config: CalibrationConfig,
        report: CalibrationReport,
    ) -> bool:
        """Run one routine over one target, recording the outcome. True if it worked."""
        started = time.monotonic()
        try:
            if routine.measures_itself:
                # A routine that has to set DC state between acquisitions runs its own
                # loop — see `CalibrationRoutine.measure`. One node needs this and the
                # rest must not pay for it.
                params = routine.measure(
                    target,
                    device,
                    routine_config,
                    backend,
                    self.bias,
                    timeout_s=config.routine_timeout_s,
                )
                elapsed = time.monotonic() - started
                # The ceiling still bounds the whole loop rather than each acquisition
                # in it, so a routine of many long schedules can exceed this. Raised by
                # the last one's allowance, which is the most that is knowable here.
                allowed = max(config.routine_timeout_s, backend.last_allowance_s)
                if elapsed > allowed:
                    raise _over_budget(elapsed, allowed, config.routine_timeout_s)
                fit = params.pop("fit", None)
                routine.apply(device, target, params)
                report.add_routine(
                    RoutineResult(
                        routine_name=routine.name,
                        target=target,
                        parameters=params,
                        timestamp=utc_timestamp(),
                        duration_s=time.monotonic() - started,
                        fit=fit,
                    )
                )
                return True

            schedule = routine.build_schedule(target, device, routine_config, backend)
            # The ceiling goes *into* the wait rather than only being checked after
            # it: `wait_done` blocks, so the check below can report a hang but never
            # end one.
            dataset = backend.run(schedule, timeout_s=config.routine_timeout_s)
            elapsed = time.monotonic() - started
            # Against what the backend was prepared to wait for, not against the
            # configured ceiling: a schedule whose pulses outlast it raises its own
            # allowance (see `SchedulerBackend.allow`), and judging the result by the
            # ceiling instead would wait the longer time and then discard the data.
            allowed = max(config.routine_timeout_s, backend.last_allowance_s)
            if elapsed > allowed:
                raise _over_budget(elapsed, allowed, config.routine_timeout_s)

            params = routine.analyse(dataset, target, device, routine_config)
            # Lifted out before `apply` and before the benchmark's `raw_data` is
            # built from what is left: the sweep behind the fit is a field of its
            # own on the result, not a parameter and not something to write to a
            # device (RFC 0006 §7).
            fit = params.pop("fit", None)
            routine.apply(device, target, params)

            if routine.is_benchmark:
                report.add_benchmarks_from(routine.name, target, params)
            report.add_routine(
                RoutineResult(
                    routine_name=routine.name,
                    target=target,
                    parameters=params,
                    timestamp=utc_timestamp(),
                    duration_s=time.monotonic() - started,
                    fit=fit,
                )
            )
            return True
        except Exception as exc:
            log.exception("routine %s failed on %s", routine.name, target)
            report.errors.append(f"{routine.name}[{target}]: {exc}")
            return False


def _over_budget(elapsed: float, allowed: float, configured: float) -> RoutineError:
    """Both numbers: the one that was enforced, and the one an operator can change.

    They differ when the schedule's own pulses raised the ceiling — see
    `SchedulerBackend.allow` — and an error naming only the setting would then be
    telling the operator to change a number that was not the limit.
    """
    return RoutineError(
        f"exceeded the {allowed:.0f}s allowed after {elapsed:.1f}s "
        f"(routine_timeout_s is {configured:.0f}s)"
    )


def utc_timestamp() -> str:
    """Now, in the millisecond-precision UTC form the report payload uses."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _report_progress(sink: ProgressSink | None, update: dict[str, Any]) -> None:
    """Hand *update* to *sink*, if there is one, without letting it stop the walk."""
    if sink is None:
        return
    try:
        sink(update)
    except Exception:  # noqa: BLE001 - a calibration outlives whoever is watching
        log.warning("could not report progress", exc_info=True)


def _human_duration(seconds: float) -> str:
    """``12.4s``, ``3m12s`` or ``2h14m`` — a full walk is hours, and ``8040.3s`` is not
    a number anyone reads."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, remainder = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{remainder:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _applies(routine: Any, device: Any, target: str, name: str) -> bool:
    """Whether *routine* has anything to measure on *target*.

    A routine whose own check raises is treated as applying: refusing a target is a
    deliberate statement, and an exception is not one.
    """
    try:
        return bool(routine.applies_to(device, target))
    except Exception:  # noqa: BLE001 - a broken predicate must not silence a routine
        log.warning("%s could not say whether it applies to %s", name, target)
        return True
