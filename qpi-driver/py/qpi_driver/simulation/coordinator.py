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
operations, and a readout that reports IQ around two blobs.

Two-qubit gates too. A CZ compiles to a flux pulse, and that pulse detunes the
control towards the ``|11⟩``–``|02⟩`` crossing where the coupling in
:mod:`qpi_driver.simulation.coupled` carries population across and back — so the
conditional phase is integrated rather than asserted, and a Bell state comes out
entangled or comes out wrong.

Qubits are simulated independently until something entangles them. A flux pulse
merges the pair into one joint density matrix and they stay merged, because from
that point their state does not factor. That keeps a one-qubit circuit cheap and
makes a two-qubit one correct, at the cost of a register that grows as
``levels^n`` — hence :data:`MAX_ENTANGLED`.

**What is not.** Crosstalk, any readout chain beyond the blobs, and a flux pulse
whose partner cannot be identified — which raises rather than guessing, since
the wrong partner is a plausible-looking gate between the wrong qubits.
"""

import logging
from dataclasses import dataclass, field
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
    #: Per-shot outcomes for this qubit, drawn jointly with the rest of its
    #: register. ``None`` for a qubit entangled with nothing, where an
    #: independent draw from the marginal is the same thing.
    outcomes: Any = None


#: The most qubits allowed in one entangled register. Each one multiplies the
#: Liouvillian's side by ``levels²``: three transmons is a 729×729 matrix to
#: exponentiate per operation, which is slow but survivable, and four is not.
MAX_ENTANGLED = 3


@dataclass
class _Register:
    """A set of qubits sharing one density matrix, and their drive detunings.

    Most qubits are a register of one. Two become a register of two the moment a
    flux pulse couples them, because from then on their joint state does not
    factor — that is what entanglement *is*, and holding them apart afterwards
    would silently discard it.
    """

    simulator: TransmonSimulator
    qubits: list[str]
    rho: Any = None
    detunings: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        import qutip

        if self.rho is None:
            ground = qutip.basis(self.simulator.levels, 0)
            single = ground * ground.dag()
            self.rho = single
            for _ in self.qubits[1:]:
                self.rho = qutip.tensor(self.rho, single)
        for qubit in self.qubits:
            self.detunings.setdefault(qubit, 0.0)

    @property
    def levels(self) -> int:
        return self.simulator.levels

    def index_of(self, qubit: str) -> int:
        return self.qubits.index(qubit)

    def embed(self, operator, qubit: str):
        """A single-qubit operator as one acting on the whole register."""
        import qutip

        identity = qutip.qeye(self.levels)
        factors = [identity] * len(self.qubits)
        factors[self.index_of(qubit)] = operator
        return qutip.tensor(*factors) if len(factors) > 1 else factors[0]

    def ladder(self, qubit: str):
        import qutip

        return self.embed(qutip.destroy(self.levels), qubit)

    def population(self, qubit: str, level: int = 1) -> float:
        """The marginal probability of *qubit* being in *level*.

        A partial trace over everything else, which for a Bell state is exactly
        the 50/50 a single-qubit readout of one half should report.
        """
        import qutip

        projector = self.embed(
            qutip.basis(self.levels, level) * qutip.basis(self.levels, level).dag(),
            qubit,
        )
        return float(np.real((self.rho * projector).tr()))

    def collapse(self) -> list:
        """Relaxation and dephasing for every qubit in the register."""
        operators = []
        for qubit in self.qubits:
            ladder = self.ladder(qubit)
            if self.simulator.t1_ns > 0:
                operators.append(np.sqrt(1.0 / self.simulator.t1_ns) * ladder)
            inverse_tphi = 1.0 / self.simulator.t2_ns - 1.0 / (
                2.0 * self.simulator.t1_ns
            )
            if inverse_tphi > 0:
                operators.append(np.sqrt(2.0 * inverse_tphi) * ladder.dag() * ladder)
        return operators

    def signature(self) -> tuple:
        """A hashable description, for caching propagators across repetitions."""
        return (
            tuple(self.qubits),
            tuple(round(self.detunings[q], 3) for q in self.qubits),
        )


class _Registers:
    """Every qubit's state, grouped by what is entangled with what.

    A qubit is created independent and stays that way — one 3×3 density matrix,
    cheap — until a two-qubit gate joins it to another. Only then do the two
    become one joint state, so a circuit pays the exponential cost of
    entanglement exactly where it actually entangles.
    """

    def __init__(self, simulator: TransmonSimulator) -> None:
        self.simulator = simulator
        self._by_qubit: dict[str, _Register] = {}

    def __contains__(self, qubit: str) -> bool:
        return qubit in self._by_qubit

    def names(self) -> list[str]:
        return list(self._by_qubit)

    def of(self, qubit: str) -> _Register:
        register = self._by_qubit.get(qubit)
        if register is None:
            register = _Register(self.simulator, [qubit])
            self._by_qubit[qubit] = register
        return register

    def distinct(self) -> list[_Register]:
        """Each register once, however many qubits point at it."""
        seen: list[_Register] = []
        for register in self._by_qubit.values():
            if not any(register is other for other in seen):
                seen.append(register)
        return seen

    def join(self, first: str, second: str) -> _Register:
        """One register holding both qubits, merging theirs if they differ."""
        import qutip

        left, right = self.of(first), self.of(second)
        if left is right:
            return left
        if len(left.qubits) + len(right.qubits) > MAX_ENTANGLED:
            raise SimulationError(
                f"entangling {first!r} with {second!r} would make a register of "
                f"{len(left.qubits) + len(right.qubits)} qubits, over the limit of "
                f"{MAX_ENTANGLED}. The joint state grows as levels^n and the "
                "Liouvillian as its square; simulate a smaller circuit."
            )

        merged = _Register(
            self.simulator,
            left.qubits + right.qubits,
            qutip.tensor(left.rho, right.rho),
            {**left.detunings, **right.detunings},
        )
        for qubit in merged.qubits:
            self._by_qubit[qubit] = merged
        return merged


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
        #: Flux port to the (amplitude, start time) of an offset currently held.
        self._flux_offsets: dict[str, tuple[float, float]] = {}
        #: Register id to its per-shot joint outcomes, dropped as it evolves.
        self._sample_cache: dict[int, dict[str, Any]] = {}

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
        self._flux_offsets = {}
        self._sample_cache = {}
        registers = _Registers(self.simulator)
        clocks = self._clock_frequencies(compiled)
        acquisitions: list[_Acquisition] = []
        last_time: dict[str, float] = {}

        for time, operation, gate_qubits in self._flatten(compiled):
            for pulse in operation.data.get("pulse_info", []) or []:
                self._apply_pulse(
                    pulse, time, registers, clocks, last_time, gate_qubits
                )
            for acquisition in operation.data.get("acquisition_info", []) or []:
                self._acquire(acquisition, registers, acquisitions)

        return acquisitions

    def _flatten(
        self, schedule: Any, offset: float = 0.0, gate_qubits: tuple[str, ...] = ()
    ) -> list[tuple[float, Any, tuple[str, ...]]]:
        """Every leaf operation with its absolute start time, in time order.

        Recursive because a gate compiles to a subschedule — a `Measure` becomes
        a pulse and an acquisition inside one — and the flattened timing table
        quantify exposes stringifies its operations, so the objects have to come
        from here.

        The enclosing gate's qubits are carried down with it. A CZ lowers to a
        bare flux pulse whose ``port`` names only the qubit being detuned, so
        without the ancestor's ``gate_info`` there is nothing left to say which
        qubit it was detuned *towards*.
        """
        from quantify_scheduler import Schedule

        found: list[tuple[float, Any, tuple[str, ...]]] = []
        for schedulable in schedule.schedulables.values():
            operation = schedule.operations[schedulable["operation_id"]]
            start = offset + float(schedulable["abs_time"])
            inherited = _gate_qubits(operation) or gate_qubits
            if isinstance(operation, Schedule):
                found.extend(self._flatten(operation, start, inherited))
            else:
                found.append((start, operation, inherited))
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
        registers: _Registers,
        clocks: dict[str, float],
        last_time: dict[str, float],
        gate_qubits: tuple[str, ...] = (),
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
            self._apply_flux(
                pulse,
                port,
                duration,
                time + float(pulse.get("t0") or 0.0),
                registers,
                gate_qubits,
            )
            return

        qubit = _qubit_of(clock) or _qubit_of(port)
        if qubit is None or not clock.endswith(".01"):
            # A readout pulse, a baseband idle, anything not driving a qubit:
            # it still takes time, and time is where decoherence happens.
            for register in registers.distinct():
                self._idle(register, duration)
            return

        register = registers.of(qubit)
        register.detunings[qubit] = clocks.get(clock, 0.0) - self.simulator.f01 * GHZ

        amplitude = pulse.get("G_amp", pulse.get("amp"))
        if amplitude is None or duration <= 0:
            self._idle(register, duration)
            return

        self._drive(
            register,
            qubit,
            amplitude=float(amplitude),
            duration=duration,
            phase_deg=float(pulse.get("phase") or 0.0),
        )
        # Every other register was idling while this one was driven.
        for other in registers.distinct():
            if other is not register:
                self._idle(other, duration)

    def _apply_flux(
        self,
        pulse: dict,
        port: str,
        duration: float,
        start: float,
        registers: _Registers,
        gate_qubits: tuple[str, ...],
    ) -> None:
        """A flux pulse — a CZ — between the pulsed qubit and its gate partner.

        A long flux pulse does not arrive as one pulse. The qblox backend emits a
        *held DC offset*: set the offset to the amplitude, hold it, set it back to
        zero, and play a short square pulse for the final few nanoseconds. So a
        110 ns CZ reaches here as an offset from 0 to 106 ns plus a 4 ns pulse,
        and reading only the pulses would apply 4 ns of a gate that ran for 110.

        The offset is therefore tracked as state: an amplitude and the time it was
        set, evolved when it is cleared.
        """
        offset = pulse.get("offset_path_I")
        if offset is not None:
            amplitude = float(np.real(offset))
            if amplitude != 0.0:
                self._flux_offsets[port] = (amplitude, start)
                return
            held = self._flux_offsets.pop(port, None)
            if held is not None:
                self._run_flux(port, held[0], start - held[1], registers, gate_qubits)
            return

        amplitude = pulse.get("amp")
        if amplitude is None or duration <= 0:
            return
        self._run_flux(
            port, float(np.real(amplitude)), duration, registers, gate_qubits
        )

    def _run_flux(
        self,
        port: str,
        amplitude: float,
        duration: float,
        registers: _Registers,
        gate_qubits: tuple[str, ...],
    ) -> None:
        """Evolve the coupled pair for *duration* at flux *amplitude*."""
        if duration <= 0:
            return
        control = _qubit_of(port)
        if control is None:
            raise SimulationError(
                f"a flux pulse on {port!r} names no qubit, so there is nothing to "
                "detune"
            )

        partners = [name for name in gate_qubits if name != control]
        if len(partners) != 1:
            others = [name for name in registers.names() if name != control]
            if len(others) != 1:
                raise SimulationError(
                    f"a flux pulse on {port!r} is a two-qubit gate, but its partner "
                    f"cannot be identified: the operation names {gate_qubits or '()'} "
                    f"and the circuit holds {registers.names()}. Without the pair "
                    "there is no coupling to apply."
                )
            partners = others

        target = partners[0]
        register = registers.join(control, target)
        self._flux(register, control, target, amplitude, duration)
        for other in registers.distinct():
            if other is not register:
                self._idle(other, duration)

    def _drive(
        self,
        register: _Register,
        qubit: str,
        amplitude: float,
        duration: float,
        phase_deg: float,
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
        drive = register.embed(
            (rabi / 2)
            * (np.exp(-1j * phase) * destroy + np.exp(1j * phase) * destroy.dag()),
            qubit,
        )
        self._sample_cache.clear()
        self._propagate(register, self._drift(register) + drive, duration)

    def _idle(self, register: _Register, duration: float) -> None:
        """Free evolution, which is where T1 and T2 do their work.

        A `Reset` is a 200 µs idle rather than a special case: at a T1 of tens
        of microseconds it relaxes to the ground state on its own, which is what
        the instruction physically is.
        """
        self._propagate(register, self._drift(register), duration)

    def _flux(
        self,
        register: _Register,
        control: str,
        target: str,
        amplitude: float,
        duration: float,
    ) -> None:
        """A flux pulse: detune *control* towards *target* and let them exchange.

        This is the whole of a CZ. The pulse walks ``|11⟩`` into resonance with
        ``|02⟩``, the exchange coupling carries population across and back, and
        the round trip leaves a phase on ``|11⟩`` and on nothing else. None of
        that is applied as a gate — it is what integrating this Hamiltonian does.

        The coupling and the flux-to-detuning curve come from
        :mod:`qpi_driver.simulation.coupled`, so the number a CZ is calibrated
        against here is the same number the tuner's chevron finds there.
        """
        from qpi_driver.simulation.coupled import FLUX_CURVATURE_GHZ, G_MHZ

        detuning = 2 * np.pi * -FLUX_CURVATURE_GHZ * amplitude**2
        coupling = 2 * np.pi * G_MHZ * 1e-3
        control_ladder = register.ladder(control)
        target_ladder = register.ladder(target)
        number = control_ladder.dag() * control_ladder

        hamiltonian = (
            self._drift(register)
            + detuning * number
            + coupling
            * (
                control_ladder.dag() * target_ladder
                + control_ladder * target_ladder.dag()
            )
        )
        self._sample_cache.clear()
        self._propagate(register, hamiltonian, duration)

    def _drift(self, register: _Register):
        """Detuning plus anharmonicity for every qubit, cached per register shape.

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
        key = register.signature()
        drift = self._drift_cache.get(key)
        if drift is None:
            drift = None
            for qubit in register.qubits:
                single = self.simulator._anharmonic_hamiltonian(
                    detuning_ghz=register.detunings[qubit] / GHZ
                )
                embedded = register.embed(single, qubit)
                drift = embedded if drift is None else drift + embedded
            self._drift_cache[key] = drift
        return drift

    def _propagate(self, register: _Register, hamiltonian, duration: float) -> None:
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
        propagator = (
            qutip.liouvillian(hamiltonian, register.collapse()) * (duration / NS)
        ).expm()
        register.rho = qutip.vector_to_operator(
            propagator * qutip.operator_to_vector(register.rho)
        )

    def _acquire(
        self,
        acquisition: dict,
        registers: _Registers,
        found: list[_Acquisition],
    ) -> None:
        qubit = (
            _qubit_of(str(acquisition.get("clock") or ""))
            or _qubit_of(str(acquisition.get("port") or ""))
            or "q0"
        )
        register = registers.of(qubit)
        # The marginal, so reading one half of a Bell pair averages to the 50/50
        # it physically is.
        population = register.population(qubit)
        found.append(
            _Acquisition(
                channel=int(acquisition.get("acq_channel") or 0),
                index=int(acquisition.get("acq_index") or 0),
                protocol=str(acquisition.get("protocol") or ""),
                bin_mode=str(acquisition.get("bin_mode") or "average"),
                excited_population=float(np.clip(population, 0.0, 1.0)),
                outcomes=self._joint_outcomes(register).get(qubit),
            )
        )

    def _joint_outcomes(self, register: _Register) -> dict[str, Any]:
        """One outcome per shot for every qubit in *register*, drawn together.

        Correlation is the whole content of an entangled state, and it lives
        between qubits rather than in either one. Both halves of a Bell pair have
        a marginal of exactly 0.5, so sampling them independently — which is what
        a per-channel readout does — reproduces both marginals perfectly and
        destroys the only thing that made the state interesting. Drawing one
        joint outcome per shot and handing each qubit its own bit keeps it.

        Cached per register, because both halves are acquired separately and must
        agree on the shot they belong to. The cache is dropped by a *gate*
        (:meth:`_drive`, :meth:`_flux`) rather than by any evolution: a readout
        pulse idles every register between the two halves being read, and
        invalidating on that would hand each half its own independent draw —
        which reproduces both marginals and loses the correlation exactly as
        sampling them separately would.
        """
        # A register of one has nothing to correlate with, and drawing here would
        # consume the same random numbers the independent path already draws —
        # changing every single-qubit acquisition in the process.
        if len(register.qubits) < 2:
            return {}

        cached = self._sample_cache.get(id(register))
        if cached is not None:
            return cached

        levels, count = register.levels, len(register.qubits)
        populations = np.clip(np.real(np.diag(register.rho.full())), 0.0, None)
        total = float(populations.sum())
        if total <= 0:
            return {}
        draws = self._rng.choice(
            populations.size, size=self._repetitions, p=populations / total
        )

        outcomes: dict[str, Any] = {}
        for position, name in enumerate(register.qubits):
            divisor = levels ** (count - position - 1)
            # Anything above the ground state reads as a 1: a discriminator sees
            # "not |0>", and leakage to |2> is not a third outcome it can report.
            outcomes[name] = (draws // divisor) % levels >= 1
        self._sample_cache[id(register)] = outcomes
        return outcomes

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
                    [
                        self._shots(entry.excited_population)
                        if entry.outcomes is None
                        else self._blobs(entry.outcomes)
                        for entry in entries
                    ],
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
        return self._blobs(self._rng.random(self._repetitions) < population)

    def _blobs(self, excited: np.ndarray) -> np.ndarray:
        """The IQ point each already-drawn outcome lands on, with readout noise."""
        centres = np.where(excited, EXCITED_IQ, GROUND_IQ)
        noise = self._rng.normal(0.0, READOUT_NOISE, self._repetitions) + 1j * (
            self._rng.normal(0.0, READOUT_NOISE, self._repetitions)
        )
        return centres + noise


def _gate_qubits(operation: Any) -> tuple[str, ...]:
    """The qubits an operation names at the gate level, if it is still a gate.

    A compiled CZ keeps its ``gate_info`` on the operation that lowered to the
    flux pulse, which is the only remaining record of which pair it acts on.
    """
    try:
        gate = operation.data.get("gate_info") or {}
    except AttributeError:
        return ()
    # `device_elements` is the current spelling; `qubits` is what older
    # quantify-scheduler called the same field.
    qubits = gate.get("device_elements") or gate.get("qubits") or ()
    return tuple(str(qubit) for qubit in qubits)


def _qubit_of(identifier: str) -> str | None:
    """The qubit a clock or port belongs to: ``q0.01`` and ``q0:mw`` are both q0."""
    for separator in (".", ":"):
        if separator in identifier:
            head = identifier.split(separator)[0]
            return head or None
    return identifier or None
