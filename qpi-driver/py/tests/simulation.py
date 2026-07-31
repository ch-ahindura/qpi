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
"""

from dataclasses import dataclass, field

import numpy as np

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
    """

    def __init__(self, **submodules):
        for name, values in submodules.items():
            setattr(self, name, _Submodule(**values))


class _Submodule:
    def __init__(self, **parameters):
        for name, value in parameters.items():
            setattr(self, name, value)


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


def device_for(simulator: TransmonSimulator, qubit: str = "q0") -> FakeDevice:
    """A device whose current parameters are near, but not at, the true ones.

    Deliberately offset: a routine that scans around the current value has to
    actually find the true one, rather than being handed it.
    """
    return FakeDevice(
        {
            qubit: FakeElement(
                clock_freqs={
                    "f01": simulator.f01 * GHZ + 3e6,  # 3 MHz off
                    "f12": (simulator.f01 + simulator.anharmonicity) * GHZ,
                    "readout": simulator.readout_frequency_ghz * GHZ,
                },
                rxy={"amp180": 0.18, "motzoi": 0.0},
                measure={"pulse_amp": 0.25},
            )
        }
    )
