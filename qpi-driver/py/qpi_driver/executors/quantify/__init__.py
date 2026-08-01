import logging
from contextlib import suppress
from pathlib import Path
from typing import Any

import numpy as np
import qiskit
import xarray as xr
from qiskit import QuantumCircuit
from qiskit.circuit import library as qiskit_library

from qpi_driver.compat.quantify import (
    IS_QUANTIFY_INSTALLED,
    BinMode,
    Instrument,
    QbloxHardwareCompilationConfig,
    Schedule,
    SerialCompiler,
    set_datadir,
)
from qpi_driver.executors import JobPayload
from qpi_driver.executors.base import Executor
from qpi_driver.executors.quantify.config import (
    load_instrument_coordinator,
    load_quantify_hardware_config,
    load_quantum_device,
)
from qpi_driver.executors.quantify.conv import to_quantify_gates
from qpi_driver.executors.utils.batch import (
    combine_circuit_datasets,
    iter_circuit_datasets,
)
from qpi_driver.executors.utils.counts import (
    build_acquisition_counts,
    build_discriminator,
    per_shot_values,
    qubit_key,
)
from qpi_driver.executors.utils.discriminator import (
    discriminators_by_qubit,
    resolve_discriminators,
)
from qpi_driver.executors.utils.qiskit import load_qasm, measured_qubits
from qpi_driver.executors.utils.types import cast_to

log = logging.getLogger(__name__)


class QuantifyExecutor(Executor):
    """Executor subclass for interacting with Quantify-scheduler acquisition backends."""

    def __new__(cls, *args, **kwargs):
        if not IS_QUANTIFY_INSTALLED:
            raise ImportError(
                "quantify-scheduler is not installed. Install the [quantify] extra to use QuantifyExecutor."
            )
        return super().__new__(cls)

    def __init__(
        self,
        name: str = "quantify",
        quantify_hardware_config: QbloxHardwareCompilationConfig | Path | dict = Path(
            "quantify.hardware.json"
        ),
        quantify_device_config: Path | dict = Path("quantify.device.json"),
        is_dummy: bool = False,
        is_simulated: bool = False,
        data_dir: Path = Path("data"),
        acquisition_timeout: int = 10,
        **kwargs: Any,
    ) -> None:
        """Initialize the QuantifyExecutor.

        Args:
            name: the name of the executor
            quantify_hardware_config: Hardware-layer configuration dictionary, file path, or config as dict.
            quantify_device_config: Device-layer configuration dictionary, file path or config as dict
            is_dummy: If True, uses a dummy Cluster instrument. It compiles and
                runs, but every acquisition comes back `nan`, so results carry
                no information about the circuit.
            is_simulated: If True, plays the compiled schedule through
                `qpi_driver.simulation.SimulatedCoordinator` instead — a
                transmon model driven by the schedule's own pulses, so counts
                reflect both the circuit and the device calibration. Needs the
                `sim` dependency group. Mutually exclusive with `is_dummy`.
            data_dir: Directory to where data is temporarily stored.
            acquisition_timeout: Timeout in seconds to wait for acquisition.
            **kwargs: Arbitrary keyword arguments passed to the base class.
        """
        super().__init__(name, **kwargs)
        set_datadir(data_dir)
        # Clean up any previously registered instruments to avoid name collision errors in QCoDeS
        with suppress(Exception):
            Instrument.close_all()

        if is_dummy and is_simulated:
            raise ValueError(
                "is_dummy and is_simulated both replace the cluster; pick one"
            )
        self._is_dummy = is_dummy
        self._is_simulated = is_simulated
        self._acquisition_timeout = acquisition_timeout
        hardware_config = load_quantify_hardware_config(quantify_hardware_config)
        self._hardware_config = hardware_config
        self._device = load_quantum_device(name=name, config=quantify_device_config)
        if is_simulated:
            from qpi_driver.executors.utils.coupler_bias import (
                declared_sideband_gaps,
            )
            from qpi_driver.simulation import SimulatedCoordinator

            self._instrument_coordinator = SimulatedCoordinator(
                kwargs.get("simulator"),
                sideband_gaps=declared_sideband_gaps(self._device),
            )
        else:
            self._instrument_coordinator = load_instrument_coordinator(
                f"{name}_ic", hardware_config=hardware_config, is_dummy=is_dummy
            )
        self._device.hardware_config(hardware_config)
        self._bias_source = self._park_couplers(kwargs.get("spi_rack_address"))
        self._compiler = SerialCompiler(
            name=f"{name}_compiler", quantum_device=self._device
        )

    @property
    def hardware_config(self) -> QbloxHardwareCompilationConfig:
        return self._hardware_config

    def execute(self, payload: JobPayload) -> xr.Dataset:
        """Execute quantum instructions using the Quantify scheduler.

        The acquisition protocol is selected based on ``payload.meas_level``:

        * ``meas_level=0`` → ``Trace`` (raw waveform)
        * ``meas_level=1`` → ``SSBIntegrationComplex`` (kerneled IQ)
        * ``meas_level=2`` → ``ThresholdedAcquisition`` if threshold params
          are configured on the device elements, else ``SSBIntegrationComplex``
          (software discrimination deferred to ``process_result()``).

        Every circuit in ``payload.circuits`` is executed, honouring each
        circuit's ``shots`` override and ``parameter_values`` bindings.  A
        single-circuit payload returns that circuit's flat dataset; multi-circuit
        payloads are bundled so circuits with different qubit widths or shot
        counts stay independent (see ``combine_circuit_datasets``).

        Args:
            payload: JobPayload specifying shots, circuits, meas_level, etc.

        Returns:
            xr.Dataset: Raw acquisition dataset.
        """
        acq_protocol, acq_kwargs, acq_overrides = self._resolve_acq_protocol(payload)
        sub_datasets: list[xr.Dataset] = []

        for circ in payload.circuits:
            circ_shots = circ.shots if circ.shots is not None else payload.shots
            circuit = load_qasm(circ.circuit)

            for param_values in circ.parameter_values or [None]:
                bound_circuit = circuit
                if param_values is not None and circuit.parameters:
                    bound_circuit = circuit.assign_parameters(param_values)
                sub_datasets.append(
                    self._acquire_circuit(
                        payload,
                        bound_circuit,
                        circ_shots,
                        acq_protocol,
                        acq_kwargs,
                        acq_overrides,
                    )
                )

        return combine_circuit_datasets(sub_datasets)

    def _acquire_circuit(
        self,
        payload: JobPayload,
        circuit: QuantumCircuit,
        shots: int,
        acq_protocol: str,
        acq_kwargs: dict,
        acq_overrides: dict[int, dict[str, float]],
    ) -> xr.Dataset:
        """One circuit's acquisition, in as many runs as the hardware requires.

        Almost always one. The exception is a raw trace over more than one
        qubit: a Qblox module can put a single sequencer into scope mode, so
        asking two qubits for a trace at once does not compile —
        *"Only one sequencer per device can trigger raw trace capture"*.

        Rather than refuse a `meas_level=0` job on a multi-qubit circuit, run
        the circuit once per measured qubit and capture one trace each time.
        That is what the instrument allows and what a lab does by hand; the
        cost is honest and unavoidable — N runs' worth of time for N qubits,
        because the shots are repeated per qubit rather than shared.
        """
        measured = measured_qubits(circuit)
        if acq_protocol != "Trace" or len(measured) < 2:
            return self._run_circuit(
                payload, circuit, shots, acq_protocol, acq_kwargs, acq_overrides
            )

        log.info(
            "raw trace over %d qubits: taking %d runs, one scope-mode acquisition each",
            len(measured),
            len(measured),
        )
        passes = [
            self._run_circuit(
                payload,
                circuit,
                shots,
                acq_protocol,
                acq_kwargs,
                acq_overrides,
                only_qubit=qubit,
            )
            for qubit in measured
        ]
        # Each pass carries exactly one qubit's variable, on its own
        # channel-suffixed dimensions, so there is nothing to collide.
        return xr.merge(passes, combine_attrs="override")

    def _run_circuit(
        self,
        payload: JobPayload,
        circuit: QuantumCircuit,
        shots: int,
        acq_protocol: str,
        acq_kwargs: dict,
        acq_overrides: dict[int, dict[str, float]],
        only_qubit: int | None = None,
    ) -> xr.Dataset:
        """Run a single (parameter-bound) circuit and return its acquisition dataset."""
        schedule = Schedule(name=payload.id, repetitions=shots)
        acq_indices: dict[int, int] = {}
        clbit_map: list[tuple[int, int, int]] = []

        for instruction in circuit.data:
            parsed_ops = to_quantify_gates(
                circuit=circuit,
                instruction=instruction,
                acq_indices=acq_indices,
                acq_protocol=acq_protocol,
                acq_kwargs=acq_kwargs,
                acq_overrides=acq_overrides,
                clbit_map=clbit_map,
                only_qubit=only_qubit,
            )
            is_parallel_op = isinstance(
                instruction.operation,
                (qiskit_library.Measure, qiskit.circuit.Delay, qiskit_library.Barrier),
            )

            if is_parallel_op and parsed_ops:
                first_op = schedule.add(parsed_ops[0])
                for op in parsed_ops[1:]:
                    schedule.add(op, ref_op=first_op, ref_pt="start")
            else:
                for op in parsed_ops:
                    schedule.add(op)

        compiled_sched = self._compiler.compile(schedule=schedule)

        self._instrument_coordinator.prepare(compiled_sched)
        self._instrument_coordinator.start()
        self._instrument_coordinator.wait_done(timeout_sec=self._acquisition_timeout)
        dataset = self._instrument_coordinator.retrieve_acquisition()
        dataset.attrs.update(
            {
                "shots": shots,
                "n_qubits": circuit.num_qubits,
                "backend": self.name,
                "meas_level": payload.meas_level,
                "meas_return": payload.meas_return,
                "acq_protocol": acq_protocol,
                "clbit_map": [list(entry) for entry in clbit_map],
                "num_clbits": circuit.num_clbits,
            }
        )
        # Per qubit, because the discriminator is. `process_result` reads this back
        # when it has to threshold in software, and a single pair here would send it
        # down the same collapse the schedule no longer has.
        if acq_overrides:
            dataset.attrs["acq_discriminators"] = {
                str(index): dict(values) for index, values in acq_overrides.items()
            }
        return dataset

    def _park_couplers(self, spi_rack_address: str | None):
        """Hold every tunable coupler at its calibrated DC bias.

        Done once at startup rather than per job, because that is what the bias
        physically is: a current that sits there while the fridge is cold. It is
        not part of any schedule — quantify has no way to express an SPI rack —
        so if this does not happen, nothing else will do it, and every CZ runs
        against a coupler parked wherever it was left.

        Replacing the cluster replaces the rack too: with ``is_simulated`` or
        ``is_dummy`` there is no instrument to talk to, so the intended currents
        are recorded and not applied.
        """
        from qpi_driver.executors.utils.coupler_bias import (
            RecordingBias,
            apply_coupler_bias,
            resolve_bias_source,
        )

        try:
            source = (
                RecordingBias()
                if (self._is_simulated or self._is_dummy)
                else resolve_bias_source(
                    self._device,
                    cluster=self._cluster(),
                    spi_address=spi_rack_address,
                )
            )
            self._parked = apply_coupler_bias(self._device, source)
        except Exception:
            # A coupler that cannot be parked is a broken two-qubit gate, not a
            # broken node: single-qubit work is unaffected, and refusing to
            # start would take the whole QPU out for it.
            log.exception("could not park the couplers; two-qubit gates will be wrong")
            self._parked = {}
            return RecordingBias()
        return source

    def _cluster(self):
        """The Cluster behind the instrument coordinator, if there is one."""
        for component in getattr(
            self._instrument_coordinator, "components", lambda: []
        )():
            instrument = getattr(component, "instrument", None)
            if instrument is not None:
                return instrument
        return None

    def _resolve_acq_protocol(
        self, payload: JobPayload
    ) -> tuple[str, dict, dict[int, dict[str, float]]]:
        """Determine the quantify-scheduler acquisition protocol for the given meas_level.

        Args:
            payload: JobPayload specifying meas_level, acq_threshold, acq_rotation, etc.

        Returns:
            Tuple of (protocol_name, kwargs_for_every_Measure, kwargs_per_qubit).
        """
        bin_mode = self._resolve_bin_mode(payload.meas_level, payload.meas_return)

        if payload.meas_level == 0:
            return "Trace", {"bin_mode": bin_mode}, {}

        if payload.meas_level == 1:
            return "SSBIntegrationComplex", {"bin_mode": bin_mode}, {}

        # meas_level == 2: threshold on the instrument when every qubit has a line to
        # threshold against, in software otherwise. Per qubit either way — see
        # `qpi_driver.executors.utils.discriminator`.
        per_qubit, complete = resolve_discriminators(
            self._device, payload.acq_rotation, payload.acq_threshold
        )
        protocol = "ThresholdedAcquisition" if complete else "SSBIntegrationComplex"
        return protocol, {"bin_mode": bin_mode}, per_qubit

    def _resolve_bin_mode(self, meas_level: int, meas_return: str) -> BinMode:
        """Choose the acquisition bin mode implied by the requested measurement mode.

        Raw traces (level 0) and averaged integrated results (level 1 with
        ``meas_return="avg"``) collapse every repetition into a single bin.
        Counts (level 2) and single-shot integrated results keep each shot as a
        separate bin so results can be processed per shot.
        """
        if meas_level == 0:
            return BinMode.AVERAGE
        if meas_level == 1 and meas_return == "avg":
            return BinMode.AVERAGE
        return BinMode.APPEND

    def _get_threshold_params(self) -> dict[int, dict[str, float]]:
        """Every qubit's discriminator, by qubit index."""
        return discriminators_by_qubit(self._device)

    def process_result(self, dataset: xr.Dataset, job_id: str) -> dict:
        """Convert a quantify-scheduler acquisition dataset into a Qiskit-compatible result dict.

        Handles all meas_levels:
        - meas_level=0 (Trace): Returns raw complex waveform data as [[real, imag], ...] per time sample.
        - meas_level=1 (SSBIntegrationComplex): Returns IQ values as [[real, imag]] per shot per qubit.
        - meas_level=2 with ThresholdedAcquisition: Aggregates 0/1 values into counts dict.
        - meas_level=2 with SSBIntegrationComplex: Performs software discrimination using
          acq_threshold and acq_rotation from the device config.

        Args:
            dataset: xr.Dataset from execute().
            job_id: Unique job ID.

        Returns:
            dict: Qiskit-compatible result dict.
        """
        from qpi_driver.executors.utils.result import build_qiskit_result

        meas_level = cast_to(int, dataset.attrs.get("meas_level"), 2)
        meas_return = str(dataset.attrs.get("meas_return", "single"))
        acq_protocol = str(dataset.attrs.get("acq_protocol", "SSBIntegrationComplex"))
        backend = dataset.attrs.get("backend", self.name)

        circuit_results = [
            self._single_dataset_to_result(
                sub_ds, meas_level, meas_return, acq_protocol
            )
            for sub_ds in iter_circuit_datasets(dataset)
        ]
        return build_qiskit_result(circuit_results, job_id, backend)

    def _single_dataset_to_result(
        self, dataset: xr.Dataset, meas_level: int, meas_return: str, acq_protocol: str
    ) -> dict:
        """Extract result data from a single-circuit quantify dataset."""
        qubit_vars, q0_key, shots = self._extract_qubit_vars(dataset)
        if not qubit_vars:
            return {"raw": str(dataset), "shots": 0}

        if meas_level == 0:
            return self._process_meas_level_0(dataset, qubit_vars, shots)
        if meas_level == 1:
            return self._process_meas_level_1(dataset, qubit_vars, shots, meas_return)
        return self._process_meas_level_2(dataset, qubit_vars, shots, acq_protocol)

    def _extract_qubit_vars(self, dataset: xr.Dataset) -> tuple[list[int], str, int]:
        """Identify qubit variables (integer-named data vars) and shots."""
        qubit_vars = []
        for var in dataset.data_vars:
            try:
                qubit_vars.append(int(var))
            except ValueError:
                pass
        if not qubit_vars:
            return [], "", 0
        qubit_vars.sort()
        q0_key = qubit_key(dataset, qubit_vars[0])
        shots = cast_to(int, dataset.attrs.get("shots"), len(dataset[q0_key]))
        return qubit_vars, q0_key, shots

    def _process_meas_level_0(
        self, dataset: xr.Dataset, qubit_vars: list[int], shots: int
    ) -> dict:
        """Extract raw complex trace data (meas_level=0)."""
        memory: list[list[list[float]]] = []
        for q_idx in qubit_vars:
            var_key = q_idx if q_idx in dataset else str(q_idx)
            trace = dataset[var_key].values.flatten()
            qubit_trace = [[float(v.real), float(v.imag)] for v in trace]
            memory.append(qubit_trace)
        return {"memory": memory, "shots": shots}

    def _process_meas_level_1(
        self, dataset: xr.Dataset, qubit_vars: list[int], shots: int, meas_return: str
    ) -> dict:
        """Extract integrated IQ memory (meas_level=1)."""
        from qpi_driver.executors.utils.result import iq_memory_avg

        per_shot = {
            q_idx: per_shot_values(dataset[qubit_key(dataset, q_idx)])
            for q_idx in qubit_vars
        }
        num_samples = len(per_shot[qubit_vars[0]])
        memory = []
        for s in range(num_samples):
            shot_iq = []
            for q_idx in qubit_vars:
                val = per_shot[q_idx][s]
                r = float(val.real) if not np.isnan(val.real) else 0.0
                i = float(val.imag) if not np.isnan(val.imag) else 0.0
                shot_iq.append([r, i])
            memory.append(shot_iq)

        if meas_return == "avg" and memory:
            memory = iq_memory_avg(memory, len(qubit_vars))
        return {"memory": memory, "shots": shots}

    def _process_meas_level_2(
        self, dataset: xr.Dataset, qubit_vars: list[int], shots: int, acq_protocol: str
    ) -> dict:
        """Extract classified counts (meas_level=2) performing software discrimination if needed.

        Counts are keyed by classical register, one bit per measured clbit,
        positioned by clbit index (see ``build_acquisition_counts``).
        """
        discriminate = build_discriminator(
            dataset, acq_protocol, self._get_threshold_params
        )
        counts_dict = build_acquisition_counts(dataset, qubit_vars, discriminate)
        return {"counts": counts_dict, "shots": shots}

    def close(self) -> None:
        """Detach the coordinator's components, then release it.

        ``InstrumentCoordinator.components`` is a qcodes ``ManualParameter``
        holding component *names*, so it has to be called — iterating it
        directly raises, and a shutdown that raises leaves the cluster held
        against the next driver that wants it.
        """
        components: list = []
        with suppress(Exception):
            components = list(self._instrument_coordinator.components())

        for name in components:
            with suppress(Exception):
                self._instrument_coordinator.remove_component(name)

        with suppress(Exception):
            self._instrument_coordinator.close()
