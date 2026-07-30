"""Fine Amplitude routine."""

from typing import Any

import xarray as xr

from qpi_driver.compat.quantify import Measure, Schedule
from qpi_driver.tuners.base.routines import CalibrationRoutine, register_routine


@register_routine
class FineAmplitude(CalibrationRoutine):
    def __init__(self):
        super().__init__(name="fine_amplitude", depends_on=("rabi",))

    def build_schedule(self, target: str, device: Any, config: Any) -> Any:
        schedule = Schedule("FineAmplitude", 1)
        schedule.add(Measure(target))
        return schedule

    def analyse(self, dataset: xr.Dataset, target: str, device: Any) -> dict:
        return {"amp180": 0.5}

    def apply(self, device: Any, target: str, params: dict) -> None:
        pass
