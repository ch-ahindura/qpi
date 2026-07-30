"""Resonator Spectroscopy routine."""

from typing import Any

import numpy as np
import xarray as xr

from qpi_driver.compat.quantify import Measure, Schedule
from qpi_driver.tuners.base.routines import CalibrationRoutine, register_routine
from qpi_driver.tuners.fitting import fit_resonator_spectroscopy


@register_routine
class ResonatorSpectroscopy(CalibrationRoutine):
    def __init__(self):
        super().__init__(name="resonator_spectroscopy")

    def build_schedule(self, target: str, device: Any, config: Any) -> Any:
        frequencies = config.get("frequencies", np.linspace(5e9, 6e9, 101))
        schedule = Schedule("ResonatorSpectroscopy", len(frequencies))
        for i, freq in enumerate(frequencies):
            schedule.add(Measure(target, acq_index=i))
        return schedule

    def analyse(self, dataset: xr.Dataset, target: str, device: Any) -> dict:
        return fit_resonator_spectroscopy(dataset, target)

    def apply(self, device: Any, target: str, params: dict) -> None:
        device.get_element(target).clock_freqs.readout(params.get("frequency"))
