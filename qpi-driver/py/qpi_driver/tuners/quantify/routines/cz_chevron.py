"""CZ Chevron routine."""

from typing import Any

import xarray as xr

from qpi_driver.compat.quantify import Schedule
from qpi_driver.tuners.base.routines import CalibrationRoutine, register_routine
from qpi_driver.tuners.fitting import fit_chevron


@register_routine
class CZChevron(CalibrationRoutine):
    def __init__(self):
        super().__init__(name="cz_chevron", targets="edges", depends_on=("rabi",))

    def build_schedule(self, target: str, device: Any, config: Any) -> Any:
        schedule = Schedule("CZChevron", 1)
        return schedule

    def analyse(self, dataset: xr.Dataset, target: str, device: Any) -> dict:
        return fit_chevron(dataset, target)

    def apply(self, device: Any, target: str, params: dict) -> None:
        pass
