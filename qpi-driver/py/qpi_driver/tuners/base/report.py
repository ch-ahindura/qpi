"""What a calibration run reports back (RFC 0004 §6.8).

:meth:`CalibrationReport.to_event_payload` is the wire shape of a
``CalibrationResult`` payload, and QPI-UI's ``CalibrationResultPayload`` is its
counterpart. The two are asserted against each other by a test in each language;
they are one contract written twice.
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

#: How much of a report's ``routine_results`` may be fit summaries before all of them
#: are dropped (RFC 0006 §7). A full walk on five qubits is projected at ~150 kB, so
#: this is more than an order of magnitude of headroom: it is not a budget to spend
#: but a floor under which a report is guaranteed to save. A report that will not
#: save is worse than a report with no chart in it.
MAX_FIT_PAYLOAD_BYTES = 2_000_000

#: Protocols whose ``fidelity`` is an average gate fidelity, and so comparable with each
#: other's.
#:
#: `allxy_check` is deliberately not one. It reports ``1 - rms_deviation`` of a *normalised
#: population* response, which is a diagnostic score and not a gate infidelity — the two are
#: not in the same units, and treating them as though they were made the worse number win by
#: construction. On the August 2026 B chip randomised benchmarking measured a gate fidelity of
#: 0.9879 while AllXY's score was 0.9232, and the report showed 0.9232: not a second, worse
#: measurement of the same thing, but a different quantity wearing the same name.
#:
#: Both are still worth having, and the gap between them is information rather than noise —
#: AllXY is sensitive to *coherent* errors that randomisation averages into a depolarising
#: rate, so it can be worse than RB and be right to be. That chip's AllXY was flat on both
#: plateaus and carried all its error antisymmetrically across the equator block, which is a
#: pi/2 pulse under-rotating. See :meth:`CalibrationReport.fidelities`.
GATE_FIDELITY_PROTOCOLS = frozenset({"rb", "interleaved_rb"})


@dataclass
class RoutineResult:
    """One routine's outcome on one target."""

    routine_name: str
    target: str
    parameters: dict[str, Any]
    timestamp: str
    duration_s: float
    #: The sweep behind the fit (RFC 0006 §7). ``None`` for a routine whose
    #: ``analyse`` does not produce one yet — one is converted at a time, and the
    #: card simply shows no chart for the rest.
    fit: dict[str, Any] | None = None
    #: Which of this routine's ``reads`` nothing had ever measured when it ran
    #: (RFC 0008). A result derived from a prior is not wrong, but it is only as
    #: good as the number it was given, and that was previously unknowable after
    #: the fact. Out of :meth:`to_dict` for the reason `CalibrationReport.notes`
    #: is out of the payload: it is one contract written twice.
    priors: tuple[str, ...] = ()
    #: Whether this result is the *trace of a refusal* rather than a measurement.
    #:
    #: A guard that rejects a fit is when the fit most wants looking at, so the sweep is
    #: kept and the parameters are not: nothing was applied and nothing was written, and
    #: `parameters` is empty to say so. The failure itself is still reported through
    #: `CalibrationReport.errors`, which stays the one place a run's failures are counted.
    #:
    #: Out of :meth:`to_dict` for the reason `priors` is: that payload is one contract
    #: written twice and a field added on one side only would fail the tests asserting the
    #: two against each other. The chart still reaches the card, because `fit` is in the
    #: payload already.
    failed: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "routine_name": self.routine_name,
            "target": self.target,
            "parameters": self.parameters,
            "timestamp": self.timestamp,
            "duration_s": self.duration_s,
        }
        if self.fit is not None:
            payload["fit"] = self.fit
        return payload


@dataclass
class BenchmarkResult:
    """A fidelity measurement — the leaves of the DAG, which calibrate nothing."""

    protocol: str
    target: str
    fidelity: float | None
    error_per_gate: float | None
    raw_data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "target": self.target,
            "fidelity": self.fidelity,
            "error_per_gate": self.error_per_gate,
            "raw_data": self.raw_data,
        }


@dataclass
class CalibrationReport:
    """The result of one calibration run."""

    timestamp: str
    duration_s: float
    mode: str  # 'full', 'partial', 'fidelity_check'
    backend: str = ""
    routine_results: list[RoutineResult] = field(default_factory=list)
    benchmarks: list[BenchmarkResult] = field(default_factory=list)
    status: str = "success"  # 'success', 'partial_failure', 'failed'
    errors: list[str] = field(default_factory=list)
    #: What the checks found, when `diagnose` decided the scope of this run
    #: (RFC 0005 §8). Deliberately **not** in :meth:`to_event_payload`: that
    #: payload is one contract written twice, asserted against QPI-UI's
    #: ``CalibrationResultPayload`` in Go and TypeScript, and a field added on one
    #: side only would fail those tests. Notes are for the operator's log and the
    #: local report until the server side is extended to carry them.
    notes: list[str] = field(default_factory=list)

    def add_routine(self, result: RoutineResult) -> None:
        self.routine_results.append(result)

    def add_benchmark(self, benchmark: BenchmarkResult) -> None:
        self.benchmarks.append(benchmark)

    def add_benchmarks_from(
        self, protocol: str, target: str, params: dict[str, Any]
    ) -> None:
        """Record a benchmark routine's fitted fidelity alongside its routine result."""
        self.benchmarks.append(
            BenchmarkResult(
                protocol=protocol,
                target=target,
                fidelity=params.get("fidelity"),
                error_per_gate=params.get("error_per_gate"),
                raw_data={
                    k: v
                    for k, v in params.items()
                    if k not in ("fidelity", "error_per_gate")
                },
            )
        )

    def fidelities(self) -> dict[str, float]:
        """Measured gate fidelity per target, for the drift check to compare against.

        Where a target was benchmarked by more than one *comparable* protocol the lowest
        wins: a drift check should trigger on the worst evidence it has, not the best.

        Comparable is the load-bearing word, and it was missing. Only
        :data:`GATE_FIDELITY_PROTOCOLS` report an average gate fidelity; `allxy_check`
        reports one minus the rms deviation of a normalised population response, which is a
        different quantity in different units. Taking the minimum across both let the
        incommensurable one win by construction — a B chip measured 0.9879 by randomised
        benchmarking and reported 0.9232, which is AllXY's score and not its gate fidelity.

        A diagnostic score is still used when nothing measured a gate fidelity, because a
        drift check with AllXY as its only evidence should compare against that rather than
        against nothing — see ``monitoring.allxy_as_smoke_test``. Both appear in
        :attr:`benchmarks` either way, each under its own protocol.
        """
        worst: dict[str, float] = {}
        fallback: dict[str, float] = {}
        for benchmark in self.benchmarks:
            if benchmark.fidelity is None:
                continue
            into = worst if benchmark.protocol in GATE_FIDELITY_PROTOCOLS else fallback
            current = into.get(benchmark.target)
            if current is None or benchmark.fidelity < current:
                into[benchmark.target] = benchmark.fidelity
        for target, score in fallback.items():
            worst.setdefault(target, score)
        return worst

    def to_event_payload(self) -> dict[str, Any]:
        """The ``CalibrationResult`` payload, flat — QPI-UI unmarshals this directly."""
        return {
            "timestamp": self.timestamp,
            "duration_s": self.duration_s,
            "mode": self.mode,
            "backend": self.backend,
            "status": self.status,
            "routine_results": _within_fit_cap(
                [r.to_dict() for r in self.routine_results]
            ),
            "benchmarks": [b.to_dict() for b in self.benchmarks],
            "errors": self.errors,
        }

    def summary(self) -> str:
        return (
            f"CalibrationReport(mode={self.mode}, status={self.status}, "
            f"duration={self.duration_s:.1f}s, routines={len(self.routine_results)}, "
            f"benchmarks={len(self.benchmarks)}, errors={len(self.errors)})"
        )


def _within_fit_cap(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """*results* with every fit summary replaced by a marker if they are too big.

    All or none, rather than the largest few: which routines kept their traces would
    otherwise depend on the order they were walked in, and a chart that appears for
    q0 and not q2 reads as a failure on q2.

    The marker is what lets the card say the traces were dropped rather than show an
    empty chart, and is why nothing here needs a new field on the payload.
    """
    fitted = [r for r in results if r.get("fit") is not None]
    if not fitted:
        return results

    size = sum(len(json.dumps(r["fit"])) for r in fitted)
    if size <= MAX_FIT_PAYLOAD_BYTES:
        return results

    log.warning(
        "dropping %d fit summaries from this report: %.1f MB, over the %.1f MB cap",
        len(fitted),
        size / 1e6,
        MAX_FIT_PAYLOAD_BYTES / 1e6,
    )
    for result in fitted:
        result["fit"] = {"dropped": True}
    return results
