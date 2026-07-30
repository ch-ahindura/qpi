from dataclasses import dataclass, field
from typing import Any


@dataclass
class RoutineResult:
    routine_name: str
    target: str
    parameters: dict[str, Any]
    timestamp: str
    duration_s: float


@dataclass
class BenchmarkResult:
    protocol: str
    target: str
    fidelity: float | None
    error_per_gate: float | None
    raw_data: dict[str, Any] = field(default_factory=dict)


@dataclass
class CalibrationReport:
    timestamp: str
    duration_s: float
    mode: str  # 'full', 'partial', 'fidelity_check'
    routine_results: list[RoutineResult] = field(default_factory=list)
    benchmarks: list[BenchmarkResult] = field(default_factory=list)
    status: str = "success"  # 'success', 'partial_failure', 'failed'
    errors: list[str] = field(default_factory=list)

    def add_routine(self, result: RoutineResult) -> None:
        self.routine_results.append(result)

    def add_benchmark(self, benchmark: BenchmarkResult) -> None:
        self.benchmarks.append(benchmark)

    def to_event_payload(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "duration_s": self.duration_s,
            "mode": self.mode,
            "status": self.status,
            "routine_results": [
                {
                    "routine_name": r.routine_name,
                    "target": r.target,
                    "parameters": r.parameters,
                    "timestamp": r.timestamp,
                    "duration_s": r.duration_s,
                }
                for r in self.routine_results
            ],
            "benchmarks": [
                {
                    "protocol": b.protocol,
                    "target": b.target,
                    "fidelity": b.fidelity,
                    "error_per_gate": b.error_per_gate,
                    "raw_data": b.raw_data,
                }
                for b in self.benchmarks
            ],
            "errors": self.errors,
        }

    def summary(self) -> str:
        return f"CalibrationReport(mode={self.mode}, status={self.status}, duration={self.duration_s}s, routines={len(self.routine_results)}, benchmarks={len(self.benchmarks)})"
