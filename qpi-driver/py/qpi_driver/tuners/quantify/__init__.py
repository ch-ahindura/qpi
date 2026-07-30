"""The ``quantify_tuner`` device: calibration through quantify-scheduler."""

import logging
from pathlib import Path
from typing import Any

import xarray as xr

from qpi_driver.compat.quantify import (
    CZ,
    IS_QUANTIFY_INSTALLED,
    BinMode,
    IdlePulse,
    Instrument,
    Measure,
    Reset,
    Rxy,
    Rz,
    Schedule,
    SerialCompiler,
    SetClockFrequency,
    SquarePulse,
    X,
    Y,
)
from qpi_driver.executors.quantify.config import (
    load_instrument_coordinator,
    load_quantify_hardware_config,
    load_quantum_device,
)
from qpi_driver.tuners.base import SchedulerBackend, Tuner

log = logging.getLogger(__name__)


class QuantifyBackend(SchedulerBackend):
    """quantify-scheduler's operations, and its compile/prepare/retrieve cycle."""

    name = "quantify"

    Schedule = Schedule
    Reset = Reset
    Measure = Measure
    Rxy = Rxy
    X = X
    Y = Y
    Rz = Rz
    CZ = CZ
    IdlePulse = IdlePulse
    SquarePulse = SquarePulse
    SetClockFrequency = SetClockFrequency
    BinMode = BinMode
    drag_parameter = "motzoi"

    def __init__(self, compiler: Any, instrument_coordinator: Any) -> None:
        self._compiler = compiler
        self._instrument_coordinator = instrument_coordinator

    def new_schedule(self, name: str, repetitions: int = 1) -> Any:
        return Schedule(name, repetitions=repetitions)

    def run(self, schedule: Any) -> xr.Dataset:
        compiled = self._compiler.compile(schedule)
        self._instrument_coordinator.prepare(compiled)
        self._instrument_coordinator.start()
        self._instrument_coordinator.wait_done(timeout_sec=60)
        return self._instrument_coordinator.retrieve_acquisition()


class QuantifyTuner(Tuner):
    """Calibrates a transmon chip through quantify-scheduler."""

    def __new__(cls, *args: Any, **kwargs: Any) -> "QuantifyTuner":
        if not IS_QUANTIFY_INSTALLED:
            raise ImportError(
                "quantify-scheduler is not installed. Install the [quantify_tuner] "
                "extra to use QuantifyTuner."
            )
        return super().__new__(cls)

    def __init__(
        self,
        name: str = "quantify_tuner",
        quantify_hardware_config: Path | dict | Any = Path("quantify.hardware.json"),
        quantify_device_config: Path | dict = Path("quantify.device.yml"),
        is_dummy: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(name, **kwargs)
        self._is_dummy = is_dummy

        hardware_config = load_quantify_hardware_config(quantify_hardware_config)
        self._hardware_config = hardware_config
        self._device = load_quantum_device(name=name, config=quantify_device_config)
        self._device.hardware_config(hardware_config)
        self._instrument_coordinator = load_instrument_coordinator(
            f"{name}_ic", hardware_config=hardware_config, is_dummy=is_dummy
        )
        self._compiler = SerialCompiler(
            name=f"{name}_compiler", quantum_device=self._device
        )
        self._backend = QuantifyBackend(self._compiler, self._instrument_coordinator)

        # Only a path can be written back to. A device handed over as a dict came
        # from somewhere this tuner does not know, so there is nothing to update.
        self._device_config_path = (
            Path(quantify_device_config)
            if isinstance(quantify_device_config, (str, Path))
            else None
        )

    @property
    def backend(self) -> SchedulerBackend:
        return self._backend

    @property
    def device(self) -> Any:
        return self._device

    def close(self) -> None:
        # Mirrors QuantifyExecutor.close: components are detached and closed
        # individually before the coordinator itself, and every step is
        # best-effort — a shutdown that raises leaves instruments held open.
        for component in list(getattr(self._instrument_coordinator, "components", [])):
            try:
                self._instrument_coordinator.remove_component(component.name)
                component.close()
            except Exception:  # noqa: BLE001 - shutdown is best-effort
                log.debug("could not close component %s", component)
        for shutdown in (self._instrument_coordinator.close, Instrument.close_all):
            try:
                shutdown()
            except Exception:  # noqa: BLE001 - shutdown is best-effort
                log.debug("could not run %s", shutdown)
