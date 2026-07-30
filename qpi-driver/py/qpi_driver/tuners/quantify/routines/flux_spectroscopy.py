"""Flux Spectroscopy routine."""

from typing import Any

import xarray as xr

from qpi_driver.compat.quantify import Schedule
from qpi_driver.tuners.base.routines import CalibrationRoutine, register_routine


@register_routine
class FluxSpectroscopy(CalibrationRoutine):
    def __init__(self):
        super().__init__(
            name="flux_spectroscopy",
            targets="edges",
            depends_on=("resonator_spectroscopy",),
        )

    def build_schedule(self, target: str, device: Any, config: Any) -> Any:
        schedule = Schedule("FluxSpectroscopy", 1)
        return schedule

    def analyse(self, dataset: xr.Dataset, target: str, device: Any) -> dict:
        return {}

    def apply(self, device: Any, target: str, params: dict) -> None:
        pass
