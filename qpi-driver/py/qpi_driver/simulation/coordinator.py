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

**What is modelled.** Drive pulses on a qubit's ``.01`` clock, idles and resets,
clock detuning from ``SetClockFrequency``, relaxation and dephasing between
operations, and a readout through a resonator.

Drive pulses by their actual envelope. A ``Rxy`` compiles to a DRAG pulse — a
Gaussian with a derivative component on the other quadrature — and the derivative
is what cancels the phase error a fast pulse picks up from the ``|1>``-``|2>``
transition. Because it is a *derivative*, a model that averaged the pulse to a
constant amplitude made the Motzoi parameter do nothing at all; so a shaped pulse
is integrated in steps and the leakage that DRAG corrects comes from the same
three-level ladder that produces it.

Readout through a resonator, so the readout chain is calibratable rather than
assumed: the response has a linewidth to find and a power at which it moves. See
:mod:`qpi_driver.simulation.resonator`. Reading out at the wrong frequency
therefore costs contrast everywhere downstream, which is the coupling that makes
`resonator_spectroscopy` worth running.

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

**What is not.** Crosstalk, the two qubit states pulling the resonator to two
different frequencies (:mod:`qpi_driver.simulation.resonator` says what follows
from that), and a flux pulse whose partner cannot be identified — which raises
rather than guessing, since the wrong partner is a plausible-looking gate between
the wrong qubits.
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

#: Per-shot scatter about a cloud's centre, per quadrature. Against the cloud
#: separation — which `TransmonSimulator.readout_gain` sets — this is the
#: single-shot readout fidelity a discriminator can reach.
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
    #: The marginal probability of each level, indexed by level. A vector rather
    #: than one excited fraction because the transmon has three rungs and the third
    #: is not a variant of the second: leakage into ``|2>`` lands somewhere else in
    #: the IQ plane, and an acquisition that carried only ``P(excited)`` reported it
    #: as one of the other two. That is what made three-state readout unmeasurable
    #: while the dynamics underneath were already right.
    populations: tuple[float, ...]
    #: Per-shot outcomes for this qubit, as level indices, drawn jointly with the
    #: rest of its register. ``None`` for a qubit entangled with nothing, where an
    #: independent draw from the marginal is the same thing.
    outcomes: Any = None
    #: Integration window, in seconds — the length of a `Trace`.
    duration: float = 0.0
    #: What a `ThresholdedAcquisition` discriminates against, as the hardware
    #: does it: rotate the IQ point by *rotation* degrees, compare the real part
    #: against *threshold*.
    threshold: float = 0.0
    rotation: float = 0.0
    #: Where each qubit level lands in the IQ plane for *this* acquisition, indexed
    #: by level. Derived from the resonator's complex response at the frequency and
    #: power this readout used, then through the amplifier chain — so the clouds move
    #: when the readout is mistuned, rather than being two fixed points. Empty when
    #: the schedule never mentioned this qubit's readout clock, where there is
    #: nothing to say the readout is off and a perfect one is assumed.
    clouds: tuple[complex, ...] = ()
    #: Seconds of dead time at the front of a `Trace`: how much longer the signal
    #: takes to arrive than this window waited for it. Negative means the window
    #: opened late and the front of the signal was missed. What `time_of_flight`
    #: measures, and zero when ``acq_delay`` already matches the wiring.
    dead_time: float = 0.0


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
        parking_currents: dict[str, float] | None = None,
    ) -> None:
        self.simulator = simulator or TransmonSimulator()
        #: Edge name to the ``|11>-|02>`` gap its CZ drive is meant to bridge,
        #: in Hz, as that edge's own config declares it. An edge that has not
        #: been characterised falls back to `SIDEBAND_GAP_GHZ` — which is a
        #: chosen number, so a device that knows its own gap should say so.
        self.sideband_gaps = dict(sideband_gaps or {})
        #: Edge name to the DC current its coupler is parked at, in amperes, as that
        #: edge's config declares it. The bias is delivered out of band — see
        #: `qpi_driver.executors.utils.coupler_bias` — so it reaches the simulator as
        #: state rather than as a pulse, which is exactly what it is on a chip.
        self.parking_currents = dict(parking_currents or {})
        #: The coupler as a mode of its own. What responds to the currents above.
        self._coupler: Any = None
        self.drive_strength = drive_strength
        self._rng = np.random.default_rng(seed)
        self._compiled: Any = None
        self._drift_cache: dict[float, Any] = {}
        self._acquisitions: list[_Acquisition] = []
        self._repetitions = 1
        #: Flux port to the (amplitude, start time) of an offset currently held.
        #: Port to (amplitude, start time, drive frequency) for a flux pulse the
        #: backend emitted as a held offset. The frequency belongs here because a
        #: *parametric* coupler drive rides a microwave clock, and dropping it
        #: leaves the held part detuned to nothing.
        self._flux_offsets: dict[str, tuple[float, float, float]] = {}
        #: Clock to its accumulated virtual-Z phase, in degrees.
        self._clock_phases: dict[str, float] = {}
        #: Register id to its per-shot joint outcomes, dropped as it evolves.
        self._sample_cache: dict[int, dict[str, Any]] = {}
        #: Readout clock to the amplitude last played on it. A punchout sweep
        #: changes this between acquisitions, which is the whole experiment.
        self._readout_amps: dict[str, float] = {}
        #: Readout clock to when its pulse began, so an acquisition knows how long
        #: after the pulse its window opened — which is what `time_of_flight`
        #: measures against the wiring's own delay.
        self._readout_starts: dict[str, float] = {}
        #: Each qubit's resonator, built once per schedule from where its readout
        #: clock started — see `TransmonSimulator.resonator`.
        self._resonators: dict[str, Any] = {}
        #: Whole-pulse propagators, keyed by everything they depend on. A shaped
        #: pulse is integrated in steps, and a benchmarking schedule replays the
        #: same few pulses thousands of times.
        self._propagator_cache: dict[Any, Any] = {}

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
        self._readout_amps = {}
        self._readout_starts = {}
        registers = _Registers(self.simulator)
        clocks = self._clock_frequencies(compiled)
        # Before any `SetClockFrequency` is replayed: a spectroscopy sweep moves
        # the readout clock across the resonator, so taking the resonator's own
        # frequency from the swept clock would make it follow the drive and the
        # routine would fit a flat line.
        self._resonators = {
            qubit: self.simulator.resonator(qubit, frequency / GHZ)
            for clock, frequency in clocks.items()
            if _is_readout_clock(clock) and (qubit := _qubit_of(clock))
        }
        acquisitions: list[_Acquisition] = []
        last_time: dict[str, float] = {}

        for time, operation, gate_qubits in self._flatten(compiled):
            for pulse in _infos(operation, "pulse_info"):
                self._apply_pulse(
                    pulse, time, registers, clocks, last_time, gate_qubits
                )
            for acquisition in _infos(operation, "acquisition_info"):
                self._acquire(acquisition, registers, acquisitions, clocks, time)

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

        Acquisitions sort after pulses that start at the same instant. A `Measure`
        lowers to a readout pulse and an acquisition both at ``t0``, and the pulse
        is what says at which frequency and power the resonator was interrogated;
        reading them in the other order would answer each acquisition with the
        *previous* one's readout settings.
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
        return sorted(
            found, key=lambda item: (item[0], bool(_infos(item[1], "acquisition_info")))
        )

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

        if _is_readout_clock(clock):
            # The resonator is interrogated at whatever amplitude this pulse
            # carries. A long readout arrives as a held DC offset plus a short
            # square tail — the same shape a flux pulse does — so the amplitude
            # is whichever of those was non-zero, and the zero that clears the
            # offset must not be mistaken for a readout at zero power.
            amplitude = _amplitude_of(pulse)
            if amplitude is None:
                amplitude = pulse.get("offset_path_I")
            if amplitude is not None and abs(np.real(amplitude)) > 0:
                self._readout_amps[clock] = float(abs(np.real(amplitude)))
                # The *first* non-zero amplitude since the last acquisition, which
                # is when the pulse began. A long readout arrives as a held offset
                # plus a short square tail, so taking the latest would report the
                # tail's start — 296 ns into a 300 ns pulse — and `time_of_flight`
                # would measure the wrong interval.
                self._readout_starts.setdefault(
                    clock, time + float(pulse.get("t0") or 0.0)
                )
            for register in registers.distinct():
                self._idle(register, duration)
            return

        qubit = _qubit_of(clock) or _qubit_of(port)
        if qubit is not None and clock.endswith(".12"):
            amplitude = _amplitude_of(pulse)
            register = registers.of(qubit)
            if amplitude is None or duration <= 0:
                self._idle(register, duration)
                return
            # Where this pulse's clock sits relative to the frame the register is
            # kept in — which is the 0-1 drive frame, at f01 plus whatever detuning
            # the last drive left there.
            frame_hz = self.simulator.f01 * GHZ + register.detunings.get(qubit, 0.0)
            self._drive_ef(
                register,
                qubit,
                amplitude=float(amplitude),
                duration=duration,
                phase_deg=float(pulse.get("phase") or 0.0)
                + self._clock_phases.get(clock, 0.0),
                frame_offset_hz=clocks.get(clock, self._f12_hz()) - frame_hz,
                shape=_envelope_of(pulse),
                origin_ns=(time + float(pulse.get("t0") or 0.0)) / NS,
            )
            for other in registers.distinct():
                if other is not register:
                    self._idle(other, duration)
            return

        if qubit is None or not clock.endswith(".01"):
            # A baseband idle, anything not driving a qubit: it still takes time,
            # and time is where decoherence happens.
            for register in registers.distinct():
                self._idle(register, duration)
            return

        register = registers.of(qubit)
        register.detunings[qubit] = clocks.get(clock, 0.0) - self._qubit_frequency_hz(
            qubit
        )

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
            shape=_envelope_of(pulse),
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
                self._flux_offsets[port] = (amplitude, start, drive_hz)
                return
            held = self._flux_offsets.pop(port, None)
            if held is not None:
                # With the frequency the offset was set at. A baseband CZ does not
                # care — its resonance is the flux amplitude — but a parametric one
                # is nothing without it: the backend splits a 100 ns coupler drive
                # into 96 ns of held offset plus a 4 ns tail, and replaying the held
                # part at zero left 96% of the gate infinitely detuned. The gate ran
                # at 4% of its length and looked simply weak.
                self._run_flux(
                    port,
                    held[0],
                    start - held[1],
                    registers,
                    gate_qubits,
                    held[2],
                )
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
        shape: "_Envelope | None" = None,
        detuning_rad_per_ns: float = 0.0,
        origin_ns: float = 0.0,
    ) -> None:
        """Evolve under a drive of *amplitude* for *duration*.

        The rotation is not written down — it falls out of the drive, the
        detuning and the collapse operators evolving together, which is what
        makes an off-resonant or mis-scaled pulse produce the wrong population
        rather than a nominally correct one.

        A square pulse is one constant Hamiltonian. A DRAG pulse is not: its
        Gaussian envelope and the derivative quadrature that rides on it both
        vary across the pulse, and the derivative is *defined* by that variation
        — averaged to a constant it is identically zero, which is what made the
        Motzoi parameter a no-op here. So a shaped pulse is stepped.
        """
        self._sample_cache.clear()
        if shape is None and not detuning_rad_per_ns:
            drive = self._constant_drive(register, qubit, amplitude, phase_deg)
            self._propagate(register, self._drift(register) + drive, duration)
            return
        # A drive off the frame's own frequency is time-dependent even when its
        # envelope is flat — the term rotates at the offset — so it steps too.
        self._propagate_shaped(
            register,
            qubit,
            amplitude,
            duration,
            phase_deg,
            shape,
            detuning_rad_per_ns,
            origin_ns,
        )

    def _constant_drive(
        self, register: _Register, qubit: str, amplitude: float, phase_deg: float
    ):
        """A square pulse's drive term: ``(Omega/2)(e^-iphi a + e^iphi a-dagger)``."""
        import qutip

        destroy = qutip.destroy(self.simulator.levels)
        rabi = self.drive_strength * amplitude * NS  # rad/ns
        phase = np.deg2rad(phase_deg)
        return register.embed(
            (rabi / 2)
            * (np.exp(-1j * phase) * destroy + np.exp(1j * phase) * destroy.dag()),
            qubit,
        )

    def _f12_hz(self) -> float:
        """The ``|1>``-``|2>`` transition of the simulated transmon, in Hz."""
        return (self.simulator.f01 + self.simulator.anharmonicity) * GHZ

    def _drive_ef(
        self,
        register: _Register,
        qubit: str,
        amplitude: float,
        duration: float,
        phase_deg: float,
        frame_offset_hz: float,
        shape: "_Envelope | None" = None,
        origin_ns: float = 0.0,
    ) -> None:
        """A drive on the ``.12`` clock: the ``|1>``-``|2>`` transition.

        An ordinary drive on the full ladder, in the *same* frame as everything else,
        offset from that frame by where its clock sits. It used to be a two-level
        ``|1>``-``|2>`` subspace in a rotating frame of its own, and three things
        followed from that which are no longer true:

        - **Frames disagreed.** The pulses ran in the EF frame while the idles between
          them ran in the 0-1 frame, so the phase between two EF pulses accumulated at
          the whole anharmonicity. Sweeping a delay on a 1 ns grid swung ``P(|2>)``
          from 0.008 to 0.978 — a Ramsey there measured the mismatch, not the qubit.
        - **There was no ladder.** The 1-2 matrix element was folded into the subspace
          operator, so an EF pi pulse took the same amplitude as an 0-1 one. The real
          ladder gives ``sqrt(2)``, and it now falls out: a pi lands at ``amp180 /
          sqrt(2)``.
        - **There was nothing to leak into.** ``|0>`` was a spectator, so an EF pulse
          could not off-resonantly excite the 0-1 transition and `drag_12` would have
          had a flat line to fit. It leaks about a part in a thousand here, which is
          what a DRAG quadrature exists to cancel.

        The cost is that this always steps: a drive off its frame's own frequency is
        time-dependent however flat its envelope. At the anharmonicity that is under
        six cycles across a 20 ns pulse, and the population converges by 40 steps.
        """
        if register.levels < 3:
            raise SimulationError(
                "a drive on the .12 clock needs a three-level transmon; this "
                f"simulator has {register.levels}"
            )
        self._sample_cache.clear()
        self._drive(
            register,
            qubit,
            amplitude=amplitude,
            duration=duration,
            phase_deg=phase_deg,
            shape=shape,
            # Negative, which is not the obvious sign and is not a free choice: it is
            # what pairs with `_drift`'s ``+(f_drive - f_qubit)`` and the ``e^{+i phi}``
            # on the raising operator. The other sign drives nothing at all — measured,
            # `P(|2>)` stays under 0.003 at every amplitude — while this one puts a pi
            # exactly at ``amp180 / sqrt(2)``.
            detuning_rad_per_ns=-2 * np.pi * frame_offset_hz * NS,
            origin_ns=origin_ns,
        )

    def _propagate_shaped(
        self,
        register: _Register,
        qubit: str,
        amplitude: float,
        duration: float,
        phase_deg: float,
        shape: "_Envelope | None",
        detuning_rad_per_ns: float = 0.0,
        origin_ns: float = 0.0,
    ) -> None:
        """Step a shaped pulse, composing the steps into one cached propagator.

        The envelope is normalised to unit *mean* over the pulse, so the rotation
        angle stays the pulse area — ``drive_strength x amplitude x duration``,
        exactly what a square pulse of the same amplitude would give. That is not
        cosmetic: it keeps `amp180` meaning what it meant before shapes were
        modelled, so the number Rabi finds here is the number it found before, and
        the DRAG term is an addition rather than a recalibration.

        The derivative quadrature is the whole point of DRAG. Its coefficient is
        in seconds and multiplies ``d(envelope)/dt``; the two schedulers spell it
        differently and :func:`_envelope_of` converts both to that form.
        """
        import qutip

        if duration <= 0:
            return
        steps = _drive_steps(duration)
        key = (
            register.signature(),
            qubit,
            round(amplitude, 12),
            round(duration, 15),
            round(phase_deg % 360.0, 9),
            round(shape.drag_seconds, 18) if shape else None,
            round(shape.sigma, 15) if shape else None,
            round(detuning_rad_per_ns, 12),
            # Only the phase the origin contributes, wrapped: two pulses a whole
            # number of frame turns apart are the same propagator, and an EF sweep
            # of hundreds of points would otherwise miss the cache every time.
            round((detuning_rad_per_ns * origin_ns) % (2 * np.pi), 9),
            steps,
        )
        propagator = self._propagator_cache.get(key)
        if propagator is None:
            propagator = self._compose_shaped(
                register,
                qubit,
                amplitude,
                duration,
                phase_deg,
                shape,
                steps,
                detuning_rad_per_ns,
                origin_ns,
            )
            if len(self._propagator_cache) < _PROPAGATOR_CACHE_LIMIT:
                self._propagator_cache[key] = propagator
        register.rho = qutip.vector_to_operator(
            propagator * qutip.operator_to_vector(register.rho)
        )

    def _compose_shaped(
        self,
        register: _Register,
        qubit: str,
        amplitude: float,
        duration: float,
        phase_deg: float,
        shape: "_Envelope | None",
        steps: int,
        detuning_rad_per_ns: float = 0.0,
        origin_ns: float = 0.0,
    ):
        import qutip

        drift = self._drift(register)
        collapse = register.collapse()
        destroy = register.ladder(qubit)
        raising = destroy.dag()
        step_ns = (duration / NS) / steps

        # Midpoints, so the piecewise-constant sampling is second order rather
        # than first, and the derivative's two lobes stay balanced.
        offsets = (np.arange(steps) + 0.5) * (duration / steps) - duration / 2.0
        if shape is None:
            # A square pulse, stepped only because its *frame* offset makes it
            # time-dependent. Flat envelope, no derivative quadrature.
            envelope = np.ones(steps)
            derivative = np.zeros(steps)
            drag_seconds = 0.0
        else:
            sigma = shape.sigma
            envelope = np.exp(-0.5 * (offsets / sigma) ** 2)
            mean = float(envelope.mean()) or 1.0
            envelope = envelope / mean
            # d/dt of that same normalised Gaussian, analytically.
            derivative = -(offsets / sigma**2) * envelope
            drag_seconds = shape.drag_seconds

        # How far the drive's own frequency sits from the frame the register is
        # kept in. Zero for a drive on its own clock; the anharmonicity for an EF
        # pulse, which is played in the 0-1 frame like everything else so that the
        # idles between pulses keep the same time as the pulses do.
        # Absolute schedule time, not time within the pulse. The frame keeps turning
        # between pulses, and a drive that restarted its phase at each one would leave
        # the delay in a Ramsey uncounted — the fringe then reports where the two
        # pulses happened to land rather than the detuning between them.
        turning = np.exp(
            1j * detuning_rad_per_ns * (origin_ns + offsets / NS + duration / NS / 2)
        )

        rabi = self.drive_strength * amplitude * NS  # rad/ns
        phase = np.exp(1j * np.deg2rad(phase_deg))
        composed = None
        for index in range(steps):
            # The played waveform: Gaussian on I, its derivative on Q, the pair
            # rotated by the pulse phase.
            # `drag_seconds` is in seconds and the derivative in inverse seconds,
            # so their product is the dimensionless quadrature ratio.
            wave = (
                rabi
                * (envelope[index] + 1j * drag_seconds * derivative[index])
                * phase
                * turning[index]
            )
            hamiltonian = drift + (wave * raising + np.conj(wave) * destroy) / 2
            step = (qutip.liouvillian(hamiltonian, collapse) * step_ns).expm()
            composed = step if composed is None else step * composed
        return composed

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

    def _qubit_frequency_hz(self, qubit: str) -> float:
        """*qubit*'s frequency, including whatever its couplers push it by.

        A parked coupler is not a passive part of the chip: it repels every qubit it
        touches, and how far depends on the current holding it. So a bias written to
        the device *moves the qubit*, which is the whole reason
        `coupler_anticrossing` can find the crossings by looking at a qubit at all.

        Measured from zero bias, so an unparked chip — every ``parking_current`` at
        its default of zero — reports exactly the frequency it always did.
        """
        base = self.simulator.f01 * GHZ
        if not self.parking_currents:
            return base
        from qpi_driver.simulation.coupled import TunableCoupler

        if self._coupler is None:
            self._coupler = TunableCoupler()
        shift = 0.0
        for edge, current in self.parking_currents.items():
            if qubit not in edge.split("_"):
                continue
            shift += self._coupler.push_ghz(self.simulator.f01, current) * GHZ
        # No cache to invalidate: `_Register.signature` keys the drift on the
        # detunings, and a moved qubit frequency *is* a different detuning.
        return base + shift

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
        clocks: dict[str, float],
        time: float = 0.0,
    ) -> None:
        clock = str(acquisition.get("clock") or "")
        qubit = (
            _qubit_of(clock) or _qubit_of(str(acquisition.get("port") or "")) or "q0"
        )
        register = registers.of(qubit)
        # The marginal, so reading one half of a Bell pair averages to the 50/50
        # it physically is. Every level of it, not just the first excited one.
        populations = tuple(
            float(np.clip(register.population(qubit, level), 0.0, 1.0))
            for level in range(register.levels)
        )
        found.append(
            _Acquisition(
                channel=int(acquisition.get("acq_channel") or 0),
                index=int(acquisition.get("acq_index") or 0),
                protocol=str(acquisition.get("protocol") or ""),
                bin_mode=str(acquisition.get("bin_mode") or "average"),
                populations=populations,
                outcomes=self._joint_outcomes(register).get(qubit),
                duration=float(acquisition.get("duration") or 0.0),
                threshold=float(acquisition.get("acq_threshold") or 0.0),
                rotation=float(acquisition.get("acq_rotation") or 0.0),
                clouds=self._clouds(qubit, clock, clocks),
                dead_time=self._dead_time(
                    clock, time + float(acquisition.get("t0") or 0.0)
                ),
            )
        )
        # The next acquisition on this clock belongs to the next readout pulse.
        self._readout_starts.pop(clock, None)

    def _dead_time(self, clock: str, window_opened: float) -> float:
        """How long the front of this window waited with nothing in it, in seconds.

        The signal arrives ``time_of_flight_ns`` after the readout pulse begins, and
        the window opens ``acq_delay`` after it. The difference is what a raw trace
        shows as dead samples, and what `time_of_flight` exists to remove: setting
        ``acq_delay`` to the wiring's delay makes this zero.

        Negative when the window opened *late*, which loses the front of the signal
        rather than wasting the front of the window — the more expensive mistake, and
        the reason this is signed rather than clamped.
        """
        started = self._readout_starts.get(clock)
        if started is None:
            return 0.0
        return self.simulator.time_of_flight_ns * NS - (window_opened - started)

    def _clouds(
        self, qubit: str, clock: str, clocks: dict[str, float]
    ) -> tuple[complex, ...]:
        """Where each qubit level lands in the IQ plane for this acquisition.

        Everything a readout is calibrated for meets here. The clock the resonator is
        interrogated at, swept by `resonator_spectroscopy`; the power it is driven
        with, swept by `resonator_punchout`; the dispersive pull, which puts each
        level at its own resonance and so at its own complex response; and the
        amplifier chain's gain and rotation, which is what
        `readout_discrimination` has to undo.

        A qubit whose readout clock this schedule never mentions gets an empty tuple
        and the caller falls back to a perfect readout, since there is nothing here to
        say the readout is off.
        """
        resonator = self._resonators.get(qubit)
        if resonator is None or clock not in clocks:
            return ()
        drive = clocks[clock] / GHZ
        amplitude = self._readout_amps.get(clock, 0.0)
        # Linear in the drive amplitude, because the reflected field is. This is the
        # half of readout power that pushes *for* more of it — more signal against a
        # noise floor that does not move — and `punched_through` inside `reflection`
        # is the half that pushes back, by collapsing the pull that made the states
        # distinguishable. `readout_amplitude_two_state` exists to find where the two
        # balance, and cannot if the response ignores the power.
        chain = (
            self.simulator.readout_gain
            * amplitude
            * np.exp(1j * np.deg2rad(self.simulator.readout_phase(qubit)))
        )
        return tuple(
            complex(chain * resonator.reflection(drive, amplitude, level))
            for level in range(self.simulator.levels)
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
            # The level itself, not "is it excited". Collapsing to a bit here is what
            # made a shot in |2> indistinguishable from one in |1>; a discriminator
            # may still choose to report one bit, but that is its decision to make
            # further down, where the clouds are known.
            outcomes[name] = (draws // divisor) % levels
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
                values = np.array([self._averaged(entry) for entry in entries])
                variables[channel] = ((axis,), values)
            coordinates[axis] = np.array([entry.index for entry in entries])

        if not variables:
            return xr.Dataset()
        return xr.Dataset(variables, coords=coordinates)

    @staticmethod
    def _cloud_of(entry: _Acquisition, level: int) -> complex:
        """Where *level* lands in the IQ plane for this acquisition.

        The fallback is what a perfect readout at resonance would give — the ground
        state at its own place and everything above it at the origin. It is used only
        for an acquisition whose readout clock the schedule never mentioned, where
        there is nothing to derive, and it deliberately does not distinguish ``|1>``
        from ``|2>``: with no readout to model there is nothing to say they differ.
        """
        if level < len(entry.clouds):
            return entry.clouds[level]
        return complex(1.0, 0.0) if level == 0 else complex(0.0, 0.0)

    def _settled(self, entry: _Acquisition) -> complex:
        """The mean IQ point: every level's cloud, weighted by its population."""
        return sum(
            self._cloud_of(entry, level) * weight
            for level, weight in enumerate(entry.populations)
        )

    def _draw_levels(self, entry: _Acquisition) -> np.ndarray:
        """One level per shot, drawn from this acquisition's population vector.

        Ordered ``|1>`` first, then ``|0>``, then the rest — which looks arbitrary and
        is not. This used to be ``rng.random(n) < P(excited)``: one uniform per shot,
        excited when it fell below. Comparing against the cumulative distribution in
        that order consumes the same uniforms and makes the same decision, so a chip
        with no population above ``|1>`` draws exactly what it drew before.

        That matters more than the tidiness of counting up from zero. Every existing
        expectation about this simulator — pi pulse amplitudes, coherence times, gate
        fidelities — was measured against the old stream, and reordering it would move
        all of them at once. A test that then failed would be reporting a change in
        the random numbers rather than in the physics, with no way to tell which.
        """
        weights = np.asarray(entry.populations, dtype=float)
        if weights.size == 0:
            return np.zeros(self._repetitions, dtype=int)
        order = [1, 0, *range(2, weights.size)] if weights.size > 1 else [0]
        ordered = np.clip(weights[order], 0.0, None)
        total = ordered.sum()
        if total <= 0:
            return np.zeros(self._repetitions, dtype=int)

        draws = self._rng.random(self._repetitions)
        picked = np.searchsorted(np.cumsum(ordered / total), draws, side="right")
        return np.asarray(order, dtype=int)[np.clip(picked, 0, len(order) - 1)]

    def _single_shots(self, entry: _Acquisition) -> np.ndarray:
        """One value per shot, in whatever the entry's protocol returns."""
        levels = (
            self._draw_levels(entry)
            if entry.outcomes is None
            else np.asarray(entry.outcomes, dtype=int)
        )
        points = self._blobs(levels, entry)
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

        The noise here is per sample and much larger than on an integrated point,
        because integrating the window is what averages it down — that ratio is most
        of why a trace looks the way it does.

        It still falls with the shot count, though, and that correction was missing.
        A `Trace` is averaged over repetitions *on the instrument*, which is why
        there is no shot axis in the returned dataset; leaving the per-sample noise
        at its single-shot size while claiming the average had been taken made the
        two halves of that sentence contradict each other. It also made the front of
        a trace unreadable: `fit_readout_timing` locates the arrival by where the
        signal leaves its noise floor, and at single-shot noise the 10% level and the
        noise floor are the same number, so no arrival could be found at any shot
        count.
        """
        samples = max(int(round(entry.duration / NS)), 1)
        times = np.arange(samples, dtype=float)

        settled = self._settled(entry)
        # Nothing arrives until the signal has travelled. Before that the digitiser
        # samples noise, and how many samples that is is exactly what
        # `time_of_flight` measures — see `_dead_time`.
        arrival = entry.dead_time / NS
        # Ring-up at the resonator linewidth: kappa in GHz gives ns directly.
        tau = 1.0 / (2 * np.pi * max(self.simulator.readout_linewidth_ghz, 1e-6))
        envelope = np.where(
            times >= arrival, 1.0 - np.exp(-(times - arrival) / tau), 0.0
        )

        spread = READOUT_NOISE / np.sqrt(max(self._repetitions, 1))
        noise = self._rng.normal(0.0, spread, samples) + 1j * self._rng.normal(
            0.0, spread, samples
        )
        return settled * envelope + noise

    def _averaged(self, entry: _Acquisition) -> complex:
        """The mean IQ point, with the noise an averaged acquisition still has."""
        centre = self._settled(entry)
        spread = READOUT_NOISE / np.sqrt(max(self._repetitions, 1))
        return complex(
            centre.real + self._rng.normal(0.0, spread),
            centre.imag + self._rng.normal(0.0, spread),
        )

    def _blobs(self, levels: np.ndarray, entry: _Acquisition) -> np.ndarray:
        """The IQ point each already-drawn outcome lands on, with readout noise.

        The clouds move and the noise does not, which is the whole reason a mistuned
        readout is bad rather than merely different: off resonance the responses all
        shrink towards the origin and towards each other, so a discriminator that
        separated them cleanly stops being able to.
        """
        available = max(len(entry.clouds), int(np.max(levels, initial=0)) + 1)
        clouds = np.array([self._cloud_of(entry, n) for n in range(available)])
        centres = clouds[np.clip(levels, 0, available - 1)]
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


#: Steps per nanosecond when integrating a shaped pulse. Chosen by convergence
#: rather than by argument: the fitted DRAG optimum agrees to five significant
#: figures with a run at eight times this resolution, and the drift term is
#: exponentiated exactly at every step, so what the steps have to resolve is only
#: how the envelope varies.
_STEPS_PER_NS = 2
_MIN_DRIVE_STEPS = 16
_MAX_DRIVE_STEPS = 256

#: Cached propagators are one per distinct pulse, and a two-qubit register's is a
#: dense (levels^2)^2 matrix. Bounded so a long sweep cannot grow without limit;
#: past the bound the physics is the same and only the speed suffers.
_PROPAGATOR_CACHE_LIMIT = 512


def _drive_steps(duration: float) -> int:
    steps = int(round(duration / NS * _STEPS_PER_NS))
    return max(_MIN_DRIVE_STEPS, min(_MAX_DRIVE_STEPS, steps))


@dataclass(frozen=True)
class _Envelope:
    """A shaped pulse: its Gaussian width, and its derivative coefficient.

    Attributes:
        sigma: the Gaussian's width in seconds.
        drag_seconds: what multiplies ``d(envelope)/dt`` to give the quadrature
            component, in seconds. Both schedulers' DRAG parameters reduce to
            this — see :func:`_envelope_of` — and the leading-order optimum is
            ``-1/alpha`` for an anharmonicity ``alpha`` in rad/s, which for a
            normal transmon is a few hundred picoseconds.
    """

    sigma: float
    drag_seconds: float = 0.0


def _envelope_of(pulse: dict) -> "_Envelope | None":
    """A drive pulse's shape, or ``None`` for a square one.

    The two schedulers' DRAG parameters differ in units, and by exactly the
    factor that makes a sweep sized for one meaningless for the other:

    - quantify-scheduler's ``D_amp`` is the *ratio* of the derivative component
      to the Gaussian, dimensionless, validated to ``-1..1``. Its waveform is
      ``-D_amp x (t-mu)/sigma x G(t)``, so it multiplies ``dG/dt`` by
      ``D_amp x sigma``.
    - qblox-scheduler's ``beta`` is in *seconds*. Its waveform is
      ``-beta x (t-mu)/sigma^2 x G(t)``, which multiplies ``dG/dt`` by ``beta``
      directly.

    So the same physical pulse is ``beta = D_amp x sigma`` — a factor of 2.5 ns
    for a 20 ns gate. Reducing both to seconds here is what lets the physics
    below be written once, and is why the routine's default sweep has to come
    from the backend rather than be a constant.
    """
    function = str(pulse.get("wf_func") or "")
    if not function.endswith(("drag", "gauss")):
        return None
    duration = float(pulse.get("duration") or 0.0)
    if duration <= 0:
        return None

    sigma = pulse.get("sigma")
    if sigma:
        sigma = float(sigma)
    else:
        # Both schedulers: sigma = duration / (2 x nr_sigma), defaulting to 4.
        sigma = duration / (2 * float(pulse.get("nr_sigma") or 4))

    if pulse.get("beta") is not None:
        drag = float(pulse["beta"])
    elif pulse.get("D_amp") is not None:
        drag = float(pulse["D_amp"]) * sigma
    else:
        drag = 0.0
    return _Envelope(sigma=sigma, drag_seconds=drag)


def _is_readout_clock(clock: str) -> bool:
    """Whether *clock* is a qubit's readout clock: ``q0.ro``, ``q0.ro_2st_opt``.

    The suffix rather than an exact match, because a chip with optimal-weight
    readout declares several clocks per qubit and they all drive one resonator.
    """
    _, separator, suffix = clock.partition(".")
    return bool(separator) and suffix.startswith("ro")


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
