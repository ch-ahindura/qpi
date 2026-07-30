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
from qpi_driver.tuners.base.routines import CalibrationRoutine, RoutineError

log = logging.getLogger(__name__)


class CalibrationDAG:
    """The enabled routines, ordered by their dependencies."""

    def __init__(
        self, routines: list[CalibrationRoutine], config: CalibrationConfig
    ) -> None:
        self.routines = {r.name: r for r in routines}
        self.config = config

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
            timestamp=_now(), duration_s=0.0, mode=mode, backend=backend.name
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
            targets = (
                config.target_qubits
                if routine.targets == "qubits"
                else config.target_edges
            )
            if not targets:
                log.info("skipping %s: no %s configured", routine_name, routine.targets)
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
                    timestamp=_now(),
                    duration_s=time.monotonic() - started,
                )
            )
            return True
        except Exception as exc:
            log.exception("routine %s failed on %s", routine.name, target)
            report.errors.append(f"{routine.name}[{target}]: {exc}")
            return False


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
