from abc import ABC, abstractmethod
from typing import Any

import xarray as xr

from .dtos import CircuitPayload, JobPayload

__all__ = ["Executor", "CircuitPayload", "JobPayload"]


class Executor(ABC):
    def __init__(self, name: str = "executor", **kwargs: Any) -> None:
        self.name = name

    @abstractmethod
    def execute(self, payload: JobPayload) -> xr.Dataset:
        """Execute the quantum circuit/instructions payload.

        Args:
            payload: JobPayload object containing circuit QASM, qubit count, shots, etc.

        Returns:
            xr.Dataset: Dataset mimicking the raw measurement counts and frequencies.
        """
        ...

    @abstractmethod
    def process_result(self, dataset: xr.Dataset, job_id: str) -> dict:
        """Convert a raw xr.Dataset from execute() into a Qiskit-compatible result dict.

        Each executor knows its own data format and how to:
        - Perform state discrimination (if meas_level=2)
        - Return IQ memory (if meas_level=1)
        - Return raw traces (if meas_level=0)
        - Handle meas_return averaging

        Args:
            dataset: The xr.Dataset returned by execute().
            job_id: The unique ID of the quantum job.

        Returns:
            dict: Qiskit-compatible result dict with keys like 'counts', 'memory',
                  'shots', 'backend', 'success', etc.
        """
        ...

    def close(self) -> None:
        """Release resources."""
        pass
