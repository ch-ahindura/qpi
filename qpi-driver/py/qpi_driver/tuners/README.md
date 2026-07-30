# QPI Calibration Tuners Framework

The calibration tuners framework enables you to automatically execute calibration experiments to track and fix parameter drift in your quantum hardware. This executes under the `calibrate` operation using a `quantify_tuner` or `qblox_tuner` device.

## Overview

Unlike a typical executor that receives independent circuits, a calibration tuner executes a Directed Acyclic Graph (DAG) of *calibration routines*. These routines have dependencies (e.g., you must calibrate the resonator frequency before measuring the qubit frequency). 

The `calibrate` device performs the following process:
1. Receives a `CalibrationConfig` indicating which routines are enabled and what qubits/edges to target.
2. Compiles a topological sort of the enabled routines using the DAG.
3. For each routine in topological order:
   - Compiles a pulse schedule or quantum circuit (via `quantify-scheduler` or `qblox-scheduler`).
   - Executes the schedule against the quantum hardware.
   - Fits the resulting data to extract updated parameters.
   - Immediately updates the local device configuration (e.g. `quantify.device.yml`) with the new parameters.
4. Optionally executes fidelity benchmark routines (e.g. Randomized Benchmarking) at the end.
5. Emits a `CalibrationResult` event with all fitted parameters, benchmark metrics, and execution durations to the server.

## Installation

```bash
# For quantify-scheduler tuners
pip install "qpi-driver[quantify_tuner]"

# For qblox-scheduler tuners
pip install "qpi-driver[qblox_tuner]"
```

## Running the Driver

Run the driver using the `start` command, setting the operation to `calibrate` and the device to your desired tuner (`quantify_tuner` or `qblox_tuner`).

```bash
qpi-driver start --operation calibrate --device quantify_tuner \
  --token "<your-access-token>" \
  --ca-fingerprint "<fingerprint>" \
  -o quantify_device_config=quantify.device.yml \
  -o quantify_hardware_config=quantify.hardware.json \
  -o calibration_config=calibration.yml
```

### Options

*   `quantify_device_config`: Path to your Qblox/Quantify device configuration YAML file. The tuner **will modify this file in-place** as it fits new parameters during calibration.
*   `quantify_hardware_config`: Path to your hardware compilation configuration JSON file.
*   `calibration_config`: (Optional) Path to your `calibration.yml` file. Defaults to `calibration.yml` in the current working directory.

## Configuration (calibration.yml)

The calibration run is controlled by a `calibration.yml` file. This specifies the targets (qubits and edges) and configure which routines are enabled and what parameters they use.

```yaml
target_qubits:
  - "q0"
  - "q1"
target_edges:
  - "q0-q1"

monitoring:
  rb_depths: [1, 2, 4, 8, 16, 32]
  n_circuits_per_depth: 20
  shots: 1024
  allxy_as_smoke_test: true

routines:
  resonator_spectroscopy:
    enabled: true
    params:
      frequencies: [5.0e9, 5.1e9, 5.2e9]
  qubit_spectroscopy:
    enabled: true
    params:
      frequencies: [6.0e9, 6.1e9, 6.2e9]
  rabi:
    enabled: true
    params:
      amplitudes: [0.1, 0.2, 0.3, 0.4]
  # disable a routine explicitly:
  ramsey:
    enabled: false
```

## Writing Custom Routines

Routines are subclasses of `CalibrationRoutine`. If you need to implement a new calibration routine, define it by extending `qpi_driver.tuners.base.routines.CalibrationRoutine`.

```python
from qpi_driver.tuners.base.routines import CalibrationRoutine
from qpi_driver.tuners.base.report import RoutineResult

class MyCustomRoutine(CalibrationRoutine):
    @property
    def name(self) -> str:
        return "my_custom_routine"
        
    @property
    def dependencies(self) -> list[str]:
        return ["rabi"] # List routines that must run before this one
        
    def build_schedule(self, target: str, device, routine_config):
        # Build your quantify/qblox schedule here
        pass
        
    def analyse(self, dataset, target: str, device) -> dict:
        # Fit your data here and return the updated parameters
        return {"new_parameter_key": 42.0}
        
    def apply(self, device, target: str, params: dict):
        # Apply the parameters to your quantum device configuration
        pass
```

Register your custom routine to the DAG in your driver initialization or a custom device builder to add it to the execution graph.
