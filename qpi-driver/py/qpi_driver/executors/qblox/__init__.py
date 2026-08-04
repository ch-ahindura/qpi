import logging
from contextlib import suppress
from pathlib import Path
from typing import Any

import numpy as np
import xarray as xr
from qiskit import QuantumCircuit

from qpi_driver.compat.qblox import (
    IS_QBLOX_SCHEDULER_INSTALLED,
    BinMode,
    HardwareAgent,
    Instrument,
    QbloxHardwareCompilationConfig,
)
from qpi_driver.executors import JobPayload
from qpi_driver.executors.base import Executor
from qpi_driver.executors.qblox.config import (
    apply_device_config,
    load_quantify_hardware_config,
    load_quantum_device,
)
from qpi_driver.executors.qblox.conv import generate_schedule
from qpi_driver.reload import ConfigFile
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
    readout_points_by_qubit,
    resolve_discriminators,
)
from qpi_driver.executors.utils.qiskit import load_qasm, measured_qubits
from qpi_driver.executors.utils.types import cast_to

log = logging.getLogger(__name__)


class QbloxExecutor(Executor):
    """Executor subclass for interacting with Qblox instruments and modules via qblox-scheduler."""

    def __new__(cls, *args, **kwargs):
        if not IS_QBLOX_SCHEDULER_INSTALLED:
            raise ImportError(
                "qblox-scheduler is not installed. Install the [qblox] extra to use QbloxExecutor."
            )
        return super().__new__(cls)

    def __init__(
        self,
        name: str = "qblox",
        quantify_hardware_config: QbloxHardwareCompilationConfig | Path | dict = Path(
            "quantify.hardware.json"
        ),
        quantify_device_config: Path | dict = Path("quantify.device.json"),
        is_dummy: bool = False,
        data_dir: Path = Path("data"),
        acquisition_timeout: int = 10,
        **kwargs: Any,
    ) -> None:
        """Initialize the QbloxExecutor.

        Args:
            name: the name of the executor
            quantify_hardware_config: Hardware-layer configuration dictionary, file path, or config as dict.
            quantify_device_config: Device-layer configuration dictionary, file path or config as dict
            is_dummy: If True, uses a dummy Cluster instrument.
            data_dir: Directory to where data is temporarily stored.
            acquisition_timeout: Timeout in seconds to wait for acquisition.
            **kwargs: Arbitrary keyword arguments passed to the base class.
        """
        super().__init__(name, **kwargs)
        # Clean up any previously registered instruments to avoid name collision errors in QCoDeS
        with suppress(Exception):
            Instrument.close_all()

        self._data_dir = data_dir
        is_simulated = bool(kwargs.pop("is_simulated", False))
        if is_dummy and is_simulated:
            raise ValueError(
                "is_dummy and is_simulated both replace the cluster; pick one"
            )
        self._is_dummy = is_dummy
        self._is_simulated = is_simulated
        self._acquisition_timeout = acquisition_timeout
        self._hardware_config = load_quantify_hardware_config(quantify_hardware_config)
        self._watched_hardware_config = (
            ConfigFile(quantify_hardware_config)
            if isinstance(quantify_hardware_config, Path)
            else None
        )
        self._watched_device_config = (
            ConfigFile(quantify_device_config)
            if isinstance(quantify_device_config, Path)
            else None
        )
        self._device = load_quantum_device(name=name, config=quantify_device_config)
        if is_simulated:
            # A real agent for compilation, the simulator for execution — the
            # agent's `run` is the only part a chip is needed for. See
            # `qpi_driver.simulation.agent`.
            from qpi_driver.executors.utils.coupler_bias import (
                declared_parking_currents,
                declared_sideband_gaps,
            )
            from qpi_driver.simulation.agent import simulated_agent

            self._agent = simulated_agent(
                hardware_configuration=self._hardware_config,
                quantum_device_configuration=self._device,
                output_dir=data_dir,
                simulator=kwargs.get("simulator"),
                sideband_gaps=declared_sideband_gaps(self._device),
                parking_currents=declared_parking_currents(self._device),
            )
        else:
            self._agent = HardwareAgent(
                hardware_configuration=self._hardware_config,
                quantum_device_configuration=self._device,
                create_dummy_connections=is_dummy,
                output_dir=data_dir,
            )
        self._bias_source = self._park_couplers(kwargs.get("spi_rack_address"))

    def _reload_device_config(self) -> None:
        """Apply the device config file, which has changed on disk, to the device.

        A calibration on this node rewrites that file (RFC 0004 §8), and so does
        anyone restoring parameters by hand. Neither can reach into this process,
        so the file is the whole channel.

        A file that will not parse leaves the device as it was and costs the next
        job its new parameters, not the driver its device. The tuner's own writes
        are atomic, so only a hand-dropped file can be seen part-written.
        """
        path = self._watched_device_config.path
        try:
            unknown = apply_device_config(self._device, path)
        except Exception:
            log.exception("could not reload %s; keeping the current parameters", path)
            return

        if unknown:
            log.warning(
                "%s names %s, which this device does not have; a new element needs a "
                "restart",
                path,
                ", ".join(unknown),
            )
        log.info("reloaded device parameters from %s", path)

        if self._is_simulated:
            from qpi_driver.executors.utils.coupler_bias import (
                declared_parking_currents,
                declared_sideband_gaps,
            )

            coordinator = getattr(self._agent, "instrument_coordinator", None)
            if coordinator is not None:
                coordinator.sideband_gaps = declared_sideband_gaps(self._device)
                coordinator.parking_currents = declared_parking_currents(self._device)

        # The device object is the same one, so the agent still points at it. The
        # bias does not follow: it is a current sitting in a rack, and only
        # re-applying it moves the coupler.
        self._reapply_coupler_bias()

    def _warn_if_hardware_config_moved(self) -> None:
        """Say so when the hardware config changes, and keep running on the old one.

        Deliberately not reloaded. It builds the instrument coordinator and the
        Cluster behind it, so applying a new one means closing a live connection to
        the rack and dialling it again — and a reconnect that fails leaves this
        driver with no coordinator and no way back, the old one being already gone.
        The device config has somewhere to fall back to; this does not.

        A restart is the honest answer: rewiring a rack is not a runtime event.
        Warned once per change so a stale hardware config is at least not a silent
        one.
        """
        if not self._watched_hardware_config:
            return
        if not self._watched_hardware_config.changed():
            return
        self._watched_hardware_config.mark_read()
        log.warning(
            "%s has changed; this driver is still running on the hardware config it "
            "started with. Restart it to pick the new one up.",
            self._watched_hardware_config.path,
        )

    def _reapply_coupler_bias(self) -> None:
        """Hold the couplers at the reloaded currents, on the rack already open.

        Resolving a second source would open a second connection to the same
        rack, or clash on its qcodes name.
        """
        from qpi_driver.executors.utils.coupler_bias import apply_coupler_bias

        try:
            self._parked = apply_coupler_bias(self._device, self._bias_source)
        except Exception:
            log.exception("could not park the couplers; two-qubit gates will be wrong")
            self._parked = {}

    @property
    def hardware_config(self) -> QbloxHardwareCompilationConfig:
        return self._hardware_config

    def execute(self, payload: JobPayload) -> xr.Dataset:
        """Execute quantum instructions using the Qblox scheduler.

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
        # Between jobs, never during one: a reload part-way through a compilation
        # would be worse than a stale parameter.
        if self._watched_device_config and self._watched_device_config.changed():
            self._reload_device_config()
            self._watched_device_config.mark_read()
        self._warn_if_hardware_config_moved()

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

    def _park_couplers(self, spi_rack_address: str | None):
        """Hold every tunable coupler at its calibrated DC bias.

        The same thing the quantify executor does, for the same reason: the bias
        is a seconds-scale DC current that no schedule can express, so if the
        driver does not set it then nothing does and every CZ runs against a
        coupler parked wherever it was left.
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
            # broken node.
            log.exception("could not park the couplers; two-qubit gates will be wrong")
            self._parked = {}
            return RecordingBias()
        return source

    def _cluster(self):
        """The first cluster the agent is connected to, if any."""
        clusters = getattr(self._agent, "get_clusters", lambda: [])()
        return clusters[0] if clusters else None

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
        asking two qubits for a trace at once does not compile. The circuit is
        played once per measured qubit instead, capturing one trace each time,
        at an honest cost of N runs for N qubits.

        The same constraint and the same remedy as the quantify executor's —
        it is a property of the module, not of the scheduler driving it.
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
        schedule, clbit_map, num_clbits = generate_schedule(
            name=payload.id,
            circuit=circuit,
            shots=shots,
            acq_protocol=acq_protocol,
            acq_kwargs=acq_kwargs,
            acq_overrides=acq_overrides,
            readout_points=readout_points_by_qubit(self._device),
            only_qubit=only_qubit,
        )

        dataset = self._agent.run(schedule, timeout=self._acquisition_timeout)
        dataset.attrs.update(
            {
                "shots": shots,
                "n_qubits": circuit.num_qubits,
                "backend": self.name,
                "meas_level": payload.meas_level,
                "meas_return": payload.meas_return,
                "acq_protocol": acq_protocol,
                "clbit_map": [list(entry) for entry in clbit_map],
                "num_clbits": num_clbits,
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

    def _resolve_acq_protocol(
        self, payload: JobPayload
    ) -> tuple[str, dict, dict[int, dict[str, float]]]:
        """Determine the qblox-scheduler acquisition protocol for the given meas_level.

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
        # And the power those shots are taken at, where a `CalibratedTransmon` says so.
        # The matching frequency cannot ride on `Measure` — it is a clock, not a pulse
        # parameter — so `_readout_overrides` sets it on the schedule instead.
        for index, point in readout_points_by_qubit(self._device).items():
            if index in per_qubit:
                per_qubit[index]["pulse_amp"] = point["pulse_amp"]
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
        """Convert a qblox-scheduler acquisition dataset into a Qiskit-compatible result dict.

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
        """Release resources."""
        with suppress(Exception):
            self._agent.instrument_coordinator.close()
