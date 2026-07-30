"""Qubit Spectroscopy routine."""

from typing import Any

import xarray as xr

from qpi_driver.compat.quantify import Measure, Schedule
from qpi_driver.tuners.base.routines import CalibrationRoutine, register_routine
from qpi_driver.tuners.fitting import fit_qubit_spectroscopy


@register_routine
class QubitSpectroscopy(CalibrationRoutine):
    def __init__(self):
        super().__init__(
            name="qubit_spectroscopy", depends_on=("resonator_spectroscopy",)
        )

    def build_schedule(self, target: str, device: Any, config: Any) -> Any:
        schedule = Schedule("QubitSpectroscopy", 1)
        schedule.add(Measure(target))
        return schedule

    def analyse(self, dataset: xr.Dataset, target: str, device: Any) -> dict:
        return fit_qubit_spectroscopy(dataset, target)

    def apply(self, device: Any, target: str, params: dict) -> None:
        device.get_element(target).clock_freqs.f01(params.get("frequency"))
