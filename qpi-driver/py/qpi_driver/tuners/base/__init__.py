from abc import ABC, abstractmethod
from typing import Any

from .config import CalibrationConfig, MonitoringConfig, RoutineConfig
from .report import BenchmarkResult, CalibrationReport, RoutineResult

__all__ = [
    "Tuner",
    "CalibrationConfig",
    "MonitoringConfig",
    "RoutineConfig",
    "CalibrationReport",
    "BenchmarkResult",
    "RoutineResult",
]


class Tuner(ABC):
    def __init__(self, name: str, **kwargs: Any) -> None:
        self.name = name

    @abstractmethod
    def calibrate(self, config: CalibrationConfig) -> CalibrationReport:
        """Run full calibration."""
        ...

    @abstractmethod
    def check_fidelity(self, config: CalibrationConfig) -> dict[str, float]:
        """Check fidelity of qubits."""
        ...

    @abstractmethod
    def recalibrate(
        self, qubits: list[str], config: CalibrationConfig
    ) -> CalibrationReport:
        """Recalibrate specific qubits."""
        ...

    def close(self) -> None:
        """Release resources."""
        pass
