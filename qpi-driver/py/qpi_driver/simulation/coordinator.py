"""A simulated instrument, where the cluster would be (RFC 0004 §7).

`quantify-scheduler` reaches its hardware through four calls, and only four::

    coordinator.prepare(compiled_schedule)
    coordinator.start()
    coordinator.wait_done(timeout_sec=...)
    dataset = coordinator.retrieve_acquisition()

Both `QuantifyBackend.run` and `QuantifyExecutor._run_circuit` go through
exactly that. So a class answering those four is a drop-in for the cluster —
and, unlike the dummy cluster, this one *reads the schedule it was given* and
returns what the physics would have produced.

Why that matters is the whole point of the thing. A dummy cluster returns
``nan+nanj`` no matter what you send it, so the calibration a tuner writes and
the pulses an executor plays are never actually connected: an ``amp180`` off by
a factor of ten compiles, runs and returns the same nothing. Here the compiled
schedule's own ``G_amp`` is the drive amplitude that rotates the state, so a
wrong calibration is a wrong population, measured.

**What is modelled.** Drive pulses on a qubit's ``.01`` clock (the DRAG and
square waveforms quantify emits for ``Rxy``/``X``/``Y``), idles and resets,
clock detuning from ``SetClockFrequency``, relaxation and dephasing between
operations, and a readout that reports IQ around two blobs. Single-qubit
operations are exact; the qubits are simulated independently.

**What is not.** Two-qubit gates. A CZ compiles to a flux pulse whose area sets
the conditional phase, and modelling that needs the coupler — so a flux pulse
raises rather than silently behaving like an identity, which would make a Bell
state look like a broken one. Also absent: crosstalk, leakage out of the
computational subspace during a gate, and any readout chain beyond the blobs.
"""

import logging
from dataclasses import dataclass
from typing import Any

import numpy as np
import xarray as xr

from qpi_driver.simulation.transmon import GHZ, NS, TransmonSimulator

log = logging.getLogger(__name__)

#: Drive amplitude to Rabi rate, in rad/s per unit of pulse amplitude. This is
#: the one number that stands in for the whole analogue chain — DAC scale, mixer
#: conversion, line attenuation, coupling — and it is a property of the
#: simulated instrument, not of the qubit.
#:
#: It is what makes the loop closed rather than circular. Nothing tells the tuner
#: this number: Rabi *discovers* the amplitude at which a 20 ns pulse rotates by
#: π, writes it to the device file as ``amp180``, and the executor later plays
#: that amplitude and gets a π rotation. Get the write-back wrong, or the units,
#: and the population comes out wrong here.
DEFAULT_DRIVE_STRENGTH = np.pi / (0.2 * 20e-9)

#: Where the two readout blobs sit in the IQ plane, and how wide they are. The
#: separation over the width is the readout fidelity a discriminator can reach.
#:
#: Their placement has to satisfy both readers of an acquisition, which want
#: different things:
#:
#: - The executor discriminates a shot by comparing the rotated *real part*
#:   against ``acq_threshold``, so the two must straddle it — ground above the
#:   threshold would label every ground state ``1`` and inverts every circuit.
#: - A calibration routine reduces an acquisition to ``|z|`` (`signal_of`), so
#:   the two must also differ in *magnitude* — blobs placed symmetrically about
#:   the origin have the same modulus, and every sweep would come out flat.
#:
#: Hence opposite real parts and different moduli. Real blobs sit wherever the
#: readout chain puts them and generally satisfy both by accident; here it has
#: to be deliberate.
GROUND_IQ = complex(-1.0, 0.0)
EXCITED_IQ = complex(1.0, 3.0)
READOUT_NOISE = 0.25


class SimulationError(RuntimeError):
    """The schedule asks for something this simulator does not model."""


@dataclass
class _Acquisition:
    """One acquisition slot, and the population found there."""

    channel: int
    index: int
    protocol: str
    bin_mode: str
    excited_population: float


@dataclass
class _QubitState:
    """One qubit's density matrix and the drive detuning currently applied."""

    simulator: TransmonSimulator
    rho: Any = None
    detuning_hz: float = 0.0

    def __post_init__(self) -> None:
        import qutip

        if self.rho is None:
            ground = qutip.basis(self.simulator.levels, 0)
            self.rho = ground * ground.dag()

    @property
    def excited_population(self) -> float:
        return float(np.real(self.rho[1, 1]))


class SimulatedCoordinator:
    """An `InstrumentCoordinator` that computes its acquisitions.

    Args:
        simulator: the transmon every qubit is modelled as. One object is
            shared: this is a model of *a* transmon, not of a heterogeneous
            chip, and pretending otherwise would invent numbers nobody supplied.
        drive_strength: see :data:`DEFAULT_DRIVE_STRENGTH`.
        seed: seeds readout noise, so a failure reproduces.
    """

    def __init__(
        self,
        simulator: TransmonSimulator | None = None,
        *,
        drive_strength: float = DEFAULT_DRIVE_STRENGTH,
        seed: int = 20260731,
    ) -> None:
        self.simulator = simulator or TransmonSimulator()
        self.drive_strength = drive_strength
        self._rng = np.random.default_rng(seed)
        self._compiled: Any = None
        self._drift_cache: dict[float, Any] = {}
        self._acquisitions: list[_Acquisition] = []
        self._repetitions = 1

    # --- the InstrumentCoordinator surface ------------------------------------

    def prepare(self, compiled_schedule: Any) -> None:
        self._compiled = compiled_schedule
        self._acquisitions = []

    def start(self) -> None:
        if self._compiled is None:
            raise SimulationError("start() before prepare()")
        self._acquisitions = self._simulate(self._compiled)

    def wait_done(self, timeout_sec: int = 10) -> None:
        """Nothing to wait for — the work happened in :meth:`start`."""

    def retrieve_acquisition(self) -> xr.Dataset:
        return self._to_dataset(self._acquisitions)

    def components(self) -> list[str]:
        """Named like the qcodes parameter the real coordinator exposes."""
        return []

    def remove_component(self, name: str) -> None:  # pragma: no cover - no components
        pass

    def close(self) -> None:
        self._compiled = None

    # --- walking the schedule --------------------------------------------------

    def _simulate(self, compiled: Any) -> list[_Acquisition]:
        """Play *compiled* through the transmons and collect what was measured."""
        self._repetitions = int(getattr(compiled, "repetitions", 1) or 1)
        qubits: dict[str, _QubitState] = {}
        clocks = self._clock_frequencies(compiled)
        acquisitions: list[_Acquisition] = []
        last_time: dict[str, float] = {}

        for time, operation in self._flatten(compiled):
            for pulse in operation.data.get("pulse_info", []) or []:
                self._apply_pulse(pulse, time, qubits, clocks, last_time, acquisitions)
            for acquisition in operation.data.get("acquisition_info", []) or []:
                self._acquire(acquisition, qubits, acquisitions)

        return acquisitions

    def _flatten(self, schedule: Any, offset: float = 0.0) -> list[tuple[float, Any]]:
        """Every leaf operation with its absolute start time, in time order.

        Recursive because a gate compiles to a subschedule — a `Measure` becomes
        a pulse and an acquisition inside one — and the flattened timing table
        quantify exposes stringifies its operations, so the objects have to come
        from here.
        """
        from quantify_scheduler import Schedule

        found: list[tuple[float, Any]] = []
        for schedulable in schedule.schedulables.values():
            operation = schedule.operations[schedulable["operation_id"]]
            start = offset + float(schedulable["abs_time"])
            if isinstance(operation, Schedule):
                found.extend(self._flatten(operation, start))
            else:
                found.append((start, operation))
        return sorted(found, key=lambda item: item[0])

    @staticmethod
    def _clock_frequencies(compiled: Any) -> dict[str, float]:
        frequencies = {}
        for name, resource in compiled.resources.items():
            try:
                frequencies[name] = float(dict(resource).get("freq", 0.0))
            except Exception:  # noqa: BLE001 - a clock without a frequency is not one
                continue
        return frequencies

    # --- the physics -----------------------------------------------------------

    def _apply_pulse(
        self,
        pulse: dict,
        time: float,
        qubits: dict[str, _QubitState],
        clocks: dict[str, float],
        last_time: dict[str, float],
        acquisitions: list[_Acquisition],
    ) -> None:
        clock = str(pulse.get("clock") or "")
        port = str(pulse.get("port") or "")
        duration = float(pulse.get("duration") or 0.0)

        # A `SetClockFrequency` retunes a clock mid-schedule, and that is how
        # every spectroscopy routine sweeps. Reading the frequencies once from
        # `resources` would make the whole sweep sit at one point and fit a flat
        # line, so the retunings have to be replayed as they are reached.
        if "clock_freq_new" in pulse:
            new = pulse.get("clock_freq_new")
            if new is not None:
                clocks[clock] = float(new)
            return

        if ":fl" in port or clock.endswith(".flux"):
            raise SimulationError(
                f"a flux pulse on {port!r} compiles a two-qubit gate, and the "
                "coupler is not modelled. Restrict the circuit to one-qubit "
                "operations, or extend this simulator."
            )

        qubit = _qubit_of(clock) or _qubit_of(port)
        if qubit is None or not clock.endswith(".01"):
            # A readout pulse, a baseband idle, anything not driving a qubit:
            # it still takes time, and time is where decoherence happens.
            for state in qubits.values():
                self._idle(state, duration)
            return

        state = qubits.setdefault(qubit, _QubitState(self.simulator))
        state.detuning_hz = clocks.get(clock, 0.0) - self.simulator.f01 * GHZ

        amplitude = pulse.get("G_amp", pulse.get("amp"))
        if amplitude is None or duration <= 0:
            self._idle(state, duration)
            return

        self._drive(
            state,
            amplitude=float(amplitude),
            duration=duration,
            phase_deg=float(pulse.get("phase") or 0.0),
        )
        # Every other qubit was idling while this one was driven.
        for name, other in qubits.items():
            if name != qubit:
                self._idle(other, duration)

    def _drive(
        self, state: _QubitState, amplitude: float, duration: float, phase_deg: float
    ) -> None:
        """Evolve under a drive of *amplitude* for *duration*.

        The rotation is not written down — it falls out of the drive, the
        detuning and the collapse operators evolving together, which is what
        makes an off-resonant or mis-scaled pulse produce the wrong population
        rather than a nominally correct one.
        """
        import qutip

        destroy = qutip.destroy(self.simulator.levels)
        rabi = self.drive_strength * amplitude * NS  # rad/ns
        phase = np.deg2rad(phase_deg)
        drive = (rabi / 2) * (
            np.exp(-1j * phase) * destroy + np.exp(1j * phase) * destroy.dag()
        )
        self._propagate(state, self._drift(state) + drive, duration)

    def _idle(self, state: _QubitState, duration: float) -> None:
        """Free evolution, which is where T1 and T2 do their work.

        A `Reset` is a 200 µs idle rather than a special case: at a T1 of tens
        of microseconds it relaxes to the ground state on its own, which is what
        the instruction physically is.
        """
        self._propagate(state, self._drift(state), duration)

    def _drift(self, state: _QubitState):
        """Detuning plus anharmonicity, cached per detuning.

        The sign is ``+(f_drive - f_qubit)``, which pairs with the phase
        convention in :meth:`_drive`. It is not a free choice and it is not
        obvious: a Ramsey measurement is precisely the experiment that
        distinguishes the two, and it does so unmistakably. With this sign, a
        device deliberately set 5 MHz off is pulled back to within 5 Hz; with
        the other, the routine "corrects" to 2 MHz off — twice the artificial
        detuning, because the fringe comes out as ``|residual - artificial|``
        instead of the sum, and the routine then subtracts the wrong way.

        Caching matters too: building this diagonalises the transmon for its
        anharmonicity, and a schedule has thousands of operations at a handful
        of distinct detunings.
        """
        key = round(state.detuning_hz, 3)
        drift = self._drift_cache.get(key)
        if drift is None:
            drift = self.simulator._anharmonic_hamiltonian(
                detuning_ghz=state.detuning_hz / GHZ
            )
            self._drift_cache[key] = drift
        return drift

    def _propagate(self, state: _QubitState, hamiltonian, duration: float) -> None:
        """Apply *hamiltonian* for *duration* by exponentiating the Liouvillian.

        Not an ODE solve. The Hamiltonian is constant across a pulse or an idle,
        so the exact propagator is ``exp(L·t)`` — and stepping to it instead
        fails outright on the operations that matter most: a `Reset` is a 200 µs
        idle, and at a few hundred MHz of detuning that is ~10^5 radians of
        accumulated phase, which exhausts any step budget ("excess work done").
        Exponentiating a 9×9 Liouvillian is both exact and faster.
        """
        import qutip

        if duration <= 0:
            return
        _destroy, _excited, collapse = self.simulator._operators()
        propagator = (qutip.liouvillian(hamiltonian, collapse) * (duration / NS)).expm()
        state.rho = qutip.vector_to_operator(
            propagator * qutip.operator_to_vector(state.rho)
        )

    def _acquire(
        self,
        acquisition: dict,
        qubits: dict[str, _QubitState],
        found: list[_Acquisition],
    ) -> None:
        qubit = _qubit_of(str(acquisition.get("clock") or "")) or _qubit_of(
            str(acquisition.get("port") or "")
        )
        state = qubits.setdefault(qubit or "q0", _QubitState(self.simulator))
        found.append(
            _Acquisition(
                channel=int(acquisition.get("acq_channel") or 0),
                index=int(acquisition.get("acq_index") or 0),
                protocol=str(acquisition.get("protocol") or ""),
                bin_mode=str(acquisition.get("bin_mode") or "average"),
                excited_population=float(np.clip(state.excited_population, 0.0, 1.0)),
            )
        )

    # --- the acquisition dataset ------------------------------------------------

    def _to_dataset(self, acquisitions: list[_Acquisition]) -> xr.Dataset:
        """The shape a real cluster returns: one complex variable per channel.

        `BinMode.AVERAGE` gives one IQ point per acquisition index, on the line
        between the two blobs. `BinMode.APPEND` gives one per shot, sampled from
        the population — which is what a `meas_level=2` job needs, because the
        executor discriminates every shot itself.
        """
        by_channel: dict[int, list[_Acquisition]] = {}
        for acquisition in acquisitions:
            by_channel.setdefault(acquisition.channel, []).append(acquisition)

        variables: dict[Any, Any] = {}
        coordinates: dict[str, Any] = {}
        for channel, entries in by_channel.items():
            entries.sort(key=lambda item: item.index)
            axis = f"acq_index_{channel}"
            single_shot = any(entry.bin_mode == "append" for entry in entries)

            if single_shot:
                shots = np.stack(
                    [self._shots(entry.excited_population) for entry in entries],
                    axis=-1,
                )
                variables[channel] = (("repetition", axis), shots)
                coordinates["repetition"] = np.arange(self._repetitions)
            else:
                values = np.array(
                    [self._averaged(entry.excited_population) for entry in entries]
                )
                variables[channel] = ((axis,), values)
            coordinates[axis] = np.array([entry.index for entry in entries])

        if not variables:
            return xr.Dataset()
        return xr.Dataset(variables, coords=coordinates)

    def _averaged(self, population: float) -> complex:
        """The mean IQ point, with the noise an averaged acquisition still has."""
        centre = GROUND_IQ * (1 - population) + EXCITED_IQ * population
        spread = READOUT_NOISE / np.sqrt(max(self._repetitions, 1))
        return complex(
            centre.real + self._rng.normal(0.0, spread),
            centre.imag + self._rng.normal(0.0, spread),
        )

    def _shots(self, population: float) -> np.ndarray:
        """One IQ point per shot: a Bernoulli draw, then the blob it landed in."""
        excited = self._rng.random(self._repetitions) < population
        centres = np.where(excited, EXCITED_IQ, GROUND_IQ)
        noise = self._rng.normal(0.0, READOUT_NOISE, self._repetitions) + 1j * (
            self._rng.normal(0.0, READOUT_NOISE, self._repetitions)
        )
        return centres + noise


def _qubit_of(identifier: str) -> str | None:
    """The qubit a clock or port belongs to: ``q0.01`` and ``q0:mw`` are both q0."""
    for separator in (".", ":"):
        if separator in identifier:
            head = identifier.split(separator)[0]
            return head or None
    return identifier or None
