"""The routine dependency graph, and the walk that runs it (RFC 0004 §5).

A full calibration is a topological walk of the enabled routines. Ordering comes
entirely from each routine's ``depends_on``; nothing else encodes the sequence.
"""

import logging
import time
from collections import defaultdict, deque
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

    # --- checking, and deciding what to recalibrate (RFC 0005 §8) --------------

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
                dataset = backend.run(schedule)
                outcome = routine.analyse_check(dataset, target, device, routine_config)
            except Exception:  # noqa: BLE001 - an unevaluable check is not drift
                log.warning(
                    "check for %s on %s could not be evaluated; treating as unknown",
                    name,
                    target,
                    exc_info=True,
                )
                continue
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
    ) -> CalibrationReport:
        """Walk the graph, running each routine over each of its targets.

        One routine's failure does not abandon the rest: it is recorded against
        that routine and the walk continues, ending in ``partial_failure``. What
        does end the walk is having nothing to run — an empty order, or a
        routine set with no targets — which is reported as ``failed`` rather
        than as a success that measured nothing.
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

        ran_any = False
        for routine_name in order:
            routine = self.routines[routine_name]
            routine_config = config.get_routine(routine_name)
            targets = self._targets_for(routine_name, config, device)
            if not targets:
                log.info(
                    "skipping %s: no %s it applies to", routine_name, routine.targets
                )
                continue

            for target in targets:
                ran_any = True
                if self._run_one(
                    routine, target, device, backend, routine_config, config, report
                ):
                    continue

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
                    target, device, routine_config, backend, self.bias
                )
                elapsed = time.monotonic() - started
                if elapsed > config.routine_timeout_s:
                    raise RoutineError(
                        f"exceeded routine_timeout_s ({config.routine_timeout_s}s) "
                        f"after {elapsed:.1f}s"
                    )
                routine.apply(device, target, params)
                report.add_routine(
                    RoutineResult(
                        routine_name=routine.name,
                        target=target,
                        parameters=params,
                        timestamp=utc_timestamp(),
                        duration_s=time.monotonic() - started,
                    )
                )
                return True

            schedule = routine.build_schedule(target, device, routine_config, backend)
            dataset = backend.run(schedule)
            elapsed = time.monotonic() - started
            if elapsed > config.routine_timeout_s:
                raise RoutineError(
                    f"exceeded routine_timeout_s ({config.routine_timeout_s}s) "
                    f"after {elapsed:.1f}s"
                )

            params = routine.analyse(dataset, target, device, routine_config)
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
                )
            )
            return True
        except Exception as exc:
            log.exception("routine %s failed on %s", routine.name, target)
            report.errors.append(f"{routine.name}[{target}]: {exc}")
            return False


def utc_timestamp() -> str:
    """Now, in the millisecond-precision UTC form the report payload uses."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


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
