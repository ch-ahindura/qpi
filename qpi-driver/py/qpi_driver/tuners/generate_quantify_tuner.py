from pathlib import Path

base = Path(
    "/Users/martinahindura/work/code/sopherapps/open-source/qpi/qpi-driver/py/qpi_driver/tuners/quantify"
)
base.mkdir(parents=True, exist_ok=True)
(base / "elements").mkdir(parents=True, exist_ok=True)
(base / "routines").mkdir(parents=True, exist_ok=True)
(base / "routines" / "benchmarks").mkdir(parents=True, exist_ok=True)

with open(base / "config.py", "w") as f:
    f.write('''"""Quantify Tuner config loading."""
from qpi_driver.executors.quantify.config import (
    load_quantify_hardware_config,
    load_quantum_device,
)

__all__ = ["load_quantify_hardware_config", "load_quantum_device"]
''')

with open(base / "__init__.py", "w") as f:
    f.write('''"""Quantify Tuner implementation."""

import logging
from pathlib import Path
from typing import Any

from qpi_driver.compat.quantify import IS_QUANTIFY_INSTALLED, SerialCompiler
from qpi_driver.executors.quantify.config import load_instrument_coordinator
from qpi_driver.tuners.base import CalibrationConfig, CalibrationReport, Tuner
from qpi_driver.tuners.base.dag import CalibrationDAG
from qpi_driver.tuners.utils.persistence import save_device_config
from .config import load_quantify_hardware_config, load_quantum_device
from .routines import ALL_ROUTINES, routine_registry

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
        self._device_config_path = quantify_device_config if isinstance(quantify_device_config, Path) else Path("quantify.device.json")

    def calibrate(self, config: CalibrationConfig) -> CalibrationReport:
        """Run full calibration."""
        dag = CalibrationDAG(ALL_ROUTINES, config)
        report = dag.run(self._device, self._instrument_coordinator, self._compiler, config)
        save_device_config(self._device, self._device_config_path)
        return report

    def check_fidelity(self, config: CalibrationConfig) -> dict[str, float]:
        """Check fidelity of qubits."""
        fidelities = {}
        # Simple fidelity check via RB
        rb_routine = next(r for r in ALL_ROUTINES if r.name == "rb")
        for q in config.target_qubits:
            schedule = rb_routine.build_schedule(q, self._device, config.get_routine("rb"))
            compiled = self._compiler.compile(schedule)
            self._instrument_coordinator.prepare(compiled)
            self._instrument_coordinator.start()
            ds = self._instrument_coordinator.retrieve_acquisition()
            res = rb_routine.analyse(ds, q, self._device)
            fidelities[q] = res.get("fidelity", 0.0)
        return fidelities

    def recalibrate(self, qubits: list[str], config: CalibrationConfig) -> CalibrationReport:
        """Recalibrate specific qubits."""
        dag = CalibrationDAG(ALL_ROUTINES, config)
        targets = [r.name for r in ALL_ROUTINES]
        partial = dag.partial_order(targets)
        report = dag.run(self._device, self._instrument_coordinator, self._compiler, config)
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
''')

with open(base / "elements" / "__init__.py", "w") as f:
    f.write('""""""\n')


with open(base / "routines" / "__init__.py", "w") as f:
    f.write('''"""Routines for Quantify Tuner."""

from .resonator_spectroscopy import ResonatorSpectroscopy
from .resonator_punchout import ResonatorPunchout
from .qubit_spectroscopy import QubitSpectroscopy
from .rabi import Rabi
from .ramsey import Ramsey
from .t1 import T1
from .t2_echo import T2Echo
from .drag import DRAG
from .allxy import AllXY
from .fine_amplitude import FineAmplitude
from .flux_spectroscopy import FluxSpectroscopy
from .cz_chevron import CZChevron
from .conditional_phase import ConditionalPhase

from .benchmarks.rb import RB
from .benchmarks.interleaved_rb import InterleavedRB
from .benchmarks.allxy_check import AllXYCheck

ALL_ROUTINES = [
    ResonatorSpectroscopy(),
    ResonatorPunchout(),
    QubitSpectroscopy(),
    Rabi(),
    Ramsey(),
    T1(),
    T2Echo(),
    DRAG(),
    AllXY(),
    FineAmplitude(),
    FluxSpectroscopy(),
    CZChevron(),
    ConditionalPhase(),
    RB(),
    InterleavedRB(),
    AllXYCheck(),
]

def routine_registry() -> dict:
    return {r.name: r for r in ALL_ROUTINES}
''')

with open(base / "routines" / "resonator_spectroscopy.py", "w") as f:
    f.write('''"""Resonator Spectroscopy routine."""
import numpy as np
import xarray as xr
from qpi_driver.compat.quantify import Schedule, Measure, ShiftClockPhase
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
''')

with open(base / "routines" / "resonator_punchout.py", "w") as f:
    f.write('''"""Resonator Punchout routine."""
import xarray as xr
from typing import Any
from qpi_driver.compat.quantify import Schedule, Measure
from qpi_driver.tuners.base.routines import CalibrationRoutine, register_routine
from qpi_driver.tuners.fitting import fit_resonator_spectroscopy

@register_routine
class ResonatorPunchout(CalibrationRoutine):
    def __init__(self):
        super().__init__(name="resonator_punchout", depends_on=("resonator_spectroscopy",))

    def build_schedule(self, target: str, device: Any, config: Any) -> Any:
        schedule = Schedule("ResonatorPunchout", 1)
        schedule.add(Measure(target))
        return schedule

    def analyse(self, dataset: xr.Dataset, target: str, device: Any) -> dict:
        return {"power": 0.5, "frequency": 5e9}

    def apply(self, device: Any, target: str, params: dict) -> None:
        pass
''')

with open(base / "routines" / "qubit_spectroscopy.py", "w") as f:
    f.write('''"""Qubit Spectroscopy routine."""
import xarray as xr
from typing import Any
from qpi_driver.compat.quantify import Schedule, Measure, Rxy, SquarePulse
from qpi_driver.tuners.base.routines import CalibrationRoutine, register_routine
from qpi_driver.tuners.fitting import fit_qubit_spectroscopy

@register_routine
class QubitSpectroscopy(CalibrationRoutine):
    def __init__(self):
        super().__init__(name="qubit_spectroscopy", depends_on=("resonator_spectroscopy",))

    def build_schedule(self, target: str, device: Any, config: Any) -> Any:
        schedule = Schedule("QubitSpectroscopy", 1)
        schedule.add(Measure(target))
        return schedule

    def analyse(self, dataset: xr.Dataset, target: str, device: Any) -> dict:
        return fit_qubit_spectroscopy(dataset, target)

    def apply(self, device: Any, target: str, params: dict) -> None:
        device.get_element(target).clock_freqs.f01(params.get("frequency"))
''')

with open(base / "routines" / "rabi.py", "w") as f:
    f.write('''"""Rabi routine."""
import xarray as xr
from typing import Any
from qpi_driver.compat.quantify import Schedule, Measure, Rxy
from qpi_driver.tuners.base.routines import CalibrationRoutine, register_routine
from qpi_driver.tuners.fitting import fit_rabi

@register_routine
class Rabi(CalibrationRoutine):
    def __init__(self):
        super().__init__(name="rabi", depends_on=("qubit_spectroscopy",))

    def build_schedule(self, target: str, device: Any, config: Any) -> Any:
        schedule = Schedule("Rabi", 1)
        schedule.add(Rxy(90, 0, target))
        schedule.add(Measure(target))
        return schedule

    def analyse(self, dataset: xr.Dataset, target: str, device: Any) -> dict:
        return fit_rabi(dataset, target)

    def apply(self, device: Any, target: str, params: dict) -> None:
        pass
''')

with open(base / "routines" / "ramsey.py", "w") as f:
    f.write('''"""Ramsey routine."""
import xarray as xr
from typing import Any
from qpi_driver.compat.quantify import Schedule, Measure, Rxy
from qpi_driver.tuners.base.routines import CalibrationRoutine, register_routine
from qpi_driver.tuners.fitting import fit_ramsey

@register_routine
class Ramsey(CalibrationRoutine):
    def __init__(self):
        super().__init__(name="ramsey", depends_on=("rabi",))

    def build_schedule(self, target: str, device: Any, config: Any) -> Any:
        schedule = Schedule("Ramsey", 1)
        schedule.add(Measure(target))
        return schedule

    def analyse(self, dataset: xr.Dataset, target: str, device: Any) -> dict:
        return fit_ramsey(dataset, target)

    def apply(self, device: Any, target: str, params: dict) -> None:
        pass
''')

with open(base / "routines" / "t1.py", "w") as f:
    f.write('''"""T1 routine."""
import xarray as xr
from typing import Any
from qpi_driver.compat.quantify import Schedule, Measure, Rxy
from qpi_driver.tuners.base.routines import CalibrationRoutine, register_routine
from qpi_driver.tuners.fitting import fit_t1

@register_routine
class T1(CalibrationRoutine):
    def __init__(self):
        super().__init__(name="t1", depends_on=("rabi",))

    def build_schedule(self, target: str, device: Any, config: Any) -> Any:
        schedule = Schedule("T1", 1)
        schedule.add(Measure(target))
        return schedule

    def analyse(self, dataset: xr.Dataset, target: str, device: Any) -> dict:
        return fit_t1(dataset, target)

    def apply(self, device: Any, target: str, params: dict) -> None:
        pass
''')

with open(base / "routines" / "t2_echo.py", "w") as f:
    f.write('''"""T2 Echo routine."""
import xarray as xr
from typing import Any
from qpi_driver.compat.quantify import Schedule, Measure, Rxy
from qpi_driver.tuners.base.routines import CalibrationRoutine, register_routine
from qpi_driver.tuners.fitting import fit_t2

@register_routine
class T2Echo(CalibrationRoutine):
    def __init__(self):
        super().__init__(name="t2_echo", depends_on=("t1",))

    def build_schedule(self, target: str, device: Any, config: Any) -> Any:
        schedule = Schedule("T2Echo", 1)
        schedule.add(Measure(target))
        return schedule

    def analyse(self, dataset: xr.Dataset, target: str, device: Any) -> dict:
        return fit_t2(dataset, target)

    def apply(self, device: Any, target: str, params: dict) -> None:
        pass
''')

with open(base / "routines" / "drag.py", "w") as f:
    f.write('''"""DRAG routine."""
import xarray as xr
from typing import Any
from qpi_driver.compat.quantify import Schedule, Measure, Rxy
from qpi_driver.tuners.base.routines import CalibrationRoutine, register_routine

@register_routine
class DRAG(CalibrationRoutine):
    def __init__(self):
        super().__init__(name="drag", depends_on=("rabi",))

    def build_schedule(self, target: str, device: Any, config: Any) -> Any:
        schedule = Schedule("DRAG", 1)
        schedule.add(Measure(target))
        return schedule

    def analyse(self, dataset: xr.Dataset, target: str, device: Any) -> dict:
        return {"motzoi": 0.1}

    def apply(self, device: Any, target: str, params: dict) -> None:
        pass
''')

with open(base / "routines" / "allxy.py", "w") as f:
    f.write('''"""AllXY routine."""
import xarray as xr
from typing import Any
from qpi_driver.compat.quantify import Schedule, Measure, Rxy
from qpi_driver.tuners.base.routines import CalibrationRoutine, register_routine

@register_routine
class AllXY(CalibrationRoutine):
    def __init__(self):
        super().__init__(name="allxy", depends_on=("rabi",))

    def build_schedule(self, target: str, device: Any, config: Any) -> Any:
        schedule = Schedule("AllXY", 1)
        schedule.add(Measure(target))
        return schedule

    def analyse(self, dataset: xr.Dataset, target: str, device: Any) -> dict:
        return {}

    def apply(self, device: Any, target: str, params: dict) -> None:
        pass
''')

with open(base / "routines" / "fine_amplitude.py", "w") as f:
    f.write('''"""Fine Amplitude routine."""
import xarray as xr
from typing import Any
from qpi_driver.compat.quantify import Schedule, Measure, Rxy
from qpi_driver.tuners.base.routines import CalibrationRoutine, register_routine

@register_routine
class FineAmplitude(CalibrationRoutine):
    def __init__(self):
        super().__init__(name="fine_amplitude", depends_on=("rabi",))

    def build_schedule(self, target: str, device: Any, config: Any) -> Any:
        schedule = Schedule("FineAmplitude", 1)
        schedule.add(Measure(target))
        return schedule

    def analyse(self, dataset: xr.Dataset, target: str, device: Any) -> dict:
        return {"amp180": 0.5}

    def apply(self, device: Any, target: str, params: dict) -> None:
        pass
''')

with open(base / "routines" / "flux_spectroscopy.py", "w") as f:
    f.write('''"""Flux Spectroscopy routine."""
import xarray as xr
from typing import Any
from qpi_driver.compat.quantify import Schedule, Measure, Rxy
from qpi_driver.tuners.base.routines import CalibrationRoutine, register_routine

@register_routine
class FluxSpectroscopy(CalibrationRoutine):
    def __init__(self):
        super().__init__(name="flux_spectroscopy", targets="edges", depends_on=("resonator_spectroscopy",))

    def build_schedule(self, target: str, device: Any, config: Any) -> Any:
        schedule = Schedule("FluxSpectroscopy", 1)
        return schedule

    def analyse(self, dataset: xr.Dataset, target: str, device: Any) -> dict:
        return {}

    def apply(self, device: Any, target: str, params: dict) -> None:
        pass
''')

with open(base / "routines" / "cz_chevron.py", "w") as f:
    f.write('''"""CZ Chevron routine."""
import xarray as xr
from typing import Any
from qpi_driver.compat.quantify import Schedule, Measure, CZ
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
''')

with open(base / "routines" / "conditional_phase.py", "w") as f:
    f.write('''"""Conditional Phase routine."""
import xarray as xr
from typing import Any
from qpi_driver.compat.quantify import Schedule, Measure, CZ
from qpi_driver.tuners.base.routines import CalibrationRoutine, register_routine

@register_routine
class ConditionalPhase(CalibrationRoutine):
    def __init__(self):
        super().__init__(name="conditional_phase", targets="edges", depends_on=("cz_chevron",))

    def build_schedule(self, target: str, device: Any, config: Any) -> Any:
        schedule = Schedule("ConditionalPhase", 1)
        return schedule

    def analyse(self, dataset: xr.Dataset, target: str, device: Any) -> dict:
        return {}

    def apply(self, device: Any, target: str, params: dict) -> None:
        pass
''')

with open(base / "routines" / "benchmarks" / "__init__.py", "w") as f:
    f.write('""""""\n')

with open(base / "routines" / "benchmarks" / "rb.py", "w") as f:
    f.write('''"""RB routine."""
import xarray as xr
from typing import Any
from qpi_driver.compat.quantify import Schedule, Measure
from qpi_driver.tuners.base.routines import CalibrationRoutine, register_routine
from qpi_driver.tuners.fitting import fit_rb_decay

@register_routine
class RB(CalibrationRoutine):
    def __init__(self):
        super().__init__(name="rb")

    def build_schedule(self, target: str, device: Any, config: Any) -> Any:
        schedule = Schedule("RB", 1)
        schedule.add(Measure(target))
        return schedule

    def analyse(self, dataset: xr.Dataset, target: str, device: Any) -> dict:
        return fit_rb_decay(dataset, target)

    def apply(self, device: Any, target: str, params: dict) -> None:
        pass
''')

with open(base / "routines" / "benchmarks" / "interleaved_rb.py", "w") as f:
    f.write('''"""Interleaved RB routine."""
import xarray as xr
from typing import Any
from qpi_driver.compat.quantify import Schedule, Measure, CZ
from qpi_driver.tuners.base.routines import CalibrationRoutine, register_routine

@register_routine
class InterleavedRB(CalibrationRoutine):
    def __init__(self):
        super().__init__(name="interleaved_rb", targets="edges")

    def build_schedule(self, target: str, device: Any, config: Any) -> Any:
        schedule = Schedule("InterleavedRB", 1)
        return schedule

    def analyse(self, dataset: xr.Dataset, target: str, device: Any) -> dict:
        return {}

    def apply(self, device: Any, target: str, params: dict) -> None:
        pass
''')

with open(base / "routines" / "benchmarks" / "allxy_check.py", "w") as f:
    f.write('''"""AllXY Check routine."""
import xarray as xr
from typing import Any
from qpi_driver.compat.quantify import Schedule, Measure
from qpi_driver.tuners.base.routines import CalibrationRoutine, register_routine

@register_routine
class AllXYCheck(CalibrationRoutine):
    def __init__(self):
        super().__init__(name="allxy_check")

    def build_schedule(self, target: str, device: Any, config: Any) -> Any:
        schedule = Schedule("AllXYCheck", 1)
        schedule.add(Measure(target))
        return schedule

    def analyse(self, dataset: xr.Dataset, target: str, device: Any) -> dict:
        return {}

    def apply(self, device: Any, target: str, params: dict) -> None:
        pass
''')

print("All files created.")
