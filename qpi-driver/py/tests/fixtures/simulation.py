"""A physically realistic transmon, for testing routines against physics (RFC 0004 §7, tier 3).

Tiers 1 and 2 leave a gap. Tier 1 generates data from the same analytic form the
fit assumes — a decaying cosine in, a decaying cosine fitted — so it proves the
optimiser works but not that the model is the right one. Tier 2 proves a schedule
compiles, and the dummy cluster returns no data at all.

This closes it. The transmon's levels come from diagonalising a real Cooper-pair-box
Hamiltonian with `scqubits`, and the time-domain responses come from integrating the
Lindblad master equation with `qutip`. Nothing here is generated from a fitting
model, so a routine that recovers the simulator's parameters has been tested
against physics rather than against itself.

It is still not hardware: it is a two- or three-level model with Markovian noise,
no readout chain and no crosstalk. What it does catch is a routine whose schedule
does not produce the physics its fit assumes — which is the failure mode a dummy
cluster cannot show, and the one that matters most.

Three things are built on the simulator, in increasing order of how much of the
package they exercise:

- :class:`StubBackend` records what a routine composes and refuses to run it, for
  a test that supplies the acquisition itself.
- :class:`SimulatedBackend` answers ``run`` from the simulator, so a routine can
  be driven through ``build_schedule → run → analyse → apply`` unattended.
- :class:`SimulatedTuner` puts that behind the real :class:`Tuner`, which is what
  makes an end-to-end calibration — DAG walk, write-back, report — testable
  without a scheduler or an instrument.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import xarray as xr

from qpi_driver.tuners.base import Tuner

# Frequencies are in GHz and times in ns throughout this module, which is
# scqubits' convention. The routines work in Hz and seconds, so the conversions
# happen at the boundary — see `acquisition` on each method.
GHZ = 1e9
NS = 1e-9

#: A microsecond-scale free evolution over a two-point time list exhausts the
#: integrator's default step budget, which surfaces as "excess work done".
_SOLVER_OPTIONS = {"nsteps": 200_000}


@dataclass
class TransmonSimulator:
    """One transmon, its spectrum from scqubits and its dynamics from qutip.

    Attributes:
        EJ: Josephson energy in GHz.
        EC: charging energy in GHz. EJ/EC sets the anharmonicity, and the
            defaults give roughly -280 MHz, which is a normal transmon.
        t1_ns: relaxation time.
        t2_ns: total dephasing time, from which the pure-dephasing rate is
            derived given T1.
        readout_frequency_ghz: bare resonator frequency, for spectroscopy.
        readout_linewidth_ghz: resonator linewidth.
        seed: seeds the shot noise, so a failure is reproducible.
    """

    EJ: float = 15.0
    EC: float = 0.25
    ng: float = 0.0
    ncut: int = 30
    t1_ns: float = 30_000.0
    t2_ns: float = 20_000.0
    readout_frequency_ghz: float = 7.1
    readout_linewidth_ghz: float = 0.002
    shot_noise: float = 0.004
    seed: int = 20260731
    levels: int = 3

    _rng: np.random.Generator = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._rng = np.random.default_rng(self.seed)

    # --- the spectrum, from scqubits ------------------------------------------

    def eigenvalues(self, count: int | None = None) -> np.ndarray:
        """Transmon eigenenergies in GHz, by diagonalising the real Hamiltonian."""
        import scqubits

        transmon = scqubits.Transmon(EJ=self.EJ, EC=self.EC, ng=self.ng, ncut=self.ncut)
        return transmon.eigenvals(evals_count=count or self.levels)

    @property
    def f01(self) -> float:
        """The 0→1 transition in GHz. This is what qubit spectroscopy must find."""
        levels = self.eigenvalues(2)
        return float(levels[1] - levels[0])

    @property
    def anharmonicity(self) -> float:
        """f12 − f01 in GHz, negative for a transmon."""
        levels = self.eigenvalues(3)
        return float((levels[2] - levels[1]) - (levels[1] - levels[0]))

    # --- the dynamics, from qutip ---------------------------------------------

    def _operators(self):
        """Ladder, projector and collapse operators for a `levels`-level transmon."""
        import qutip

        n = self.levels
        destroy = qutip.destroy(n)
        excited = qutip.basis(n, 1) * qutip.basis(n, 1).dag()

        collapse = []
        if self.t1_ns > 0:
            collapse.append(np.sqrt(1.0 / self.t1_ns) * destroy)
        # Pure dephasing from 1/T2 = 1/(2·T1) + 1/T_phi.
        inverse_tphi = 1.0 / self.t2_ns - 1.0 / (2.0 * self.t1_ns)
        if inverse_tphi > 0:
            collapse.append(np.sqrt(2.0 * inverse_tphi) * destroy.dag() * destroy)
        return destroy, excited, collapse

    def _anharmonic_hamiltonian(self, detuning_ghz: float = 0.0):
        """The drive-frame Hamiltonian: detuning plus the transmon's anharmonicity.

        Angular frequency units (rad/ns), which is what qutip integrates in.
        """
        import qutip

        destroy = qutip.destroy(self.levels)
        number = destroy.dag() * destroy
        delta = 2 * np.pi * detuning_ghz
        alpha = 2 * np.pi * self.anharmonicity
        return delta * number + (alpha / 2) * number * (number - 1)

    def _evolve(self, hamiltonian, duration_ns: float, initial=None, e_ops=None):
        """Integrate the master equation and return the expectation values."""
        import qutip

        _destroy, excited, collapse = self._operators()
        state = initial if initial is not None else qutip.basis(self.levels, 0)
        times = np.array([0.0, duration_ns], dtype=float)
        result = qutip.mesolve(
            hamiltonian, state, times, collapse, e_ops=e_ops or [excited]
        )
        return result

    def _measure(self, values: np.ndarray, averages: int = 1) -> np.ndarray:
        """Add shot noise, as a real acquisition would have.

        *averages* is how many independent measurements were combined into each
        point, and the noise falls as 1/√N accordingly. Without that scaling the
        parameter would be decorative: averaging more would not help, and a test
        could not distinguish "the fit is wrong" from "you did not average
        enough to resolve this".
        """
        scale = self.shot_noise / np.sqrt(max(averages, 1))
        return values + self._rng.normal(0.0, scale, len(values))

    # --- acquisitions, in the units the routines expect ------------------------

    def rabi(self, amplitudes) -> np.ndarray:
        """Excited-state population after driving at each amplitude.

        The rotation angle is the drive amplitude times a fixed pulse duration,
        so the oscillation comes out of the evolution rather than being written
        down. `amp180` is where the angle reaches π.
        """
        import qutip

        _destroy, excited, collapse = self._operators()
        destroy = qutip.destroy(self.levels)
        duration = 20.0  # ns, a typical pi-pulse length

        populations = []
        for amplitude in np.asarray(amplitudes, dtype=float):
            # Rabi rate chosen so amp180 lands at 0.2 in the sweep's units.
            rabi_rate = np.pi * (amplitude / 0.2) / duration
            drive = (rabi_rate / 2) * (destroy + destroy.dag())
            hamiltonian = self._anharmonic_hamiltonian() + drive
            result = qutip.mesolve(
                hamiltonian,
                qutip.basis(self.levels, 0),
                np.array([0.0, duration]),
                collapse,
                e_ops=[excited],
            )
            populations.append(float(result.expect[0][-1]))
        return self._measure(np.array(populations))

    def t1(self, delays_s) -> np.ndarray:
        """Population after exciting and waiting. The decay emerges from the solver."""
        import qutip

        _destroy, excited, collapse = self._operators()
        populations = []
        for delay in np.asarray(delays_s, dtype=float) / NS:
            result = qutip.mesolve(
                self._anharmonic_hamiltonian(),
                qutip.basis(self.levels, 1),
                np.array([0.0, max(delay, 1e-9)]),
                collapse,
                e_ops=[excited],
            )
            populations.append(float(result.expect[0][-1]))
        return self._measure(np.array(populations))

    def _pulse(self, theta_deg: float, phi_deg: float):
        """A calibrated Rxy acting on the 0–1 subspace, identity above it.

        Built on the subspace rather than from ``a + a†`` because that ladder
        operator also couples 1↔2, with a √2 matrix element — so an
        ``exp(-iθ(a+a†)/2)`` "π/2 pulse" leaks into the second excited state and
        distorts every fringe measured through it. A real calibrated pulse is
        shaped to avoid exactly that; this is the idealised version of it.
        """
        import qutip

        theta, phi = np.deg2rad(theta_deg), np.deg2rad(phi_deg)
        matrix = np.eye(self.levels, dtype=complex)
        matrix[0, 0] = np.cos(theta / 2)
        matrix[1, 1] = np.cos(theta / 2)
        matrix[0, 1] = -1j * np.exp(-1j * phi) * np.sin(theta / 2)
        matrix[1, 0] = -1j * np.exp(1j * phi) * np.sin(theta / 2)
        return qutip.Qobj(matrix)

    def _free(self, state, duration_ns: float, hamiltonian, collapse):
        """Evolve *state* for *duration_ns* and return the density matrix."""
        import qutip

        result = qutip.mesolve(
            hamiltonian,
            state,
            np.array([0.0, max(duration_ns, 1e-9)]),
            collapse,
            e_ops=[],
            options=_SOLVER_OPTIONS,
        )
        return result.final_state

    def t2_echo(self, delays_s) -> np.ndarray:
        """Hahn echo: π/2, wait, π, wait, π/2. The refocusing pulse cancels
        static detuning, so what survives is T2."""
        import qutip

        _destroy, _excited, collapse = self._operators()
        half_pi = self._pulse(90, 0)
        pi_pulse = self._pulse(180, 0)
        hamiltonian = self._anharmonic_hamiltonian()

        populations = []
        for delay in np.asarray(delays_s, dtype=float) / NS:
            state = half_pi * qutip.basis(self.levels, 0)
            state = state * state.dag()
            half = delay / 2
            state = self._free(state, half, hamiltonian, collapse)
            state = pi_pulse * state * pi_pulse.dag()
            state = self._free(state, half, hamiltonian, collapse)
            final = half_pi * state * half_pi.dag()
            populations.append(float(np.real(final[1, 1])))
        return self._measure(np.array(populations))

    def ramsey(self, delays_s, artificial_detuning_hz: float) -> np.ndarray:
        """Ramsey fringe, generated the way the routine's schedule makes one.

        The routine does not detune the clock — it phase-advances the second
        π/2 by ``360·δ·t``. Reproducing that here is deliberate: it means the
        test exercises the routine's actual approach, so a schedule that
        advanced the phase the wrong way, or not at all, would fail rather than
        quietly agree with a differently-generated fringe.
        """
        import qutip

        _destroy, _excited, collapse = self._operators()
        first_pulse = self._pulse(90, 0)
        hamiltonian = self._anharmonic_hamiltonian()

        populations = []
        for delay_s in np.asarray(delays_s, dtype=float):
            delay_ns = delay_s / NS
            state = first_pulse * qutip.basis(self.levels, 0)
            state = state * state.dag()
            state = self._free(state, delay_ns, hamiltonian, collapse)
            phase = (360.0 * artificial_detuning_hz * delay_s) % 360.0
            second_pulse = self._pulse(90, phase)
            final = second_pulse * state * second_pulse.dag()
            populations.append(float(np.real(final[1, 1])))
        return self._measure(np.array(populations))

    def qubit_spectroscopy(self, frequencies_hz) -> np.ndarray:
        """Steady-state response of a weakly driven qubit, swept in frequency.

        The Lorentzian comes out of the steady state of the driven, damped
        system — it is not written down. Its centre is the scqubits f01.
        """
        import qutip

        _destroy, excited, collapse = self._operators()
        destroy = qutip.destroy(self.levels)
        drive_rate = 2 * np.pi * 0.0005  # weak, so the line is not power-broadened

        response = []
        for frequency in np.asarray(frequencies_hz, dtype=float) / GHZ:
            hamiltonian = self._anharmonic_hamiltonian(
                detuning_ghz=frequency - self.f01
            ) + (drive_rate / 2) * (destroy + destroy.dag())
            state = qutip.steadystate(hamiltonian, collapse)
            response.append(float(np.real((state * excited).tr())))
        return self._measure(np.array(response))

    def randomized_benchmarking(self, depths, circuits_per_depth, error_per_gate):
        """Survival probability after real Clifford sequences with a known error.

        Each sequence is composed from this package's own Clifford
        decomposition, evolved as unitaries, closed with its recovery gate, and
        depolarised by *error_per_gate* per Clifford. That makes this a test of
        the decomposition and the recovery gate as much as of the fit: if the
        recovery does not invert the sequence, the decay does not appear.
        """
        import random

        import qutip

        from qpi_driver.tuners.utils.clifford import (
            clifford_to_gates,
            generate_clifford_sequence,
            sequence_with_recovery,
        )

        rng = random.Random(self.seed)
        ground = qutip.basis(2, 0)
        survivals = []

        for depth in depths:
            shots = []
            for _ in range(circuits_per_depth):
                sequence = sequence_with_recovery(
                    generate_clifford_sequence(int(depth), rng)
                )
                state = ground * ground.dag()
                for clifford in sequence:
                    for theta, phi in clifford_to_gates(clifford):
                        gate = _rxy_qobj(theta, phi)
                        state = gate * state * gate.dag()
                    # Depolarise towards the maximally mixed state, which is
                    # what a gate error does on average over the Clifford group.
                    state = (1 - error_per_gate) * state + error_per_gate * (
                        qutip.qeye(2) / 2
                    )
                shots.append(float(np.real((state * ground * ground.dag()).tr())))
            survivals.append(float(np.mean(shots)))
        return self._measure(np.array(survivals), averages=circuits_per_depth)


def _rxy_qobj(theta_deg: float, phi_deg: float):
    """The rotation both schedulers' ``Rxy(theta, phi)`` performs, as a Qobj."""
    import qutip

    theta, phi = np.deg2rad(theta_deg), np.deg2rad(phi_deg)
    matrix = np.array(
        [
            [np.cos(theta / 2), -1j * np.exp(-1j * phi) * np.sin(theta / 2)],
            [-1j * np.exp(1j * phi) * np.sin(theta / 2), np.cos(theta / 2)],
        ],
        dtype=complex,
    )
    return qutip.Qobj(matrix)


class FakeElement:
    """A device element that reads and writes like the real ones do.

    Enough of a `QuantumDevice` for a routine's `build_schedule` to read the
    current frequency it should scan around, and for `apply` to write back — so
    tier 3 exercises the whole routine, not only its fit.

    ``name``, ``submodules`` and ``parameters`` are what
    `qpi_driver.tuners.utils.persistence` walks, so a device built from these
    also serialises: an end-to-end calibration writes a real device YAML rather
    than skipping the step that every later job depends on.
    """

    def __init__(self, name: str = "q0", **submodules):
        self.name = name
        self.submodules = {
            attribute: _Submodule(**values) for attribute, values in submodules.items()
        }
        for attribute, submodule in self.submodules.items():
            setattr(self, attribute, submodule)

    @property
    def parameters(self) -> dict[str, Any]:
        """None of its own; every parameter here lives on a submodule."""
        return {}


class _Submodule:
    def __init__(self, **parameters):
        for name, value in parameters.items():
            setattr(self, name, value)

    @property
    def parameters(self) -> dict[str, Any]:
        """Its parameters as plain values, which is what pydantic elements give too."""
        return dict(vars(self))


class FakeDevice:
    """A minimal stand-in for a QuantumDevice, holding one element per qubit."""

    def __init__(self, elements: dict[str, FakeElement]):
        self._elements = elements

    def elements(self):
        return list(self._elements)

    def edges(self):
        return []

    def get_element(self, name: str) -> FakeElement:
        return self._elements[name]


def device_for(simulator: TransmonSimulator, *qubits: str) -> FakeDevice:
    """A device whose current parameters are near, but not at, the true ones.

    Deliberately offset: a routine that scans around the current value has to
    actually find the true one, rather than being handed it.

    Every qubit is given the same starting parameters. They are the same
    simulated transmon — this is a model of one qubit, not of a chip — which is
    enough for what more than one target is here to test: that a routine runs
    once per target, and that a partial recalibration leaves the others alone.
    """
    return FakeDevice(
        {
            qubit: FakeElement(
                name=qubit,
                clock_freqs={
                    "f01": simulator.f01 * GHZ + 3e6,  # 3 MHz off
                    "f12": (simulator.f01 + simulator.anharmonicity) * GHZ,
                    "readout": simulator.readout_frequency_ghz * GHZ,
                },
                rxy={"amp180": 0.18, "motzoi": 0.0},
                measure={"pulse_amp": 0.25},
            )
            for qubit in (qubits or ("q0",))
        }
    )


# --- the scheduler seam ------------------------------------------------------


class _Operation:
    """One operation as recorded on a schedule.

    A scheduler's operation classes are opaque objects its compiler consumes.
    These stand in for them and keep their arguments legible, which is what lets
    :class:`SimulatedBackend` answer ``run`` from what a routine actually
    composed rather than from what it is assumed to have composed.
    """

    kind = "operation"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs

    def __repr__(self) -> str:
        return f"{self.kind}(*{self.args}, **{self.kwargs})"


def _operation(kind: str) -> type[_Operation]:
    return type(kind, (_Operation,), {"kind": kind})


class _Schedule:
    """A recorded schedule: its name, its repetitions, and its operations in order."""

    def __init__(self, name: str, repetitions: int = 1) -> None:
        self.name = name
        self.repetitions = repetitions
        self.operations: list[_Operation] = []

    def add(self, operation: Any, **kwargs: Any) -> None:
        self.operations.append(operation)

    def __repr__(self) -> str:
        return f"<_Schedule {self.name!r} with {len(self.operations)} operations>"


class RecordingBackend:
    """The scheduler vocabulary, recording rather than compiling.

    Deliberately not a :class:`~qpi_driver.tuners.base.SchedulerBackend`
    subclass: that ABC requires ``run``, and half the point of
    :class:`StubBackend` is that it has none. Routines only ever use the
    attributes, so duck typing is the honest relationship here.
    """

    name = "simulated"
    drag_parameter = "motzoi"

    Schedule = _Schedule
    Reset = _operation("Reset")
    Measure = _operation("Measure")
    Rxy = _operation("Rxy")
    X = _operation("X")
    Y = _operation("Y")
    Rz = _operation("Rz")
    CZ = _operation("CZ")
    IdlePulse = _operation("IdlePulse")
    SquarePulse = _operation("SquarePulse")
    SetClockFrequency = _operation("SetClockFrequency")

    class BinMode:
        AVERAGE = "average"
        APPEND = "append"

    def new_schedule(self, name: str, repetitions: int = 1) -> _Schedule:
        return _Schedule(name, repetitions)

    def idle(self, schedule: _Schedule, duration: float) -> None:
        schedule.add(self.IdlePulse(duration=duration))


class StubBackend(RecordingBackend):
    """Just enough backend for a routine to build a schedule.

    For a test that supplies the acquisition itself: the schedule is built —
    which is what sets the routine's setpoints — and then discarded. ``run``
    raises rather than returning something plausible, so a test that forgets to
    supply data fails loudly instead of fitting a fabrication.
    """

    def run(self, schedule: _Schedule) -> xr.Dataset:
        raise NotImplementedError(
            "StubBackend does not run schedules; supply the acquisition yourself, "
            "or use SimulatedBackend"
        )


class SimulatedBackend(RecordingBackend):
    """A backend that answers ``run`` from the simulator.

    What it is *not* is a schedule-level simulator. It reads a schedule for the
    sweep the routine encoded there — the frequencies of its
    ``SetClockFrequency``\\ s, the durations of its idles, the amplitudes on its
    ``Rxy``\\ s — and then asks the simulator for that experiment by name. So a
    routine that swept the wrong axis, or built the wrong number of
    acquisitions, fails here; a routine that composed the right sweep out of the
    wrong gates does not. Closing that last gap is RFC 0004 §11's outstanding
    item, and this is not it.

    What it does make possible is an end-to-end run: with ``run`` answered, a
    :class:`~qpi_driver.tuners.base.Tuner` can walk its whole DAG, fit, apply
    and persist without a scheduler or an instrument.

    ``rb`` is the exception to the description above, and reads the gates
    literally — see :meth:`_acquire_rb`.
    """

    def __init__(
        self, simulator: TransmonSimulator, *, gate_error: float = 0.001
    ) -> None:
        self.simulator = simulator
        #: Depolarising strength applied per gate in an RB sequence. Raising it
        #: is how a test drives the measured fidelity below a drift threshold.
        self.gate_error = gate_error

    def run(self, schedule: _Schedule) -> xr.Dataset:
        acquire = self._ACQUISITIONS.get(schedule.name)
        if acquire is None:
            raise NotImplementedError(
                f"the simulator has no physics for {schedule.name!r}. Disable it in "
                f"the calibration config, or add it here — do not let it return "
                f"data that means nothing. Simulated: "
                f"{', '.join(sorted(self._ACQUISITIONS))}"
            )
        values = np.asarray(acquire(self, schedule), dtype=float)
        return xr.Dataset({"y0": ("acq_index", values)})

    # --- reading the sweep back off a schedule --------------------------------

    @staticmethod
    def _of_kind(schedule: _Schedule, kind: str) -> list[_Operation]:
        return [op for op in schedule.operations if op.kind == kind]

    def _idle_durations(self, schedule: _Schedule) -> list[float]:
        return [
            float(op.kwargs["duration"]) for op in self._of_kind(schedule, "IdlePulse")
        ]

    def _acquire_qubit_spectroscopy(self, schedule: _Schedule) -> np.ndarray:
        frequencies = [
            float(op.kwargs["clock_freq_new"])
            for op in self._of_kind(schedule, "SetClockFrequency")
        ]
        return self.simulator.qubit_spectroscopy(frequencies)

    def _acquire_rabi(self, schedule: _Schedule) -> np.ndarray:
        amplitudes = [
            float(op.kwargs["amp180"])
            for op in self._of_kind(schedule, "Rxy")
            if "amp180" in op.kwargs
        ]
        return self.simulator.rabi(amplitudes)

    def _acquire_t1(self, schedule: _Schedule) -> np.ndarray:
        return self.simulator.t1(self._idle_durations(schedule))

    def _acquire_t2_echo(self, schedule: _Schedule) -> np.ndarray:
        # The routine splits each delay in two around the refocusing pulse, so
        # the idles come in pairs and the delay is their sum.
        halves = self._idle_durations(schedule)
        return self.simulator.t2_echo(
            [first + second for first, second in zip(halves[::2], halves[1::2])]
        )

    def _acquire_ramsey(self, schedule: _Schedule) -> np.ndarray:
        delays = self._idle_durations(schedule)
        second_pulses = self._of_kind(schedule, "Rxy")[1::2]
        phases = [float(op.kwargs["phi"]) for op in second_pulses]
        return self.simulator.ramsey(delays, _detuning_of(delays, phases))

    def _acquire_rb(self, schedule: _Schedule) -> np.ndarray:
        """Play the schedule's own Clifford gates, with a known error on each.

        This one reads the gates rather than the sweep, because an RB schedule
        carries its sequences literally: the survival probability is computed
        from the rotations the routine emitted, so its Clifford decomposition
        and its recovery gate are under test and not merely assumed.

        Survival is the ground-state population, which is what RB's decay model
        is written in — a readout used for RB is calibrated to report it.
        """
        import qutip

        ground = qutip.basis(2, 0) * qutip.basis(2, 0).dag()
        mixed = qutip.qeye(2) / 2

        state = ground
        survival: list[float] = []
        for op in schedule.operations:
            if op.kind == "Reset":
                state = ground
            elif op.kind in ("Rxy", "X", "Y"):
                gate = _rxy_qobj(*_rotation_of(op))
                state = gate * state * gate.dag()
                # A depolarising channel is what a gate error looks like once
                # averaged over the Clifford group, which is the assumption RB
                # itself rests on.
                state = (1 - self.gate_error) * state + self.gate_error * mixed
            elif op.kind == "Measure":
                survival.append(float(np.real((state * ground).tr())))
        return self.simulator._measure(
            np.array(survival), averages=schedule.repetitions
        )

    #: Routine name to acquisition. A routine's schedule is named after it, so
    #: this is a lookup rather than a guess. Absent means unsimulated, and
    #: :meth:`run` refuses rather than inventing something.
    _ACQUISITIONS = {
        "qubit_spectroscopy": _acquire_qubit_spectroscopy,
        "rabi": _acquire_rabi,
        "t1": _acquire_t1,
        "t2_echo": _acquire_t2_echo,
        "ramsey": _acquire_ramsey,
        "rb": _acquire_rb,
    }


def _rotation_of(operation: _Operation) -> tuple[float, float]:
    """The ``(theta, phi)`` an operation rotates by, in degrees."""
    if operation.kind == "X":
        return 180.0, 0.0
    if operation.kind == "Y":
        return 180.0, 90.0
    return float(operation.kwargs["theta"]), float(operation.kwargs["phi"])


def _detuning_of(delays: list[float], phases: list[float]) -> float:
    """Recover the artificial detuning a Ramsey schedule encoded as a phase advance.

    The routine does not detune the clock; it advances the second π/2 by
    ``360·δ·t`` modulo a turn. The shortest non-zero delay therefore recovers δ
    exactly, since at nanoseconds and megahertz that phase has not yet wrapped.

    Reading it back rather than being told it is the point: the simulator then
    generates the fringe the schedule asks for, and the routine's own fit has to
    reconcile that with the detuning it *configured*. A schedule that advanced
    the phase by the wrong amount shows up as a residual detuning the qubit does
    not have.
    """
    candidates = [
        (delay, phase)
        for delay, phase in zip(delays, phases)
        if delay > 0 and phase > 0
    ]
    if not candidates:
        raise ValueError(
            "no delay carries a phase advance; this schedule encodes no detuning"
        )
    delay, phase = min(candidates)
    return phase / (360.0 * delay)


class SimulatedTuner(Tuner):
    """The real :class:`Tuner`, over the simulator instead of a scheduler.

    ``Tuner`` supplies everything a calibration is — the DAG walk, the retry-free
    error accounting, the write-back, the report — over exactly two things a
    subclass provides. Providing those two from the simulator makes all of it
    testable end to end, which is the only way to find the faults that live
    between the pieces rather than inside them.
    """

    def __init__(
        self,
        simulator: TransmonSimulator | None = None,
        *,
        qubits: tuple[str, ...] = ("q0",),
        device_config_path: Path | str | None = None,
        gate_error: float = 0.001,
        name: str = "simulated",
    ) -> None:
        super().__init__(name=name)
        self.simulator = simulator or TransmonSimulator()
        self._backend = SimulatedBackend(self.simulator, gate_error=gate_error)
        self._device = device_for(self.simulator, *qubits)
        self._device_config_path = (
            Path(device_config_path) if device_config_path is not None else None
        )

    @property
    def backend(self) -> SimulatedBackend:
        return self._backend

    @property
    def device(self) -> FakeDevice:
        return self._device
