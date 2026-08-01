"""The ``quantify_tuner`` device: calibration through quantify-scheduler."""

import logging
from pathlib import Path
from typing import Any

import xarray as xr

from qpi_driver.compat.quantify import (
    CZ,
    IS_QUANTIFY_INSTALLED,
    BinMode,
    ClockResource,
    DRAGPulse,
    IdlePulse,
    Instrument,
    Measure,
    Reset,
    Rxy,
    Rz,
    Schedule,
    SerialCompiler,
    SetClockFrequency,
    ShiftClockPhase,
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
    ShiftClockPhase = ShiftClockPhase
    ClockResource = ClockResource

    def drag_pulse(self, *, amp, drag, duration, port, clock, phase_deg=0.0):
        """``D_amp`` is the dimensionless ratio of derivative to Gaussian."""
        return DRAGPulse(
            G_amp=amp,
            D_amp=drag,
            phase=phase_deg % 360.0,
            duration=duration,
            port=port,
            clock=clock,
        )

    BinMode = BinMode
    drag_parameter = "motzoi"
    # Dimensionless: the derivative component as a fraction of the Gaussian.
    drag_span = 0.2

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
        is_simulated: bool = False,
        **kwargs: Any,
    ) -> None:
        """
        Args:
            is_dummy: run against the vendor's dummy cluster. Schedules compile
                and execute, but every acquisition comes back ``nan``, so a
                routine fails rather than fitting anything.
            is_simulated: run against
                :class:`~qpi_driver.simulation.SimulatedCoordinator` instead —
                the compiled schedule is played through a transmon model, so
                routines fit real physics and the calibration written back is a
                calibration of *something*. Needs the ``sim`` dependency group.
                Mutually exclusive with *is_dummy*.
        """
        super().__init__(name, **kwargs)
        if is_dummy and is_simulated:
            raise ValueError(
                "is_dummy and is_simulated both replace the cluster; pick one"
            )
        self._is_dummy = is_dummy
        self._is_simulated = is_simulated

        hardware_config = load_quantify_hardware_config(quantify_hardware_config)
        self._hardware_config = hardware_config
        self._device = load_quantum_device(name=name, config=quantify_device_config)
        self._device.hardware_config(hardware_config)
        if is_simulated:
            from qpi_driver.executors.utils.coupler_bias import (
                declared_parking_currents,
                declared_sideband_gaps,
            )
            from qpi_driver.simulation import SimulatedCoordinator

            self._instrument_coordinator = SimulatedCoordinator(
                kwargs.get("simulator"),
                sideband_gaps=declared_sideband_gaps(self._device),
                parking_currents=declared_parking_currents(self._device),
            )
        else:
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
        """Detach the coordinator's components, then release every instrument.

        ``InstrumentCoordinator.components`` is a qcodes ``ManualParameter``
        holding component *names*, so it has to be called — iterating it
        directly raises, and the shutdown that raises is the one that leaves a
        cluster held open against the next driver that wants it.

        Every step is best-effort, including reading the component list: by the
        time this runs the coordinator may already have been closed.
        """
        try:
            components = list(self._instrument_coordinator.components())
        except Exception:  # noqa: BLE001 - shutdown is best-effort
            components = []

        for name in components:
            try:
                self._instrument_coordinator.remove_component(name)
            except Exception:  # noqa: BLE001 - shutdown is best-effort
                log.debug("could not detach component %s", name)

        # `close_all` closes the components themselves, so detaching them above
        # is enough; looking each one up by name to close it individually would
        # duplicate what the next line does anyway.
        for shutdown in (self._instrument_coordinator.close, Instrument.close_all):
            try:
                shutdown()
            except Exception:  # noqa: BLE001 - shutdown is best-effort
                log.debug("could not run %s", shutdown)
