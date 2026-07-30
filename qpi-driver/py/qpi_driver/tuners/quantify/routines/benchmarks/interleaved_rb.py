"""Interleaved RB routine."""

from typing import Any

import xarray as xr

from qpi_driver.compat.quantify import Schedule
from qpi_driver.tuners.base.routines import CalibrationRoutine, register_routine


@register_routine
class InterleavedRB(CalibrationRoutine):
    def __init__(self):
        super().__init__(name="interleaved_rb", targets="edges")

    def build_schedule(self, target: str, device: Any, config: Any) -> Any:
        schedule = Schedule("InterleavedRB", 1)
        return schedule

    def analyse(self, dataset: xr.Dataset, target: str, device: Any) -> dict:
        return {}

    def apply(self, device: Any, target: str, params: dict) -> None:
        pass
