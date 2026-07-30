"""QubitSpectroscopy implementation."""

from typing import Any

import xarray as xr

from qpi_driver.compat.qblox import TimeableSchedule
from qpi_driver.tuners.base.routines import CalibrationRoutine, register_routine


@register_routine
class QubitSpectroscopy(CalibrationRoutine):
    """QubitSpectroscopy for qblox."""

    def __init__(self):
        super().__init__(name="qubit_spectroscopy")

    def build_schedule(self, target: str, device: Any, config: Any) -> Any:
        schedule = TimeableSchedule(name="qubit_spectroscopy")
        return schedule

    def analyse(self, dataset: xr.Dataset, target: str, device: Any) -> dict[str, Any]:
        return {}

    def apply(self, device: Any, target: str, params: dict[str, Any]) -> None:
        pass
