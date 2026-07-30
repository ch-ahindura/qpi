"""Resonator Punchout routine."""

from typing import Any

import xarray as xr

from qpi_driver.compat.quantify import Measure, Schedule
from qpi_driver.tuners.base.routines import CalibrationRoutine, register_routine


@register_routine
class ResonatorPunchout(CalibrationRoutine):
    def __init__(self):
        super().__init__(
            name="resonator_punchout", depends_on=("resonator_spectroscopy",)
        )

    def build_schedule(self, target: str, device: Any, config: Any) -> Any:
        schedule = Schedule("ResonatorPunchout", 1)
        schedule.add(Measure(target))
        return schedule

    def analyse(self, dataset: xr.Dataset, target: str, device: Any) -> dict:
        return {"power": 0.5, "frequency": 5e9}

    def apply(self, device: Any, target: str, params: dict) -> None:
        pass
