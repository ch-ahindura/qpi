from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Literal

import xarray as xr


@dataclass
class CalibrationRoutine(ABC):
    name: str
    depends_on: tuple[str, ...] = ()
    targets: Literal["qubits", "edges"] = "qubits"

    @abstractmethod
    def build_schedule(self, target: str, device: Any, config: Any) -> Any:
        """Build the scheduler Schedule for this experiment."""
        ...

    @abstractmethod
    def analyse(self, dataset: xr.Dataset, target: str, device: Any) -> dict[str, Any]:
        """Fit raw data and return extracted parameters."""
        ...

    @abstractmethod
    def apply(self, device: Any, target: str, params: dict[str, Any]) -> None:
        """Write extracted parameters back to the QuantumDevice in-memory."""
        ...


_REGISTRY: dict[str, type[CalibrationRoutine]] = {}


def register_routine(cls: type[CalibrationRoutine]) -> type[CalibrationRoutine]:
    """Decorator to register a calibration routine."""
    _REGISTRY[cls.__name__] = cls
    return cls


def routine_registry() -> dict[str, type[CalibrationRoutine]]:
    """Returns all registered routine classes by name."""
    return _REGISTRY
