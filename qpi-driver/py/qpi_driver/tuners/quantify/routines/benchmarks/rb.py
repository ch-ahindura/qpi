"""RB routine."""

from typing import Any

import xarray as xr

from qpi_driver.compat.quantify import Measure, Schedule
from qpi_driver.tuners.base.routines import CalibrationRoutine, register_routine
from qpi_driver.tuners.fitting import fit_rb_decay


@register_routine
class RB(CalibrationRoutine):
    def __init__(self):
        super().__init__(name="rb")

    def build_schedule(self, target: str, device: Any, config: Any) -> Any:
        schedule = Schedule("RB", 1)
        schedule.add(Measure(target))
        return schedule

    def analyse(self, dataset: xr.Dataset, target: str, device: Any) -> dict:
        return fit_rb_decay(dataset, target)

    def apply(self, device: Any, target: str, params: dict) -> None:
        pass
