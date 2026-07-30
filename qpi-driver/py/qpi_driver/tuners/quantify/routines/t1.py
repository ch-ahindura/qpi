"""T1 routine."""

from typing import Any

import xarray as xr

from qpi_driver.compat.quantify import Measure, Schedule
from qpi_driver.tuners.base.routines import CalibrationRoutine, register_routine
from qpi_driver.tuners.fitting import fit_t1


@register_routine
class T1(CalibrationRoutine):
    def __init__(self):
        super().__init__(name="t1", depends_on=("rabi",))

    def build_schedule(self, target: str, device: Any, config: Any) -> Any:
        schedule = Schedule("T1", 1)
        schedule.add(Measure(target))
        return schedule

    def analyse(self, dataset: xr.Dataset, target: str, device: Any) -> dict:
        return fit_t1(dataset, target)

    def apply(self, device: Any, target: str, params: dict) -> None:
        pass
