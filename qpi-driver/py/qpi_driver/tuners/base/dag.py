"""The routine dependency graph, and the walk that runs it (RFC 0004 §5).

A full calibration is a topological walk of the enabled routines. Ordering comes
entirely from each routine's ``depends_on``; nothing else encodes the sequence.
"""

import logging
import time
from collections import defaultdict, deque
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from typing import Any

from qpi_driver.tuners.base.backend import SchedulerBackend
from qpi_driver.tuners.base.config import CalibrationConfig
from qpi_driver.tuners.base.device import (
    component_for,
    edge_names,
    has_path,
    read_path,
)
from qpi_driver.tuners.base.fusion import channels_of
from qpi_driver.tuners.base.grouping import (
    by_output,
    couplings_of,
    groups_of,
    outputs_of,
    readout_misfit,
    split_to_fit,
)
from qpi_driver.tuners.base.provenance import ProvenanceStore
from qpi_driver.tuners.base.report import CalibrationReport, RoutineResult
from qpi_driver.tuners.base.sweep import Sweep
from qpi_driver.tuners.base.routines import (
    CalibrationRoutine,
    CheckOutcome,
    RoutineError,
)

log = logging.getLogger(__name__)

#: Called as the walk proceeds, with the keys the ``CalibrationProgress`` event
#: carries. Three shapes, told apart by which key is present: ``plan`` once before
#: anything runs (RFC 0006 §5.1), ``running`` when targets are about to be measured,
#: and ``target`` when one has finished (RFC 0009 §7.1).
#: Reporting is best-effort — see :meth:`CalibrationDAG.run` — so a sink may
#: raise without ending a calibration.
ProgressSink = Callable[[dict[str, Any]], None]

#: Sequencers on one Qblox RF module, which bounds how many clocks an output can carry
#: at once. A constant rather than a config walk: every RF module in the family has six,
#: and `parallel.max_group` is the knob for a chip that disagrees.
SEQUENCERS_PER_MODULE = 6


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
                check = Sweep(target)
                schedule = routine.build_check_schedule(
                    target, device, routine_config, backend, check
                )
                if schedule is None:
                    continue
                dataset = backend.run(schedule, timeout_s=config.timeout_for(name))
                outcome = routine.analyse_check(
                    dataset, target, device, routine_config, check
                )
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
                    "groups": self.groups_for(name, config, device),
                }
                for name in order
                + [name for name in self.routines if name not in planned]
            ]
        }

    def _prior_notes(
        self,
        order: list[str],
        config: CalibrationConfig,
        device: Any,
        ledger: "_ParameterLedger",
    ) -> list[str]:
        """What this walk is about to trust without anything having measured it (RFC 0008).

        Two kinds, and separating them is the whole value of the note. A prior that a
        routine *in this walk* is about to measure is ordinary — that is what a bring-up
        is. A prior nothing in this walk measures is a number somebody supplied, and the
        run's results are only as good as it: the August 2026 chip's ``f01`` was exactly
        that, and nothing said so for six runs.

        One note per target and kind rather than per parameter, because a five-qubit chip
        reads twenty-odd paths and twenty notes per qubit is a wall rather than a warning.

        The third kind is silent, and it is what made this undecidable before RFC 0008: a
        path **no** routine produces anywhere is hand-supplied on every chip by design —
        `measure.integration_time` and `r12.ef_duration` have no producer in the graph at
        all — so a rule that could not tell it from an absent producer fired on every run,
        which is why RFC 0007 §11 withdrew its pre-walk check. Provenance separates them.
        """
        produced_here = {path for name in order for path in self.routines[name].updates}
        producible = {
            path for routine in self.routines.values() for path in routine.updates
        }
        by_target: dict[str, tuple[set[str], set[str]]] = {}
        for name in order:
            routine = self.routines[name]
            for target in self._targets_for(name, config, device):
                coming, orphaned = by_target.setdefault(target, (set(), set()))
                component = component_for(device, target, routine.targets)
                for path in ledger.priors(routine, target, component):
                    if path in produced_here:
                        coming.add(path)
                    elif path in producible:
                        orphaned.add(path)

        notes = []
        for target, (coming, orphaned) in sorted(by_target.items()):
            if orphaned:
                notes.append(
                    f"{target}: nothing has ever measured {', '.join(sorted(orphaned))}, "
                    f"and the routine that would ({self._producers_of(orphaned)}) is not "
                    "in this run — every result derived from them is only as good as the "
                    "value supplied"
                )
            if coming:
                notes.append(
                    f"{target}: {', '.join(sorted(coming))} not measured before this run"
                )
        return notes

    def _producers_of(self, paths: set[str]) -> str:
        """The routines that write any of *paths*, for a note that names the way out."""
        producers = {
            name
            for name, routine in self.routines.items()
            if paths & set(routine.updates)
        }
        return ", ".join(sorted(producers))

    def groups_for(
        self,
        name: str,
        config: CalibrationConfig,
        device: Any = None,
        backend: SchedulerBackend | None = None,
    ) -> list[list[str]]:
        """Routine *name*'s targets, in the sets that may be measured at once (RFC 0009 §5).

        One target per group unless ``parallel.enabled``, which is what keeps this
        additive: every caller sees the sequential shape until a config asks for
        another.

        Explicit ``parallel.groups`` are filtered to the targets this run actually
        walks rather than used as given — a config naming a qubit the run excludes
        would otherwise put it back.
        """
        targets = self._targets_for(name, config, device)
        parallel = config.parallel
        if not parallel.enabled or len(targets) < 2:
            return [[target] for target in targets]

        kind = self.routines[name].targets
        named = parallel.groups.get(kind)
        if named is not None:
            wanted = set(targets)
            grouped = [[t for t in group if t in wanted] for group in named]
            claimed = {t for group in grouped for t in group}
            # Whatever the config forgot still has to be calibrated.
            groups = [group for group in grouped if group] + [
                [t] for t in targets if t not in claimed
            ]
        else:
            adjacency = couplings_of(edge_names(device) or config.target_edges)
            spacing = parallel.spacing_for(kind)
            if len(targets) > 1 and spacing > 1 and not adjacency:
                # No adjacency is not the same as nothing being adjacent. A device whose
                # edges cannot be read, or a config that targets none, would otherwise
                # put every qubit at infinite distance and so in one group — the most
                # aggressive setting available, arrived at by accident.
                log.warning(
                    "%s: no coupling graph is readable, so a spacing of %d cannot be "
                    "honoured over %d targets — running them one at a time. Configure "
                    "'target_edges', name the groups under 'parallel.groups', or set a "
                    "spacing of 1 to measure the whole chip at once",
                    name,
                    spacing,
                    len(targets),
                )
                return [[target] for target in targets]
            groups = groups_of(
                targets,
                adjacency=adjacency,
                spacing=spacing,
                exclude=parallel.exclude,
                max_group=parallel.max_group,
            )
        return self._fitted(groups, device, backend)

    def _fitted(
        self, groups: list[list[str]], device: Any, backend: SchedulerBackend | None
    ) -> list[list[str]]:
        """*groups* narrowed to what the instruments can actually play at once (§5.4).

        Bisected rather than refused, and only when both a device and a backend are to
        hand — a bare `plan` call has neither and gets the colouring as it stands.
        """
        if device is None or backend is None:
            return groups
        wiring = outputs_of(device)
        band_hz = float(getattr(backend, "if_limit_hz", 0.0) or 0.0)
        if not band_hz:
            return groups

        def misfit(group: Sequence[str]) -> str | None:
            # Per output, because targets behind different ones share no LO and no DAC
            # and constrain each other not at all.
            for shared in by_output(list(group), wiring, "res").values():
                readouts = [_readout_of(device, target) for target in shared]
                usable = [pair for pair in readouts if pair is not None]
                if len(usable) < 2:
                    continue
                reason = readout_misfit(
                    [clock for clock, _ in usable],
                    [amplitude for _, amplitude in usable],
                    band_hz=band_hz,
                    sequencers=SEQUENCERS_PER_MODULE,
                )
                if reason is not None:
                    return reason
            return None

        return [
            narrowed for group in groups for narrowed in split_to_fit(group, misfit)
        ]

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
        provenance: ProvenanceStore | None = None,
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

        *provenance* is what earlier runs measured, and is read but never written here:
        the walk reports which of its inputs nothing has ever measured, and the write
        happens after the device write-back, where a record cannot outrun the value it
        describes. ``None`` makes every parameter a prior, which is what a chip with no
        sidecar has and must still calibrate from.
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
        skipped = 0
        ledger = _ParameterLedger(provenance)
        for note in self._prior_notes(order, config, device, ledger):
            log.warning("%s", note)
            report.notes.append(note)
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

            groups = self.groups_for(routine_name, config, device, backend)
            if len(groups) < len(targets):
                log.info(
                    "%s running on %s in %d group(s): %s",
                    label,
                    ", ".join(targets),
                    len(groups),
                    " | ".join(" ".join(group) for group in groups),
                )
            else:
                log.info("%s running on %s", label, ", ".join(targets))
            head = {"step": position, "total": len(order), "routine": routine_name}
            for group in groups:
                runnable: list[str] = []
                for target in group:
                    blocked = ledger.blockers(routine, target)
                    if not blocked:
                        runnable.append(target)
                        continue
                    # Not run and not failed: it has nothing to measure against, so
                    # running it would report a confident number off an uncalibrated
                    # chip, and failing it would invent an error that never happened.
                    # RFC 0007 §11.
                    detail = ledger.explain(blocked)
                    log.warning("%s %s skipped: %s", label, target, detail)
                    report.notes.append(f"{routine_name}[{target}]: skipped, {detail}")
                    # What it did not reconfirm, and what those parameters still hold.
                    # RFC 0007 §11 keeps a skipped node's stale values — clearing them
                    # would stop a chip that ran yesterday from running today — so the
                    # operator's question is how old they are, which provenance answers.
                    left = ledger.unconfirmed(routine, target)
                    if left:
                        log.warning("%s %s %s", label, target, left)
                        report.notes.append(f"{routine_name}[{target}]: {left}")
                    ledger.unsatisfied(routine, target, blame=ledger.blame(blocked))
                    skipped += 1
                    # Without this the node reports nothing at all, and one whose every
                    # target is blocked stays `pending` for the rest of the run.
                    _report_progress(
                        on_progress,
                        {**head, **_tally(report, skipped, started), "target": target},
                    )

                if not runnable:
                    continue

                # Before the work, so the graph can colour the node it is on rather than
                # the node it has just left, and name every target in flight. RFC 0009 §7.
                _report_progress(
                    on_progress,
                    {**head, **_tally(report, skipped, started), "running": runnable},
                )
                ran_any = True
                group_started = time.monotonic()
                # Before the run, not after: the ledger records what this routine
                # produced, and a routine that refines its own input would otherwise
                # look as though it had been given a measured one.
                priors = {
                    target: ledger.priors(
                        routine, target, component_for(device, target, routine.targets)
                    )
                    for target in runnable
                }
                outcomes = self._run_group(
                    routine,
                    runnable,
                    device,
                    backend,
                    routine_config,
                    config,
                    report,
                    priors,
                )
                for target in runnable:
                    if outcomes.get(target):
                        ledger.produced(routine, target)
                    else:
                        ledger.unsatisfied(routine, target)
                log.info(
                    "%s %s %s in %s",
                    label,
                    ", ".join(runnable),
                    "ok" if all(outcomes.get(t) for t in runnable) else "FAILED",
                    _human_duration(time.monotonic() - group_started),
                )
                for target in runnable:
                    _report_progress(
                        on_progress,
                        {**head, **_tally(report, skipped, started), "target": target},
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
            "%s calibration %s in %s: %d succeeded, %d failed, %d skipped",
            mode,
            report.status,
            _human_duration(report.duration_s),
            len(report.routine_results),
            len(report.errors),
            skipped,
        )
        return report

    def _run_group(
        self,
        routine: CalibrationRoutine,
        targets: list[str],
        device: Any,
        backend: SchedulerBackend,
        routine_config: Any,
        config: CalibrationConfig,
        report: CalibrationReport,
        priors: dict[str, tuple[str, ...]],
    ) -> dict[str, bool]:
        """Run *routine* over *targets*, fused into one acquisition where it can be.

        A group of one, or a routine that has not opted into fusion, goes through
        `_run_one` exactly as it did before any of this existed — which is what keeps
        every unconverted routine's behaviour identical.
        """
        # Split before running: a routine whose grid is derived per target cannot hold
        # two of them in one schedule (RFC 0009 D7).
        if len(targets) > 1:
            compatible = routine.compatible_groups(targets, device, routine_config)
            if len(compatible) > 1:
                split: dict[str, bool] = {}
                for subgroup in compatible:
                    split.update(
                        self._run_group(
                            routine,
                            subgroup,
                            device,
                            backend,
                            routine_config,
                            config,
                            report,
                            priors,
                        )
                    )
                return split
        # The acceptance measurement: benchmark in company, then alone, and report the
        # difference. Before the group runs, because the isolated pass is the control and
        # a group that fails should not leave a penalty computed from half a comparison.
        if (
            len(targets) > 1
            and routine.is_benchmark
            and config.parallel.measure_penalty
        ):
            return self._run_with_penalty(
                routine,
                targets,
                device,
                backend,
                routine_config,
                config,
                report,
                priors,
            )
        if len(targets) > 1 and routine.measures_group:
            return self._run_measured_group(
                routine,
                targets,
                device,
                backend,
                routine_config,
                config,
                report,
                priors,
            )
        # A routine with its own measurement loop must not have it bypassed merely
        # because its schedule can be fused: `measure` is where escalation, refinement
        # and any between-pass write-back live, and `_run_fused` runs neither. Such a
        # routine groups only once it has a `measure_group` (RFC 0009 §6.5).
        own_loop_only = routine.measures_itself and not routine.measures_group
        if len(targets) == 1 or not routine.fusable or own_loop_only:
            return {
                target: self._run_one(
                    routine,
                    target,
                    device,
                    backend,
                    routine_config,
                    config,
                    report,
                    priors.get(target, ()),
                )
                for target in targets
            }
        return self._run_fused(
            routine, targets, device, backend, routine_config, config, report, priors
        )

    def _run_with_penalty(
        self,
        routine: CalibrationRoutine,
        targets: list[str],
        device: Any,
        backend: SchedulerBackend,
        routine_config: Any,
        config: CalibrationConfig,
        report: CalibrationReport,
        priors: dict[str, tuple[str, ...]],
    ) -> dict[str, bool]:
        """Benchmark *targets* together and then alone, recording what company cost them.

        The measurement RFC 0009 §5.6 turns `qubit_spacing` from a guess into a setting,
        and it is Gambetta et al.'s: benchmark each qubit alone, benchmark them
        simultaneously, and the difference in average gate fidelity *is* the
        addressability. Both halves are runs this walk can already do, so the whole of it
        is arithmetic on two reports.

        The isolated pass runs second and its results are dropped, because the fused
        numbers are the ones that describe how the chip will actually be driven. What is
        kept is the difference, on the benchmark the group produced.

        Costs twice what benchmarking once does, which is why it is opt-in.
        """
        outcomes = self._run_group_without_penalty(
            routine, targets, device, backend, routine_config, config, report, priors
        )
        together = {
            benchmark.target: benchmark
            for benchmark in report.benchmarks
            if benchmark.protocol == routine.name
        }

        # A throwaway report, so the control pass cannot add rows of its own: the graph is
        # the fused one and the isolated numbers are a reference, not a result.
        control = CalibrationReport(
            timestamp=utc_timestamp(),
            duration_s=0.0,
            mode=report.mode,
            backend=backend.name,
        )
        for target in targets:
            self._run_one(
                routine,
                target,
                device,
                backend,
                routine_config,
                config,
                control,
                priors.get(target, ()),
            )
        alone = {
            benchmark.target: benchmark.fidelity for benchmark in control.benchmarks
        }

        for target, benchmark in together.items():
            isolated = alone.get(target)
            if isolated is None or benchmark.fidelity is None:
                continue
            benchmark.parallel_penalty = isolated - benchmark.fidelity
            log.info(
                "%s on %s: %.5f alone against %.5f in company — a penalty of %.5f",
                routine.name,
                target,
                isolated,
                benchmark.fidelity,
                benchmark.parallel_penalty,
            )
        return outcomes

    def _run_group_without_penalty(
        self,
        routine: CalibrationRoutine,
        targets: list[str],
        device: Any,
        backend: SchedulerBackend,
        routine_config: Any,
        config: CalibrationConfig,
        report: CalibrationReport,
        priors: dict[str, tuple[str, ...]],
    ) -> dict[str, bool]:
        """The grouped run itself, without the control pass — see `_run_with_penalty`."""
        if routine.measures_group:
            return self._run_measured_group(
                routine,
                targets,
                device,
                backend,
                routine_config,
                config,
                report,
                priors,
            )
        return self._run_fused(
            routine, targets, device, backend, routine_config, config, report, priors
        )

    def _run_measured_group(
        self,
        routine: CalibrationRoutine,
        targets: list[str],
        device: Any,
        backend: SchedulerBackend,
        routine_config: Any,
        config: CalibrationConfig,
        report: CalibrationReport,
        priors: dict[str, tuple[str, ...]],
    ) -> dict[str, bool]:
        """A group whose routine runs its own loop — escalation included (RFC 0009 §6.5).

        The loop is the routine's, so unlike `_run_fused` this cannot slice one dataset:
        a widening splits the group and the pieces are measured separately. What comes back
        is one outcome per target, fitted parameters or the refusal that ended it.
        """
        started = time.monotonic()
        backend.start_accounting()
        allowance = config.timeout_for(routine.name)
        sweeps = {target: Sweep(target) for target in targets}
        try:
            measured = routine.measure_group(
                targets,
                device,
                routine_config,
                backend,
                sweeps,
                self.bias,
                timeout_s=allowance,
            )
        except Exception as exc:  # noqa: BLE001 - the loop itself failed, so all of them did
            log.exception(
                "routine %s failed on the group %s", routine.name, ", ".join(targets)
            )
            for target in targets:
                report.errors.append(f"{routine.name}[{target}]: {exc}")
                _record_refused_fit(report, routine, target, exc, started)
            return {target: False for target in targets}

        # The whole loop under one ceiling, as the sequential path judges `measure`.
        allowed = max(allowance, backend.total_allowance_s)
        elapsed = time.monotonic() - started
        over = (
            _over_budget(elapsed, allowed, allowance, routine.name)
            if elapsed > allowed
            else None
        )

        outcomes: dict[str, bool] = {}
        for target in targets:
            result = over or measured.get(
                target, RoutineError(f"{routine.name} reported nothing for {target}")
            )
            if isinstance(result, Exception):
                log.error("routine %s on %s: %s", routine.name, target, result)
                report.errors.append(f"{routine.name}[{target}]: {result}")
                _record_refused_fit(report, routine, target, result, started)
                outcomes[target] = False
                continue
            outcomes[target] = self._record_measured(
                routine, target, device, result, report, started, priors.get(target, ())
            )
        return outcomes

    def _record_measured(
        self,
        routine: CalibrationRoutine,
        target: str,
        device: Any,
        params: dict[str, Any],
        report: CalibrationReport,
        started: float,
        priors: tuple[str, ...],
    ) -> bool:
        """Write back and record what a routine's own loop measured for one target."""
        try:
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
                    priors=priors,
                )
            )
            return True
        except Exception as exc:  # noqa: BLE001 - a write-back failure is this target's
            log.exception("routine %s could not apply on %s", routine.name, target)
            report.errors.append(f"{routine.name}[{target}]: {exc}")
            _record_refused_fit(report, routine, target, exc, started)
            return False

    def _run_fused(
        self,
        routine: CalibrationRoutine,
        targets: list[str],
        device: Any,
        backend: SchedulerBackend,
        routine_config: Any,
        config: CalibrationConfig,
        report: CalibrationReport,
        priors: dict[str, tuple[str, ...]],
    ) -> dict[str, bool]:
        """One schedule over every target, then one fit per target (RFC 0009 §6.2).

        The acquisition is shared and the analysis is not: each target gets its own
        channel of the dataset and its own `analyse`, so a refused fit is that target's
        failure and the rest of the group still lands.
        """
        outcomes = {target: False for target in targets}
        started = time.monotonic()
        backend.start_accounting()
        allowance = config.timeout_for(routine.name)
        # One per target, and the same object the fit reads back: a sweep centred on a
        # per-qubit value has a different grid per target, and a routine keeping them on
        # itself would leave every fit in the group reading whichever built last.
        sweeps = {target: Sweep(target) for target in targets}
        try:
            schedule = routine.build_group_schedule(
                targets, device, routine_config, backend, sweeps
            )
            dataset = backend.run(schedule, timeout_s=allowance)
            elapsed = time.monotonic() - started
            # One arm-and-wait cycle for the whole group, so the ceiling bounds the
            # group — the sequencers played concurrently and the pulses were one
            # target's.
            allowed = max(allowance, backend.total_allowance_s)
            if elapsed > allowed:
                raise _over_budget(elapsed, allowed, allowance, routine.name)
        except Exception as exc:
            # The acquisition is shared, so its failure is every target's. Recorded once
            # each, because a routine-and-target is what the report accounts for.
            log.exception(
                "routine %s failed on the group %s", routine.name, ", ".join(targets)
            )
            for target in targets:
                report.errors.append(f"{routine.name}[{target}]: {exc}")
                _record_refused_fit(report, routine, target, exc, started)
            return outcomes

        sliced = channels_of(dataset, targets)
        for target in targets:
            acquisition = sliced.get(target)
            if acquisition is None:
                message = "the fused acquisition carried no channel for it"
                log.error("routine %s on %s: %s", routine.name, target, message)
                report.errors.append(f"{routine.name}[{target}]: {message}")
                continue
            outcomes[target] = self._fit_one(
                routine,
                target,
                acquisition,
                device,
                routine_config,
                report,
                started,
                sweeps[target],
                priors.get(target, ()),
            )
        return outcomes

    def _fit_one(
        self,
        routine: CalibrationRoutine,
        target: str,
        dataset: Any,
        device: Any,
        routine_config: Any,
        report: CalibrationReport,
        started: float,
        sweep: Sweep,
        priors: tuple[str, ...] = (),
    ) -> bool:
        """Analyse one target's acquisition and record what it produced.

        The tail of `_run_one` from the fit onwards, shared so a fused target is
        recorded exactly as a sequential one is.
        """
        try:
            params = routine.analyse(dataset, target, device, routine_config, sweep)
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
                    priors=priors,
                )
            )
            return True
        except Exception as exc:
            log.exception("routine %s failed on %s", routine.name, target)
            report.errors.append(f"{routine.name}[{target}]: {exc}")
            _record_refused_fit(report, routine, target, exc, started)
            return False

    def _run_one(
        self,
        routine: CalibrationRoutine,
        target: str,
        device: Any,
        backend: SchedulerBackend,
        routine_config: Any,
        config: CalibrationConfig,
        report: CalibrationReport,
        priors: tuple[str, ...] = (),
    ) -> bool:
        """Run one routine over one target, recording the outcome. True if it worked."""
        started = time.monotonic()
        backend.start_accounting()
        allowance = config.timeout_for(routine.name)
        sweep = Sweep(target)
        try:
            if routine.measures_itself:
                # A routine whose acquisitions cannot be one schedule — DC state set
                # between them, or setpoints that depend on an earlier result — runs its
                # own loop. See `CalibrationRoutine.measure`; the rest must not pay
                # for it.
                params = routine.measure(
                    target,
                    device,
                    routine_config,
                    backend,
                    sweep,
                    self.bias,
                    timeout_s=allowance,
                )
                elapsed = time.monotonic() - started
                # Against the *sum* of what each acquisition was owed, since the ceiling
                # bounds the whole loop. Judging a three-schedule search by the last
                # schedule's allowance alone would fail a routine that never exceeded its
                # allowance once — the exact failure `allow` exists to prevent, moved one
                # level out.
                allowed = max(allowance, backend.total_allowance_s)
                if elapsed > allowed:
                    raise _over_budget(elapsed, allowed, allowance, routine.name)
                fit = params.pop("fit", None)
                routine.apply(device, target, params)
                # On this path too, and it was not. A benchmark that gained a `measure`
                # silently stopped appearing in `report.benchmarks` while still appearing
                # in `routine_results` — so it looked like it had run, and the drift check
                # compared against nothing. `rb` gaining an escalation is what surfaced it;
                # `allxy_check` and `interleaved_rb` would have hit the same wall.
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
                        priors=priors,
                    )
                )
                return True

            # Through `acquire`, so a routine whose sweep needs more than one schedule
            # chunks it there rather than here — see `CalibrationRoutine.acquire`.
            dataset = routine.acquire(
                target, device, routine_config, backend, allowance, sweep
            )
            elapsed = time.monotonic() - started
            # Against what the backend was prepared to wait for, not against the
            # configured ceiling: a schedule whose pulses outlast it raises its own
            # allowance (see `SchedulerBackend.allow`), and judging the result by the
            # ceiling instead would wait the longer time and then discard the data. The
            # total and the last are the same number on this path, which runs one
            # schedule; it is the total so that both paths read the same way.
            allowed = max(allowance, backend.total_allowance_s)
            if elapsed > allowed:
                raise _over_budget(elapsed, allowed, allowance, routine.name)

            params = routine.analyse(dataset, target, device, routine_config, sweep)
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
                    priors=priors,
                )
            )
            return True
        except Exception as exc:
            log.exception("routine %s failed on %s", routine.name, target)
            report.errors.append(f"{routine.name}[{target}]: {exc}")
            _record_refused_fit(report, routine, target, exc, started)
            return False


def _record_refused_fit(
    report: CalibrationReport,
    routine: CalibrationRoutine,
    target: str,
    exc: BaseException,
    started: float,
) -> None:
    """Keep the sweep a guard refused, where the guard handed one back.

    Duck-typed off the exception rather than typed, because the two exception hierarchies
    a routine can raise from are unrelated — see `CarriesFit`, which both mix in.

    No parameters: nothing was applied and nothing was written, and an empty mapping says
    that where a populated one would read as a measurement. The failure is still counted
    once, through `report.errors`.
    """
    refused = getattr(exc, "fit", None)
    if refused is None:
        return
    report.add_routine(
        RoutineResult(
            routine_name=routine.name,
            target=target,
            parameters={},
            timestamp=utc_timestamp(),
            duration_s=time.monotonic() - started,
            fit=refused,
            failed=True,
        )
    )


def _over_budget(
    elapsed: float, allowed: float, configured: float, routine: str
) -> RoutineError:
    """Both numbers: the one that was enforced, and the one an operator can change.

    They differ when the schedule's own pulses raised the ceiling — see
    `SchedulerBackend.allow` — and an error naming only the setting would then be
    telling the operator to change a number that was not the limit.

    Names *where* the setting lives too, since a routine may carry its own ``timeout_s``
    and an operator raising the global one would otherwise see no effect.
    """
    return RoutineError(
        f"exceeded the {allowed:.0f}s allowed after {elapsed:.1f}s "
        f"(the ceiling for {routine} is {configured:.0f}s — raise its own timeout_s, "
        f"or routine_timeout_s if it has none)"
    )


def utc_timestamp() -> str:
    """Now, in the millisecond-precision UTC form the report payload uses."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _readout_of(device: Any, target: str) -> tuple[float, float] | None:
    """*target*'s readout frequency and pulse amplitude, or ``None`` if either is absent.

    Absent leaves the group as it was: an element with nowhere to keep a readout is not
    an element whose readout collides with anyone's.
    """
    element = component_for(device, target)
    if element is None:
        return None
    try:
        return (
            float(read_path(element, "clock_freqs.readout")),
            float(read_path(element, "measure.pulse_amp")),
        )
    except Exception:  # noqa: BLE001 - an unreadable readout imposes no constraint
        return None


def _tally(report: CalibrationReport, skipped: int, started: float) -> dict[str, Any]:
    """The walk's running totals, which every progress event carries.

    On the start event too, not only the finish: the server reads whether a target
    failed from the difference against the last event's ``failed``, so an event that
    reported zero would make the next one look like a failure.
    """
    return {
        "succeeded": len(report.routine_results),
        "failed": len(report.errors),
        "skipped": skipped,
        "elapsed_s": round(time.monotonic() - started, 1),
    }


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


class _ParameterLedger:
    """What a walk has produced, and what it has failed to produce (RFC 0007 §11).

    A node whose input was never measured cannot measure anything either. Running it
    anyway is how one failure became six on an August 2026 chip: `qubit_spectroscopy`
    failed, and six nodes behind it measured a qubit still in ``|0>`` and reported
    confident numbers fitted from its noise.

    Keyed on *parameters* rather than on routines, which is what makes it correct here.
    `depends_on` orders the walk and is not a data dependency — `cz_chevron` depends on
    `rb` and `flux_spectroscopy`, and neither writes a parameter at all — so blocking a
    node because a neighbour failed would decline work that has everything it needs.
    Two consequences fall out of the parameter view for free:

    - **A failed refiner blocks nothing.** Seven parameters have two writers, the
      first producing and the second refining. `ramsey` failing leaves the
      `clock_freqs.f01` that `qubit_spectroscopy` produced, so every node reading f01
      still runs.
    - **A disabled node is not a failed one.** It never ran, so it never recorded a
      failure, and nothing downstream is blocked by its absence. A parameter an
      operator supplies by hand — `measure.integration_time` and `r12.ef_duration` have
      no producer in the graph at all — is likewise never in question.
    """

    def __init__(self, provenance: ProvenanceStore | None = None) -> None:
        self._produced: set[tuple[str, str]] = set()
        self._unsatisfied: dict[tuple[str, str], set[str]] = {}
        #: What earlier runs measured. Empty when there is no sidecar, which makes
        #: every parameter a prior and leaves this ledger exactly as it was before
        #: RFC 0008 — the walk must not need the file to be there.
        self._provenance = provenance or ProvenanceStore()

    def attributable(self, target: str, path: str) -> bool:
        """Whether anything ever measured *path* on *target*.

        The union of two facts, and it needs both: this walk produced it, or some earlier
        run recorded that it did. "Produced here" alone is right for a bring-up and too
        narrow afterwards — on a partial recalibration almost nothing is produced by this
        run, so almost nothing would be checkable (RFC 0008 §6).
        """
        return (target, path) in self._produced or self._provenance.is_measured(
            target, path
        )

    def priors(
        self, routine: CalibrationRoutine, target: str, component: Any = None
    ) -> tuple[str, ...]:
        """The parameters *routine* reads that nothing has ever measured.

        Restricted to paths *component* actually has. Several reads are opt-in fields a
        given element does not carry — a `BasicTransmonElement` has no ``spec`` submodule
        at all — and a path that does not exist on this chip is not an unmeasured one, so
        reporting it would put a permanent warning in front of every run.
        """
        return tuple(
            path
            for path in routine.reads
            if not self.attributable(target, path)
            and (component is None or has_path(component, path))
        )

    def produced(self, routine: CalibrationRoutine, target: str) -> None:
        """Record that *routine* measured what it writes."""
        for path in routine.updates:
            self._produced.add((target, path))

    def unsatisfied(
        self,
        routine: CalibrationRoutine,
        target: str,
        blame: set[str] | None = None,
    ) -> None:
        """Record that *routine* did not produce what it writes.

        *blame* carries the root cause forward when this routine was itself skipped, so
        a chain of skips names the failure that started it rather than its neighbour —
        the same choice `diagnose` makes in blaming the deepest failing ancestor.
        """
        culprits = blame or {routine.name}
        for path in routine.updates:
            self._unsatisfied.setdefault((target, path), set()).update(culprits)

    def unconfirmed(self, routine: CalibrationRoutine, target: str) -> str:
        """What a skipped *routine* left standing on *target*, and how old it is.

        Empty when it writes nothing, or when nothing ever measured what it writes — there
        is then no staleness to report, only the prior the pre-walk notes already named.
        """
        described = [
            f"{path} still holds what {record.routine} measured at {record.at}"
            for path in routine.updates
            if (record := self._provenance.of(target, path)) is not None
        ]
        if not described:
            return ""
        return "not reconfirmed by this run: " + "; ".join(described)

    def blockers(self, routine: CalibrationRoutine, target: str) -> dict[str, set[str]]:
        """The parameters *routine* reads that this walk failed to produce.

        An edge is asked about its endpoints as well as itself, because a two-qubit gate is
        measured *through* its qubits — `cz_chevron` prepares ``|11>`` with a pi pulse on
        each — so a failed `rabi` on either end leaves it nothing to prepare with.
        `CalibrationConfig.validate_targets` already refuses an edge whose qubits are not
        themselves being calibrated, and splits the name the same way, so the convention is
        load-bearing before it gets here.

        Both spellings are tried per path rather than classifying paths by where they live:
        ``("q5_q10", "rxy.amp180")`` is a key nothing writes, and ``("q5", "cz.square_amp")``
        likewise, so an irrelevant spelling is silently absent rather than wrong.
        """
        blocked: dict[str, set[str]] = {}
        for path in routine.reads:
            for site in self._sites(routine, target):
                key = (site, path)
                if key in self._produced:
                    continue
                culprits = self._unsatisfied.get(key)
                if culprits:
                    blocked.setdefault(path, set()).update(culprits)
        return blocked

    @staticmethod
    def _sites(routine: CalibrationRoutine, target: str) -> tuple[str, ...]:
        """Where *routine*'s reads may live: the target, plus an edge's two endpoints."""
        if routine.targets != "edges":
            return (target,)
        return (target, *target.split("_"))

    @staticmethod
    def blame(blocked: dict[str, set[str]]) -> set[str]:
        """Every routine implicated in *blocked*, to pass on to whatever this blocks."""
        return {name for culprits in blocked.values() for name in culprits}

    @staticmethod
    def explain(blocked: dict[str, set[str]]) -> str:
        """Why a node was skipped, naming the parameter and who failed to produce it."""
        return "; ".join(
            f"nothing produced {path} ({', '.join(sorted(culprits))} failed)"
            for path, culprits in sorted(blocked.items())
        )
