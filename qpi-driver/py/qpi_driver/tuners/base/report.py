"""What a calibration run reports back (RFC 0004 §6.8).

:meth:`CalibrationReport.to_event_payload` is the wire shape of a
``CalibrationResult`` payload, and QPI-UI's ``CalibrationResultPayload`` is its
counterpart. The two are asserted against each other by a test in each language;
they are one contract written twice.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RoutineResult:
    """One routine's outcome on one target."""

    routine_name: str
    target: str
    parameters: dict[str, Any]
    timestamp: str
    duration_s: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "routine_name": self.routine_name,
            "target": self.target,
            "parameters": self.parameters,
            "timestamp": self.timestamp,
            "duration_s": self.duration_s,
        }


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
            "routine_results": [r.to_dict() for r in self.routine_results],
            "benchmarks": [b.to_dict() for b in self.benchmarks],
            "errors": self.errors,
        }

    def summary(self) -> str:
        return (
            f"CalibrationReport(mode={self.mode}, status={self.status}, "
            f"duration={self.duration_s:.1f}s, routines={len(self.routine_results)}, "
            f"benchmarks={len(self.benchmarks)}, errors={len(self.errors)})"
        )
