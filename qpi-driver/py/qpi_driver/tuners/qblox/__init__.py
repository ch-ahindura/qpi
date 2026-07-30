"""Qblox Tuner implementation."""

import logging
from pathlib import Path
from typing import Any

from qpi_driver.compat.qblox import IS_QBLOX_SCHEDULER_INSTALLED, HardwareAgent
from qpi_driver.executors.qblox.config import (
    load_quantify_hardware_config,
    load_quantum_device,
)
from qpi_driver.tuners.base import CalibrationConfig, CalibrationReport, Tuner
from qpi_driver.tuners.base.dag import CalibrationDAG
from qpi_driver.tuners.utils.persistence import save_device_config

from .routines import ALL_ROUTINES

logger = logging.getLogger(__name__)


class QbloxTuner(Tuner):
    """Tuner subclass for interacting with qblox-scheduler."""

    def __new__(cls, *args, **kwargs):
        if not IS_QBLOX_SCHEDULER_INSTALLED:
            raise ImportError(
                "qblox-scheduler is not installed. Install the [qblox] extra to use QbloxTuner."
            )
        return super().__new__(cls)

    def __init__(
        self,
        name: str = "qblox_tuner",
        quantify_hardware_config: Path | dict | Any = Path("quantify.hardware.json"),
        quantify_device_config: Path | dict = Path("quantify.device.json"),
        is_dummy: bool = False,
        data_dir: Path = Path("data"),
        **kwargs: Any,
    ) -> None:
        super().__init__(name, **kwargs)
        self._is_dummy = is_dummy
        self._data_dir = data_dir

        hardware_config = load_quantify_hardware_config(quantify_hardware_config)
        self._hardware_config = hardware_config
        self._device = load_quantum_device(name=name, config=quantify_device_config)

        self._agent = HardwareAgent(
            hardware_configuration=hardware_config,
            quantum_device_configuration=self._device,
            create_dummy_connections=is_dummy,
            output_dir=data_dir,
        )
        self._device_config_path = (
            quantify_device_config
            if isinstance(quantify_device_config, Path)
            else Path("quantify.device.json")
        )

    def calibrate(self, config: CalibrationConfig) -> CalibrationReport:
        """Run full calibration."""
        dag = CalibrationDAG(ALL_ROUTINES, config)
        report = dag.run(self._device, self._agent, None, config)
        save_device_config(self._device, self._device_config_path)
        return report

    def check_fidelity(self, config: CalibrationConfig) -> dict[str, float]:
        """Check fidelity of qubits."""
        fidelities = {}
        # Simple fidelity check via RB
        rb_routine = next(r for r in ALL_ROUTINES if r.name == "rb")
        for q in config.target_qubits:
            schedule = rb_routine.build_schedule(
                q, self._device, config.get_routine("rb")
            )
            ds = self._agent.run(schedule)
            res = rb_routine.analyse(ds, q, self._device)
            fidelities[q] = res.get("fidelity", 0.0)
        return fidelities

    def recalibrate(
        self, qubits: list[str], config: CalibrationConfig
    ) -> CalibrationReport:
        """Recalibrate specific qubits."""
        dag = CalibrationDAG(ALL_ROUTINES, config)
        report = dag.run(self._device, self._agent, None, config)
        save_device_config(self._device, self._device_config_path)
        return report

    def close(self) -> None:
        """Release resources."""
        try:
            self._agent.instrument_coordinator.close()
        except Exception:
            pass
