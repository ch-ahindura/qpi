"""Quantify Tuner implementation."""

import logging
from pathlib import Path
from typing import Any

from qpi_driver.compat.quantify import IS_QUANTIFY_INSTALLED, SerialCompiler
from qpi_driver.executors.quantify.config import load_instrument_coordinator
from qpi_driver.tuners.base import CalibrationConfig, CalibrationReport, Tuner
from qpi_driver.tuners.base.dag import CalibrationDAG
from qpi_driver.tuners.utils.persistence import save_device_config

from .config import load_quantify_hardware_config, load_quantum_device
from .routines import ALL_ROUTINES
from .routines import routine_registry as routine_registry

logger = logging.getLogger(__name__)


class QuantifyTuner(Tuner):
    """Tuner subclass for interacting with Quantify-scheduler."""

    def __new__(cls, *args, **kwargs):
        if not IS_QUANTIFY_INSTALLED:
            raise ImportError(
                "quantify-scheduler is not installed. Install the [quantify] extra to use QuantifyTuner."
            )
        return super().__new__(cls)

    def __init__(
        self,
        name: str = "quantify_tuner",
        quantify_hardware_config: Path | dict | Any = Path("quantify.hardware.json"),
        quantify_device_config: Path | dict = Path("quantify.device.json"),
        is_dummy: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(name, **kwargs)
        self._is_dummy = is_dummy
        hardware_config = load_quantify_hardware_config(quantify_hardware_config)
        self._hardware_config = hardware_config
        self._device = load_quantum_device(name=name, config=quantify_device_config)
        self._instrument_coordinator = load_instrument_coordinator(
            f"{name}_ic", hardware_config=hardware_config, is_dummy=is_dummy
        )
        self._device.hardware_config(hardware_config)
        self._compiler = SerialCompiler(
            name=f"{name}_compiler", quantum_device=self._device
        )
        self._device_config_path = (
            quantify_device_config
            if isinstance(quantify_device_config, Path)
            else Path("quantify.device.json")
        )

    def calibrate(self, config: CalibrationConfig) -> CalibrationReport:
        """Run full calibration."""
        dag = CalibrationDAG(ALL_ROUTINES, config)
        report = dag.run(
            self._device, self._instrument_coordinator, self._compiler, config
        )
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
            compiled = self._compiler.compile(schedule)
            self._instrument_coordinator.prepare(compiled)
            self._instrument_coordinator.start()
            ds = self._instrument_coordinator.retrieve_acquisition()
            res = rb_routine.analyse(ds, q, self._device)
            fidelities[q] = res.get("fidelity", 0.0)
        return fidelities

    def recalibrate(
        self, qubits: list[str], config: CalibrationConfig
    ) -> CalibrationReport:
        """Recalibrate specific qubits."""
        dag = CalibrationDAG(ALL_ROUTINES, config)
        targets = [r.name for r in ALL_ROUTINES]
        _ = dag.partial_order(targets)
        report = dag.run(
            self._device, self._instrument_coordinator, self._compiler, config
        )
        save_device_config(self._device, self._device_config_path)
        return report

    def close(self) -> None:
        """Release resources."""
        for component in self._instrument_coordinator.components:
            self._instrument_coordinator.remove_component(component.name)
            component.close()
        try:
            self._instrument_coordinator.close()
        except Exception:
            pass
