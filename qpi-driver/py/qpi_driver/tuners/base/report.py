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
        """Measured fidelity per target, for the drift check to compare against.

        Where a target was benchmarked by more than one protocol the lowest wins:
        a drift check should trigger on the worst evidence it has, not the best.
        """
        worst: dict[str, float] = {}
        for benchmark in self.benchmarks:
            if benchmark.fidelity is None:
                continue
            current = worst.get(benchmark.target)
            if current is None or benchmark.fidelity < current:
                worst[benchmark.target] = benchmark.fidelity
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
