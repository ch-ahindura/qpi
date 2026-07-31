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
    #: Integration window, in seconds — the length of a `Trace`.
    duration: float = 0.0
    #: What a `ThresholdedAcquisition` discriminates against, as the hardware
    #: does it: rotate the IQ point by *rotation* degrees, compare the real part
    #: against *threshold*.
    threshold: float = 0.0
    rotation: float = 0.0


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
        sideband_gaps: dict[str, float] | None = None,
    ) -> None:
        self.simulator = simulator or TransmonSimulator()
        #: Edge name to the ``|11>-|02>`` gap its CZ drive is meant to bridge,
        #: in Hz, as that edge's own config declares it. An edge that has not
        #: been characterised falls back to `SIDEBAND_GAP_GHZ` — which is a
        #: chosen number, so a device that knows its own gap should say so.
        self.sideband_gaps = dict(sideband_gaps or {})
        self.drive_strength = drive_strength
        self._rng = np.random.default_rng(seed)
        self._compiled: Any = None
        self._drift_cache: dict[float, Any] = {}
        self._acquisitions: list[_Acquisition] = []
        self._repetitions = 1
        #: Flux port to the (amplitude, start time) of an offset currently held.
        self._flux_offsets: dict[str, tuple[float, float]] = {}
        #: Clock to its accumulated virtual-Z phase, in degrees.
        self._clock_phases: dict[str, float] = {}
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
        self._clock_phases = {}
        self._sample_cache = {}
        registers = _Registers(self.simulator)
        clocks = self._clock_frequencies(compiled)
        acquisitions: list[_Acquisition] = []
        last_time: dict[str, float] = {}

        for time, operation, gate_qubits in self._flatten(compiled):
            for pulse in _infos(operation, "pulse_info"):
                self._apply_pulse(
                    pulse, time, registers, clocks, last_time, gate_qubits
                )
            for acquisition in _infos(operation, "acquisition_info"):
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
        found: list[tuple[float, Any, tuple[str, ...]]] = []
        operations = _operations_of(schedule)
        for schedulable in schedule.schedulables.values():
            operation = operations[schedulable["operation_id"]]
            start = offset + float(schedulable["abs_time"])
            inherited = _gate_qubits(operation) or gate_qubits
            if _is_subschedule(operation):
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

        # A `ShiftClockPhase` is a virtual Z: no pulse is played, the frame the
        # *next* drive is referenced to simply moves. Every `rz`, `z`, `s` and
        # `t` compiles to one, and so does each half of a CZ's phase correction,
        # so ignoring these makes all of them silently do nothing.
        if "phase_shift" in pulse:
            shift = pulse.get("phase_shift")
            if shift is not None:
                self._clock_phases[clock] = (
                    self._clock_phases.get(clock, 0.0) + float(shift)
                ) % 360.0
            return

        if ":fl" in port or clock.endswith(".flux"):
            self._apply_flux(
                pulse,
                port,
                duration,
                time + float(pulse.get("t0") or 0.0),
                registers,
                gate_qubits,
                clocks.get(clock, 0.0),
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

        amplitude = _amplitude_of(pulse)
        if amplitude is None or duration <= 0:
            self._idle(register, duration)
            return

        self._drive(
            register,
            qubit,
            amplitude=float(amplitude),
            duration=duration,
            # The pulse's own phase, in the frame the clock has been shifted to.
            phase_deg=float(pulse.get("phase") or 0.0)
            + self._clock_phases.get(clock, 0.0),
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
        drive_hz: float = 0.0,
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

        amplitude = _amplitude_of(pulse)
        if amplitude is None or duration <= 0:
            return
        self._run_flux(
            port, float(np.real(amplitude)), duration, registers, gate_qubits, drive_hz
        )

    def _run_flux(
        self,
        port: str,
        amplitude: float,
        duration: float,
        registers: _Registers,
        gate_qubits: tuple[str, ...],
        drive_hz: float = 0.0,
    ) -> None:
        """Evolve the coupled pair for *duration* at flux *amplitude*."""
        if duration <= 0:
            return
        head = _qubit_of(port)
        if head is None:
            raise SimulationError(
                f"a flux pulse on {port!r} names no qubit, so there is nothing to "
                "detune"
            )

        # A coupler's port names the *edge* — `q1_q2:fl` — and that is the most
        # reliable statement of which pair the pulse acts on. It has to be,
        # because a `FluxTunableCoupler` lowers its CZ into a fresh subschedule
        # and the gate-level `device_elements` does not survive into it. A
        # qubit's own flux port (`q1:fl`, the CompositeSquareEdge case) names
        # only the qubit being detuned, and there the partner comes from the
        # gate.
        pair = _edge_qubits(head)
        if pair is not None:
            control, target = pair
            register = registers.join(control, target)
            # A coupler's pulse rides a microwave clock, and that frequency is
            # the gate's resonance condition rather than a carrier detail: a
            # sideband drive off the transition it means to bridge does nothing
            # at all. A qubit's own flux line is DC and takes the other branch.
            self._parametric(register, control, target, amplitude, duration, drive_hz)
            for other in registers.distinct():
                if other is not register:
                    self._idle(other, duration)
            return

        control = head
        partners = [name for name in gate_qubits if name != control]
        if len(partners) != 1:
            # Not every flux pulse is a two-qubit gate. `flux_spectroscopy`
            # holds a DC bias on one qubit's own port to move its frequency and
            # then looks for the line — no partner, no coupling, and treating it
            # as a CZ made the routine fail with a complaint about a pair it
            # never mentioned. A flux pulse whose enclosing gate names no second
            # qubit is a frequency shift of the one it names.
            others = [name for name in registers.names() if name != control]
            if len(others) != 1:
                self._flux_detune(registers.of(control), control, amplitude, duration)
                return
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

    def _flux_detune(
        self, register: _Register, qubit: str, amplitude: float, duration: float
    ) -> None:
        """A DC flux bias on one qubit: its frequency moves while the pulse lasts.

        Down, and quadratically, for the reason :data:`FLUX_CURVATURE_GHZ` gives —
        a transmon sits at a sweet spot where the first derivative of frequency
        with flux vanishes. The shift is restored afterwards because the bias is
        a pulse rather than a parking current; a `flux_spectroscopy` schedule
        that drives *after* the pulse rather than during it therefore sees an
        unshifted qubit, which is a property of the schedule and not of this.
        """
        from qpi_driver.simulation.coupled import FLUX_CURVATURE_GHZ

        shift = -FLUX_CURVATURE_GHZ * float(amplitude) ** 2 * GHZ
        before = register.detunings.get(qubit, 0.0)
        register.detunings[qubit] = before + shift
        try:
            self._idle(register, duration)
        finally:
            register.detunings[qubit] = before

    def _parametric(
        self,
        register: _Register,
        parent: str,
        child: str,
        amplitude: float,
        duration: float,
        drive_hz: float,
    ) -> None:
        """A parametrically driven coupler: the CZ a `FluxTunableCoupler` plays.

        The coupler is modulated at microwave frequency and a sideband of that
        modulation bridges ``|11⟩``–``|02⟩``. Amplitude sets how fast the
        exchange runs; the *frequency* sets whether it runs at all, so a drive
        off the transition produces a gate that compiles, plays, and does
        nothing — which is precisely the failure a fixed-carrier model could
        not show.
        """
        import qutip

        from qpi_driver.simulation.coupled import (
            PARAMETRIC_RATE_MHZ,
            SIDEBAND_GAP_GHZ,
            STARK_ASYMMETRY,
            STARK_SHIFT_MHZ,
        )

        levels = register.levels
        gap_ghz = self.sideband_gaps.get(f"{parent}_{child}", 0.0) / GHZ or (
            SIDEBAND_GAP_GHZ
        )
        detuning = 2 * np.pi * (gap_ghz - drive_hz / GHZ)
        rate = 2 * np.pi * PARAMETRIC_RATE_MHZ * 1e-3 * abs(amplitude)
        stark = 2 * np.pi * STARK_SHIFT_MHZ * 1e-3 * amplitude**2

        def transition(low: int, high: int, qubit: str):
            return register.embed(
                qutip.basis(levels, low) * qutip.basis(levels, high).dag(), qubit
            )

        # |11><02| factorises across the two qubits, and operators on different
        # qubits commute — so this is the pair's exchange term wherever the two
        # happen to sit in the register, with any spectators left alone.
        raising = transition(1, 0, parent) * transition(1, 2, child)
        excited_02 = register.embed(
            qutip.basis(levels, 2) * qutip.basis(levels, 2).dag(), child
        )
        parent_number = register.ladder(parent).dag() * register.ladder(parent)
        child_number = register.ladder(child).dag() * register.ladder(child)

        hamiltonian = (
            detuning * excited_02
            + rate * (raising + raising.dag())
            + stark * parent_number
            + STARK_ASYMMETRY * stark * child_number
        )
        self._sample_cache.clear()
        self._propagate(register, hamiltonian, duration)

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
                duration=float(acquisition.get("duration") or 0.0),
                threshold=float(acquisition.get("acq_threshold") or 0.0),
                rotation=float(acquisition.get("acq_rotation") or 0.0),
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
        """The shape a real cluster returns, which depends on the protocol asked for.

        The three acquisition protocols do not differ only in how much averaging
        they do — they return different *kinds* of thing, and a simulator that
        returns integrated IQ for all three lets a job take a code path it would
        never take against hardware:

        - ``Trace`` (``meas_level=0``) is a time series, one sample per
          nanosecond across the integration window. Returning a single point
          instead makes a raw-waveform job look like it worked and produce a
          one-sample waveform.
        - ``ThresholdedAcquisition`` (``meas_level=2``) is discriminated *on the
          instrument* and comes back as 0 or 1. Returning IQ here means the
          executor silently falls through to discriminating in software, so the
          hardware path — the one production uses — goes untested.
        - ``SSBIntegrationComplex`` (``meas_level=1``) is the integrated IQ
          point, which is the only one of the three this used to be right about.

        Within that, `BinMode.AVERAGE` gives one value per acquisition index and
        `BinMode.APPEND` one per shot.
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

            if any(entry.protocol == "Trace" for entry in entries):
                traces = np.stack([self._trace(entry) for entry in entries], axis=0)
                # One trace per acquisition index, sampled along time. A Trace is
                # averaged over repetitions on the instrument, so there is no
                # shot axis here however many shots were asked for.
                variables[channel] = ((axis, f"trace_index_{channel}"), traces)
                coordinates[f"trace_index_{channel}"] = np.arange(traces.shape[1])
                coordinates[axis] = np.array([entry.index for entry in entries])
                continue

            if single_shot:
                shots = np.stack(
                    [self._single_shots(entry) for entry in entries], axis=-1
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

    def _single_shots(self, entry: _Acquisition) -> np.ndarray:
        """One value per shot, in whatever the entry's protocol returns."""
        outcomes = (
            self._rng.random(self._repetitions) < entry.excited_population
            if entry.outcomes is None
            else np.asarray(entry.outcomes, dtype=bool)
        )
        points = self._blobs(outcomes)
        if entry.protocol != "ThresholdedAcquisition":
            return points

        # What the instrument does: rotate the IQ plane so the two blobs
        # separate along the real axis, then compare against the threshold. The
        # simulator runs the same rule rather than reporting `outcomes`
        # directly, so a device configured with the wrong rotation or threshold
        # produces the wrong bits here exactly as it would on the bench.
        rotated = points * np.exp(-1j * np.deg2rad(entry.rotation))
        return (np.real(rotated) >= entry.threshold).astype(np.int32)

    def _trace(self, entry: _Acquisition) -> np.ndarray:
        """The readout resonator's response over the integration window.

        A `Trace` is what the digitiser saw, so it has to *ring up* rather than
        appear at its final value: the resonator fills at its own linewidth, and
        only the settled part of the trace carries the qubit's state. Sampled at
        1 GSa/s, which is the Qblox digitiser's rate.

        The noise here is per sample and much larger than on an integrated
        point, because integrating the window is what averages it down — that
        ratio is most of why a trace looks the way it does.
        """
        samples = max(int(round(entry.duration / NS)), 1)
        times = np.arange(samples, dtype=float)

        settled = GROUND_IQ * (1 - entry.excited_population) + (
            EXCITED_IQ * entry.excited_population
        )
        # Ring-up at the resonator linewidth: kappa in GHz gives ns directly.
        tau = 1.0 / (2 * np.pi * max(self.simulator.readout_linewidth_ghz, 1e-6))
        envelope = 1.0 - np.exp(-times / tau)

        noise = self._rng.normal(0.0, READOUT_NOISE, samples) + 1j * self._rng.normal(
            0.0, READOUT_NOISE, samples
        )
        return settled * envelope + noise

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


# --- reading either scheduler's compiled schedule ---------------------------
#
# quantify-scheduler and qblox-scheduler describe a compiled schedule almost
# identically and not quite. The differences are small, undocumented, and would
# otherwise be scattered through the walk below, so they are named here instead:
#
# - `pulse_info` and `acquisition_info` are a *list* of dicts under quantify and
#   a single dict under qblox;
# - a subschedule is a `Schedule` with `.operations` under quantify, and a
#   `TimeableSchedule` carrying `operation_dict` inside `.data` under qblox —
#   and qblox nests them several deep where quantify has one level;
# - a drive pulse's amplitude is `G_amp` under quantify and `amplitude` under
#   qblox, with `amp` used by both for square pulses.
#
# quantify-scheduler is being deprecated, so the qblox spelling is the one that
# has to keep working; supporting both is what makes that transition a
# non-event rather than a rewrite of the simulator.


def _operations_of(schedule: Any) -> Any:
    """The operation table of a compiled schedule, by either spelling."""
    operations = getattr(schedule, "operations", None)
    if operations is not None:
        return operations
    return schedule.data["operation_dict"]


def _is_subschedule(operation: Any) -> bool:
    """Whether an operation is itself a schedule to descend into."""
    if getattr(operation, "schedulables", None) is not None:
        return True
    data = getattr(operation, "data", None)
    return isinstance(data, dict) and "schedulables" in data


def _infos(operation: Any, key: str) -> list[dict]:
    """``pulse_info`` or ``acquisition_info`` as a list, whichever shape it is."""
    data = getattr(operation, "data", None)
    if not isinstance(data, dict):
        return []
    info = data.get(key)
    if not info:
        return []
    return list(info) if isinstance(info, (list, tuple)) else [info]


def _amplitude_of(pulse: dict) -> Any:
    """A drive pulse's amplitude, by whichever key the scheduler used."""
    for key in ("G_amp", "amplitude", "amp"):
        if pulse.get(key) is not None:
            return pulse[key]
    return None


def _edge_qubits(name: str) -> tuple[str, str] | None:
    """The pair an edge name joins, or ``None`` if *name* is not one.

    ``q1_q2`` is the two qubits a coupler sits between — the same
    ``<parent>_<child>`` form the device config and the routines use.
    """
    parts = name.split("_")
    if len(parts) != 2 or not all(parts):
        return None
    return parts[0], parts[1]


def _qubit_of(identifier: str) -> str | None:
    """The qubit a clock or port belongs to: ``q0.01`` and ``q0:mw`` are both q0."""
    for separator in (".", ":"):
        if separator in identifier:
            head = identifier.split(separator)[0]
            return head or None
    return identifier or None
