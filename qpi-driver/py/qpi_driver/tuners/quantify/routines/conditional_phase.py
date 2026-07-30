"""Conditional Phase routine."""

from typing import Any

import xarray as xr

from qpi_driver.compat.quantify import Schedule
from qpi_driver.tuners.base.routines import CalibrationRoutine, register_routine


@register_routine
class ConditionalPhase(CalibrationRoutine):
    def __init__(self):
        super().__init__(
            name="conditional_phase", targets="edges", depends_on=("cz_chevron",)
        )

    def build_schedule(self, target: str, device: Any, config: Any) -> Any:
        schedule = Schedule("ConditionalPhase", 1)
        return schedule

    def analyse(self, dataset: xr.Dataset, target: str, device: Any) -> dict:
        return {}

    def apply(self, device: Any, target: str, params: dict) -> None:
        pass
