"""The ``qblox_tuner`` device: calibration through qblox-scheduler.

The routines are the same ones ``quantify_tuner`` runs — the two schedulers
share a gate vocabulary, so only the schedule class and the execution path
differ, and both live in :class:`QbloxBackend`.
"""

import logging
from pathlib import Path
from typing import Any

import xarray as xr

from qpi_driver.compat.qblox import (
    CZ,
    IS_QBLOX_SCHEDULER_INSTALLED,
    BinMode,
    ClockResource,
    DRAGPulse,
    HardwareAgent,
    IdlePulse,
    Instrument,
    Measure,
    Reset,
    Rxy,
    Rz,
    SetClockFrequency,
    ShiftClockPhase,
    SquarePulse,
    TimeableSchedule,
    X,
    Y,
)
from qpi_driver.executors.qblox.config import (
    load_quantify_hardware_config,
    load_quantum_device,
)
from qpi_driver.tuners.base import SchedulerBackend, Tuner

log = logging.getLogger(__name__)


class QbloxBackend(SchedulerBackend):
    """qblox-scheduler's operations, run through a ``HardwareAgent``."""

    name = "qblox"

    Schedule = TimeableSchedule
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
        """``beta`` is in seconds — one pulse sigma larger than quantify's ratio."""
        return DRAGPulse(
            amplitude=amp,
            beta=drag,
            phase=phase_deg % 360.0,
            duration=duration,
            port=port,
            clock=clock,
        )

    BinMode = BinMode
    drag_parameter = "beta"
    # Seconds: qblox divides the derivative by sigma squared where quantify
    # divides by sigma, so the same pulse is one sigma larger here.
    drag_span = 5e-10

    def __init__(self, agent: Any) -> None:
        self._agent = agent

    def new_schedule(self, name: str, repetitions: int = 1) -> Any:
        return TimeableSchedule(name=name, repetitions=repetitions)

    def run(self, schedule: Any) -> xr.Dataset:
        # The agent owns compilation and execution together, which is the whole
        # of the difference from quantify's separate compiler and coordinator.
        return self._agent.run(schedule)


class QbloxTuner(Tuner):
    """Calibrates a transmon chip through qblox-scheduler."""

    def __new__(cls, *args: Any, **kwargs: Any) -> "QbloxTuner":
        if not IS_QBLOX_SCHEDULER_INSTALLED:
            raise ImportError(
                "qblox-scheduler is not installed. Install the [qblox_tuner] "
                "extra to use QbloxTuner."
            )
        return super().__new__(cls)

    def __init__(
        self,
        name: str = "qblox_tuner",
        quantify_hardware_config: Path | dict | Any = Path("quantify.hardware.json"),
        quantify_device_config: Path | dict = Path("quantify.device.yml"),
        is_dummy: bool = False,
        data_dir: Path = Path("bin/data"),
        **kwargs: Any,
    ) -> None:
        is_simulated = bool(kwargs.pop("is_simulated", False))
        simulator = kwargs.pop("simulator", None)
        if is_dummy and is_simulated:
            raise ValueError(
                "is_dummy and is_simulated both replace the cluster; pick one"
            )
        super().__init__(name, **kwargs)
        self._is_dummy = is_dummy
        self._is_simulated = is_simulated
        self._data_dir = Path(data_dir)

        hardware_config = load_quantify_hardware_config(quantify_hardware_config)
        self._hardware_config = hardware_config
        self._device = load_quantum_device(name=name, config=quantify_device_config)
        if is_simulated:
            # The agent's `compile` is the only part a chip is not needed for,
            # so that half stays real and the simulator takes over execution.
            from qpi_driver.executors.utils.coupler_bias import (
                declared_sideband_gaps,
            )
            from qpi_driver.simulation.agent import simulated_agent

            self._agent = simulated_agent(
                hardware_configuration=hardware_config,
                quantum_device_configuration=self._device,
                output_dir=self._data_dir,
                simulator=simulator,
                sideband_gaps=declared_sideband_gaps(self._device),
            )
        else:
            self._agent = HardwareAgent(
                hardware_configuration=hardware_config,
                quantum_device_configuration=self._device,
                create_dummy_connections=is_dummy,
                output_dir=self._data_dir,
            )
        self._backend = QbloxBackend(self._agent)

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
        try:
            self._agent.close()
        except Exception:  # noqa: BLE001 - shutdown is best-effort
            log.debug("could not close the hardware agent")
        try:
            Instrument.close_all()
        except Exception:  # noqa: BLE001 - shutdown is best-effort
            log.debug("could not close all instruments")
