"""AllXY routine."""

from typing import Any

import xarray as xr

from qpi_driver.compat.quantify import Measure, Schedule
from qpi_driver.tuners.base.routines import CalibrationRoutine, register_routine


@register_routine
class AllXY(CalibrationRoutine):
    def __init__(self):
        super().__init__(name="allxy", depends_on=("rabi",))

    def build_schedule(self, target: str, device: Any, config: Any) -> Any:
        schedule = Schedule("AllXY", 1)
        schedule.add(Measure(target))
        return schedule

    def analyse(self, dataset: xr.Dataset, target: str, device: Any) -> dict:
        return {}

    def apply(self, device: Any, target: str, params: dict) -> None:
        pass
