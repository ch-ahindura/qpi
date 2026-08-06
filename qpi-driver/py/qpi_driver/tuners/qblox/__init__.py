"""The ``qblox_tuner`` device: calibration through qblox-scheduler.

The routines are the same ones ``quantify_tuner`` runs — the two schedulers
share a gate vocabulary, so only the schedule class and the execution path
differ, and both live in :class:`QbloxBackend`.
"""

import logging
from contextlib import suppress
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
from qpi_driver.tuners.base.config import DEFAULT_ROUTINE_TIMEOUT_S

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

    def __init__(self, agent: Any, *, should_save_raw_data: bool = False) -> None:
        self._agent = agent
        self._should_save_raw_data = should_save_raw_data

    def new_schedule(self, name: str, repetitions: int = 1) -> Any:
        return TimeableSchedule(name=name, repetitions=repetitions)

    def run(
        self, schedule: Any, timeout_s: float = DEFAULT_ROUTINE_TIMEOUT_S
    ) -> xr.Dataset:
        # The agent owns compilation and execution together, which is the whole
        # of the difference from quantify's separate compiler and coordinator.
        #
        # Saving is off unless asked for: the agent would otherwise write a dataset
        # and an instrument snapshot per schedule — one per routine per target —
        # that nothing here reads and nothing prunes, and quantify's backend keeps
        # nothing, so the two would differ in what a calibration leaves behind.
        return self._agent.run(
            schedule,
            timeout=int(timeout_s),
            save_to_experiment=self._should_save_raw_data,
            save_snapshot=self._should_save_raw_data,
        )


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
        save_raw_data: bool = False,
        **kwargs: Any,
    ) -> None:
        is_simulated = bool(kwargs.pop("is_simulated", False))
        simulator = kwargs.pop("simulator", None)
        if is_dummy and is_simulated:
            raise ValueError(
                "is_dummy and is_simulated both replace the cluster; pick one"
            )
        super().__init__(name, **kwargs)
        #: Where the SPI rack lives, when the couplers are biased through one.
        #: `coupler_anticrossing` needs to drive it; every other node ignores it.
        self._spi_rack_address = kwargs.get("spi_rack_address")
        self._bias: Any = None
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
                declared_parking_currents,
                declared_sideband_gaps,
            )
            from qpi_driver.simulation.agent import simulated_agent

            self._agent = simulated_agent(
                hardware_configuration=hardware_config,
                quantum_device_configuration=self._device,
                output_dir=self._data_dir,
                simulator=simulator,
                sideband_gaps=declared_sideband_gaps(self._device),
                parking_currents=declared_parking_currents(self._device),
            )
        else:
            self._agent = HardwareAgent(
                hardware_configuration=hardware_config,
                quantum_device_configuration=self._device,
                create_dummy_connections=is_dummy,
                output_dir=self._data_dir,
            )
        self._backend = QbloxBackend(
            self._agent, should_save_raw_data=bool(save_raw_data)
        )

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

    @property
    def bias(self):
        """Whatever can hold this chip's couplers at a DC current. See the quantify twin."""
        if getattr(self, "_bias", None) is not None:
            return self._bias

        # Through the agent: `SimulatedAgent` is what holds the coordinator here,
        # where the quantify tuner holds it directly.
        coordinator = getattr(self._agent, "_coordinator", None)
        if coordinator is not None:
            from qpi_driver.simulation import SimulatedBias

            self._bias = SimulatedBias(coordinator)
            return self._bias

        from qpi_driver.executors.utils.coupler_bias import resolve_bias_source

        try:
            clusters = getattr(self._agent, "get_clusters", lambda: [])()
            self._bias = resolve_bias_source(
                self._device,
                cluster=clusters[0] if clusters else None,
                spi_address=self._spi_rack_address,
                require_current=False,
            )
        except Exception:
            log.exception(
                "could not open a bias source; coupler_anticrossing will fail"
            )
            self._bias = None
        return self._bias

    def _release_bias(self) -> None:
        """Let go of the rack, if one was opened. Best-effort, like every step
        of shutdown: a rack held open outlives this process and blocks the next."""
        source = getattr(self, "_bias", None)
        if source is not None:
            with suppress(Exception):
                source.close()
            self._bias = None

    def close(self) -> None:
        self._release_bias()
        try:
            self._agent.close()
        except Exception:  # noqa: BLE001 - shutdown is best-effort
            log.debug("could not close the hardware agent")
        try:
            Instrument.close_all()
        except Exception:  # noqa: BLE001 - shutdown is best-effort
            log.debug("could not close all instruments")
