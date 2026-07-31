"""Two coupled transmons, for the CZ that one transmon cannot have (RFC 0004 §7).

:class:`~qpi_driver.simulation.transmon.TransmonSimulator` models one qubit. Two
of them side by side are still two independent density matrices, so entanglement
is not merely absent — it is unrepresentable, and a Bell state could not come out
wrong because it could not come out at all. This module holds the two in one
register (3 levels each, so a 9-dimensional Hilbert space and an 81-dimensional
Liouvillian) and couples them, which is the smallest change that makes a CZ a
thing the simulator can get wrong.

**The physics.** ``|11⟩`` and ``|02⟩`` are degenerate when the control sits one
anharmonicity below the target, and the exchange coupling turns that degeneracy
into an avoided crossing. A flux pulse walks the control through it; population
leaves ``|11⟩`` for ``|02⟩`` and comes back, and the round trip leaves a phase on
``|11⟩`` alone. That is the CZ. Nothing here writes down a chevron or a
conditional phase — both fall out of integrating the coupled Hamiltonian.

**Where this tier is weaker than the one-qubit one, and it matters.** The
one-qubit simulator derives everything from a Cooper-pair-box Hamiltonian whose
parameters are physical constants: no number in it was chosen to make a test
pass. Two of the numbers here *were* chosen — the exchange coupling :data:`G_MHZ`
and the flux-to-detuning curve :data:`FLUX_CURVATURE_GHZ` — because they describe
a coupler and a flux line this project has no device to measure. Given them the
dynamics are real, and a routine still has to find an avoided crossing it was not
told the location of. But the claim in RFC 0004 §7 that "nothing is generated
from a fitting model" is a claim about the one-qubit tier, and it is weaker here.
Read a two-qubit result as "the routine recovers the operating point of a
plausible coupler", not "of a real one".
"""

from dataclasses import dataclass, field

import numpy as np

from qpi_driver.simulation.transmon import NS, TransmonSimulator

#: Exchange coupling ``g/2π`` in MHz. Chosen, not measured: it sets the size of
#: the avoided crossing, and with it the duration of a CZ. The ``|11⟩``–``|02⟩``
#: matrix element is ``√2 g``, so a full round trip takes ``π/(√2·2π·g)`` — about
#: 110 ns here, which puts the CZ inside the duration window `cz_chevron` sweeps
#: by default rather than off its edge. A real fixed-coupling pair is a few MHz
#: to a few tens; this is at the low end of normal.
G_MHZ = 3.2

#: Flux-pulse amplitude to control-qubit detuning, in GHz per squared unit of
#: amplitude. Also chosen. Quadratic because a transmon sits at a flux sweet spot
#: where the first derivative of frequency with flux vanishes, so the leading
#: behaviour is second order. The scale puts the ``|11⟩``–``|02⟩`` resonance near
#: the middle of the amplitude window `cz_chevron` sweeps — but deliberately
#: *between* two of its default setpoints (0.376, where the grid has 0.35 and
#: 0.40). A resonance sitting exactly on a grid point would let a routine find it
#: by luck, and the coarseness of that default grid is a real finding (§7).
FLUX_CURVATURE_GHZ = 2.0

#: The transition a parametric coupler drive bridges, in GHz.
#:
#: This is the *other* CZ, and the one `FluxTunableCoupler` builds. Rather than
#: pushing a qubit onto the ``|11⟩``–``|02⟩`` crossing with DC flux, the coupler
#: between the two is modulated at microwave frequency and a sideband of that
#: modulation bridges the gap. The DC bias only parks the coupler; the gate is
#: the tone on top.
#:
#: Two consequences make it a different gate to model rather than a rearrangement
#: of the same one: it works with qubits nowhere near each other, because the
#: modulation supplies the detuning instead of the qubit placement having to; and
#: the resonance condition is on the drive *frequency*, so amplitude sets how fast
#: the exchange runs while frequency sets whether it runs at all.
#:
#: The value matches the ``clock_freqs.cz`` every edge in the lab's device config
#: carries. Which transition of the coupler-qubit spectrum it actually is depends
#: on the coupler's own frequency, which this simulator does not model — so this
#: is a chosen constant, and a routine finding a resonance here has found the one
#: it was configured to look for.
SIDEBAND_GAP_GHZ = 3.9

#: Exchange rate in MHz per unit of coupler drive amplitude — linear, as the
#: leading order of a parametric drive is.
#:
#: Not invented: the lab's four calibrated edges imply 4.29, 1.91, 4.91 and 6.41
#: MHz per unit from their own (amplitude, duration) pairs, reading each duration
#: as one full ``|11⟩ → |02⟩ → |11⟩`` round trip. This is their mean, so a device
#: config taken off the bench lands near a real CZ here rather than nowhere. The
#: spread across those four is itself the point that couplers differ.
PARAMETRIC_RATE_MHZ = 4.4

#: AC Stark shift per qubit, in MHz per unit of drive amplitude squared. A
#: modulated coupler pushes both qubits around while it drives the exchange, and
#: the phase that leaves is single-qubit rather than conditional — so no amount
#: of tuning amplitude or duration removes it. It is exactly what the edge's
#: ``<qubit>_phase_correction`` parameters exist to cancel, and with them at zero
#: a perfectly good CZ still produces the wrong Bell state.
STARK_SHIFT_MHZ = 9.0

#: How much harder the drive pushes the child than the parent. Deliberately not
#: 1: with a symmetric shift one correction would fix both qubits, and a test
#: could not tell a correction written to the wrong qubit from a right one.
STARK_ASYMMETRY = 1.37


@dataclass
class CoupledTransmons:
    """Two transmons in one register, exchange-coupled, one of them flux-tunable.

    Attributes:
        control: the flux-tunable qubit — the one a CZ's flux pulse detunes.
        target: the fixed-frequency qubit it is tuned towards.
        g_mhz: exchange coupling. See :data:`G_MHZ`.
        flux_curvature_ghz: flux amplitude to detuning. See
            :data:`FLUX_CURVATURE_GHZ`.
        conditional_phase_offset_deg: a static phase error added to the CZ's
            conditional phase, so a test can present a gate that needs
            correcting rather than one that is already perfect.
    """

    control: TransmonSimulator = field(default_factory=TransmonSimulator)
    target: TransmonSimulator = field(default_factory=TransmonSimulator)
    g_mhz: float = G_MHZ
    flux_curvature_ghz: float = FLUX_CURVATURE_GHZ
    sideband_gap_ghz: float = SIDEBAND_GAP_GHZ
    conditional_phase_offset_deg: float = 0.0
    shot_noise: float = 0.004
    seed: int = 20260731

    _rng: np.random.Generator = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._rng = np.random.default_rng(self.seed)
        if self.control.levels < 3 or self.target.levels < 3:
            raise ValueError(
                "a CZ exchanges |11> with |02>, so both transmons need a second "
                "excited state — levels must be at least 3"
            )

    # --- the register ---------------------------------------------------------

    @property
    def levels(self) -> tuple[int, int]:
        return self.control.levels, self.target.levels

    def _ladders(self):
        """Joint annihilation operators for the two qubits."""
        import qutip

        control_levels, target_levels = self.levels
        return (
            qutip.tensor(qutip.destroy(control_levels), qutip.qeye(target_levels)),
            qutip.tensor(qutip.qeye(control_levels), qutip.destroy(target_levels)),
        )

    def state(self, control_level: int, target_level: int):
        """A joint basis ket, e.g. ``state(1, 1)`` for ``|11⟩``."""
        import qutip

        control_levels, target_levels = self.levels
        return qutip.tensor(
            qutip.basis(control_levels, control_level),
            qutip.basis(target_levels, target_level),
        )

    def detuning_of(self, flux_amplitude: float) -> float:
        """Control-qubit detuning in GHz for a flux pulse of *flux_amplitude*.

        Negative: flux tunes a transmon down from its sweet spot, never up.
        """
        return -self.flux_curvature_ghz * float(flux_amplitude) ** 2

    @property
    def resonant_detuning(self) -> float:
        """The detuning in GHz at which ``|11⟩`` and ``|02⟩`` are degenerate.

        ``E|11⟩ = ω_c + ω_t`` and ``E|02⟩ = 2ω_t + α_t``, so they meet when the
        control sits exactly one target-anharmonicity below the target.
        """
        return float(self.target.anharmonicity)

    @property
    def resonant_amplitude(self) -> float:
        """The flux amplitude that reaches :attr:`resonant_detuning`."""
        return float(np.sqrt(-self.resonant_detuning / self.flux_curvature_ghz))

    @property
    def exchange_rate_ghz(self) -> float:
        """The ``|11⟩``–``|02⟩`` matrix element in GHz — ``√2 g``."""
        return np.sqrt(2.0) * self.g_mhz * 1e-3

    @property
    def cz_duration_ns(self) -> float:
        """A full ``|11⟩ → |02⟩ → |11⟩`` round trip, which is the CZ."""
        return 1.0 / (2.0 * self.exchange_rate_ghz)

    def _hamiltonian(self, flux_amplitude: float = 0.0):
        """The coupled Hamiltonian in the target's rotating frame, in rad/ns.

        The control's frequency is offset by the flux pulse; each transmon keeps
        its own anharmonicity; and the exchange term is what makes ``|11⟩`` and
        ``|02⟩`` an avoided crossing rather than a degeneracy.
        """
        control_ladder, target_ladder = self._ladders()
        control_number = control_ladder.dag() * control_ladder
        target_number = target_ladder.dag() * target_ladder

        detuning = 2 * np.pi * self.detuning_of(flux_amplitude)
        control_alpha = 2 * np.pi * self.control.anharmonicity
        target_alpha = 2 * np.pi * self.target.anharmonicity
        coupling = 2 * np.pi * self.g_mhz * 1e-3

        return (
            detuning * control_number
            + (control_alpha / 2) * control_number * (control_number - 1)
            + (target_alpha / 2) * target_number * (target_number - 1)
            + coupling
            * (
                control_ladder.dag() * target_ladder
                + control_ladder * target_ladder.dag()
            )
        )

    # --- the parametric coupler ----------------------------------------------

    def parametric_rate(self, amplitude: float) -> float:
        """``|11⟩``–``|02⟩`` exchange rate in GHz at a coupler drive *amplitude*."""
        return PARAMETRIC_RATE_MHZ * 1e-3 * abs(float(amplitude))

    def parametric_cz_duration_ns(self, amplitude: float) -> float:
        """One full round trip at *amplitude* — the CZ, when on resonance."""
        rate = self.parametric_rate(amplitude)
        return float("inf") if rate <= 0 else 1.0 / (2.0 * rate)

    def parametric_operating_point(self, amplitude: float) -> tuple[float, float]:
        """The ``(drive_ghz, duration_ns)`` that make a CZ at *amplitude*.

        The drive frequency is not simply :data:`SIDEBAND_GAP_GHZ`. The coupler
        Stark-shifts both qubits while it drives them, and it shifts ``|02⟩`` —
        two excitations in the child — by more than ``|11⟩``, so the crossing
        moves as the amplitude rises. Retuning per amplitude is what a real
        calibration does about that, and the chevron is how it finds it.
        """
        stark = STARK_SHIFT_MHZ * 1e-3 * float(amplitude) ** 2
        return (
            self.sideband_gap_ghz - stark * (1.0 - STARK_ASYMMETRY),
            self.parametric_cz_duration_ns(amplitude),
        )

    def _parametric_hamiltonian(self, amplitude: float, drive_ghz: float):
        """The coupler-driven Hamiltonian, in the frame rotating at the drive.

        Written on the exchange itself rather than on the coupler's ladder. In
        the rotating frame the modulation leaves a static coupling between
        ``|11⟩`` and ``|02⟩`` and a detuning equal to how far the drive sits from
        their gap, which is the whole content of the gate. Carrying the coupler
        as a third oscillator would add a 27-dimensional register and a
        time-dependent solve to produce the same two numbers.
        """
        detuning = 2 * np.pi * (self.sideband_gap_ghz - drive_ghz)
        rate = 2 * np.pi * self.parametric_rate(amplitude)
        stark = 2 * np.pi * STARK_SHIFT_MHZ * 1e-3 * float(amplitude) ** 2

        excited_11 = self.state(1, 1)
        excited_02 = self.state(0, 2)
        control_ladder, target_ladder = self._ladders()

        return (
            detuning * (excited_02 * excited_02.dag())
            + rate * (excited_11 * excited_02.dag() + excited_02 * excited_11.dag())
            + stark * (control_ladder.dag() * control_ladder)
            + STARK_ASYMMETRY * stark * (target_ladder.dag() * target_ladder)
        )

    def parametric_evolve(self, state, amplitude: float, drive_ghz: float, duration_ns):
        """Integrate the coupler-driven master equation over *duration_ns*."""
        import qutip

        if state.isket:
            state = state * state.dag()
        return qutip.mesolve(
            self._parametric_hamiltonian(amplitude, drive_ghz),
            state,
            np.array([0.0, max(duration_ns, 1e-9)]),
            self._collapse(),
            e_ops=[],
            options={"nsteps": 200_000},
        ).final_state

    def parametric_phases(
        self, amplitude: float, drive_ghz: float, duration_ns: float
    ) -> dict[str, float]:
        """The phases a coupler CZ leaves behind, in degrees.

        ``conditional`` is the part not explained by the two single-qubit
        phases; ``parent`` and ``child`` are those, and are exactly what the
        edge's ``<qubit>_phase_correction`` parameters have to undo.
        """
        import qutip

        propagator = qutip.propagator(
            self._parametric_hamiltonian(amplitude, drive_ghz),
            duration_ns,
            [],
            options={"nsteps": 200_000},
        )
        if isinstance(propagator, list):
            propagator = propagator[-1]

        def phase_of(parent_level: int, child_level: int) -> float:
            ket = self.state(parent_level, child_level)
            element = ket.dag() * propagator * ket
            if hasattr(element, "full"):
                element = element.full()[0, 0]
            return float(np.angle(complex(element)))

        parent, child = phase_of(1, 0), phase_of(0, 1)
        conditional = phase_of(1, 1) - parent - child
        return {
            "parent": float(np.rad2deg(parent)),
            "child": float(np.rad2deg(child)),
            "conditional": float((np.rad2deg(conditional) + 180.0) % 360.0 - 180.0),
        }

    def _collapse(self):
        """Relaxation and dephasing on both qubits, in the joint space."""
        control_ladder, target_ladder = self._ladders()
        operators = []
        for transmon, ladder in (
            (self.control, control_ladder),
            (self.target, target_ladder),
        ):
            if transmon.t1_ns > 0:
                operators.append(np.sqrt(1.0 / transmon.t1_ns) * ladder)
            inverse_tphi = 1.0 / transmon.t2_ns - 1.0 / (2.0 * transmon.t1_ns)
            if inverse_tphi > 0:
                operators.append(np.sqrt(2.0 * inverse_tphi) * ladder.dag() * ladder)
        return operators

    def _pulse(self, theta_deg: float, phi_deg: float, *, on_control: bool):
        """A calibrated single-qubit Rxy on one half of the register.

        On the 0–1 subspace only, and identity elsewhere, for the reason
        :meth:`TransmonSimulator._pulse` gives: ``a + a†`` would also drive 1↔2
        and leak the very population a CZ is trying to place there deliberately.
        """
        import qutip

        control_levels, target_levels = self.levels
        levels = control_levels if on_control else target_levels
        theta, phi = np.deg2rad(theta_deg), np.deg2rad(phi_deg)
        matrix = np.eye(levels, dtype=complex)
        matrix[0, 0] = np.cos(theta / 2)
        matrix[1, 1] = np.cos(theta / 2)
        matrix[0, 1] = -1j * np.exp(-1j * phi) * np.sin(theta / 2)
        matrix[1, 0] = -1j * np.exp(1j * phi) * np.sin(theta / 2)
        single = qutip.Qobj(matrix)

        if on_control:
            return qutip.tensor(single, qutip.qeye(target_levels))
        return qutip.tensor(qutip.qeye(control_levels), single)

    def _evolve(self, state, flux_amplitude: float, duration_ns: float):
        """Integrate the coupled master equation and return the density matrix."""
        import qutip

        if state.isket:
            state = state * state.dag()
        result = qutip.mesolve(
            self._hamiltonian(flux_amplitude),
            state,
            np.array([0.0, max(duration_ns, 1e-9)]),
            self._collapse(),
            e_ops=[],
            options={"nsteps": 200_000},
        )
        return result.final_state

    # --- populations ----------------------------------------------------------

    def _population(self, density, *, qubit_level: int, on_control: bool) -> float:
        """Probability that one qubit is in *qubit_level*, tracing out the other."""
        import qutip

        control_levels, target_levels = self.levels
        if on_control:
            projector = qutip.tensor(
                qutip.basis(control_levels, qubit_level)
                * qutip.basis(control_levels, qubit_level).dag(),
                qutip.qeye(target_levels),
            )
        else:
            projector = qutip.tensor(
                qutip.qeye(control_levels),
                qutip.basis(target_levels, qubit_level)
                * qutip.basis(target_levels, qubit_level).dag(),
            )
        return float(np.real((density * projector).tr()))

    def _measure(self, values: np.ndarray, averages: int = 1) -> np.ndarray:
        scale = self.shot_noise / np.sqrt(max(averages, 1))
        return values + self._rng.normal(0.0, scale, len(values))

    # --- acquisitions, in the units the routines expect ------------------------

    def chevron(self, amplitudes, durations, averages: int = 1) -> np.ndarray:
        """Population left on the control after a flux pulse, over the grid.

        ``|11⟩`` is prepared, the flux pulse runs at each (amplitude, duration),
        and the control is measured — which is what `cz_chevron`'s schedule does.
        Off resonance nothing moves and the control stays excited; on resonance
        population swings to ``|02⟩`` and back.

        Flattened row-major over ``(amplitude, duration)``, matching the order
        the routine emits its acquisitions in.
        """
        excited = self.state(1, 1)
        populations = [
            self._population(
                self._evolve(excited, float(amplitude), float(duration) / NS),
                qubit_level=1,
                on_control=True,
            )
            for amplitude in np.asarray(amplitudes, dtype=float)
            for duration in np.asarray(durations, dtype=float)
        ]
        return self._measure(np.array(populations), averages=averages)

    def cz_conditional_phase(
        self, flux_amplitude: float | None = None, duration_ns: float | None = None
    ) -> float:
        """The phase the CZ actually leaves on ``|11⟩``, in degrees.

        Computed from the propagator rather than assumed: the conditional phase
        is the part of ``|11⟩``'s phase that is not explained by the two
        single-qubit phases, which is exactly ``φ11 − φ10 − φ01``.
        """
        amplitude = (
            self.resonant_amplitude if flux_amplitude is None else flux_amplitude
        )
        duration = self.cz_duration_ns if duration_ns is None else duration_ns

        import qutip

        propagator = qutip.propagator(
            self._hamiltonian(amplitude), duration, [], options={"nsteps": 200_000}
        )
        if isinstance(propagator, list):
            propagator = propagator[-1]

        def phase_of(control_level: int, target_level: int) -> float:
            ket = self.state(control_level, target_level)
            amplitude = ket.dag() * propagator * ket
            if hasattr(amplitude, "full"):
                amplitude = amplitude.full()[0, 0]
            return float(np.angle(complex(amplitude)))

        conditional = phase_of(1, 1) - phase_of(1, 0) - phase_of(0, 1)
        degrees = np.rad2deg(conditional) + self.conditional_phase_offset_deg
        return float((degrees + 180.0) % 360.0 - 180.0)

    def conditional_phase(
        self,
        phases_deg,
        *,
        flux_amplitude: float | None = None,
        duration_ns: float | None = None,
        averages: int = 1,
    ) -> np.ndarray:
        """A Ramsey on the control across a CZ, with the target down then up.

        Reproduces `conditional_phase`'s schedule: π/2 on the control, the CZ,
        then a second π/2 whose phase is swept — run once with the target in
        ``|0⟩`` and once in ``|1⟩``. The offset between the two fringes is the
        conditional phase, and the routine's job is to recover it.

        Returns the two fringes concatenated, ground-target first, which is the
        order the routine reads them back in.
        """
        amplitude = (
            self.resonant_amplitude if flux_amplitude is None else flux_amplitude
        )
        duration = self.cz_duration_ns if duration_ns is None else duration_ns
        offset = np.deg2rad(self.conditional_phase_offset_deg)

        first = self._pulse(90, 0, on_control=True)
        excite_target = self._pulse(180, 0, on_control=False)

        populations: list[float] = []
        for target_excited in (False, True):
            for phase in np.asarray(phases_deg, dtype=float):
                state = self.state(0, 0)
                if target_excited:
                    state = excite_target * state
                state = first * state
                density = self._evolve(state, amplitude, duration)
                # The offset stands in for a miscalibrated flux pulse: a phase
                # on |11> that the physics above did not produce, so a test can
                # hand the routine a gate that genuinely needs correcting.
                if target_excited and offset:
                    phase_gate = self._conditional_phase_gate(offset)
                    density = phase_gate * density * phase_gate.dag()
                second = self._pulse(90, float(phase), on_control=True)
                density = second * density * second.dag()
                populations.append(
                    self._population(density, qubit_level=1, on_control=True)
                )
        return self._measure(np.array(populations), averages=averages)

    def _conditional_phase_gate(self, phase_rad: float):
        """A diagonal gate putting *phase_rad* on ``|11⟩`` and nothing else."""
        import qutip

        control_levels, target_levels = self.levels
        diagonal = np.ones(control_levels * target_levels, dtype=complex)
        index = 1 * target_levels + 1
        diagonal[index] = np.exp(1j * phase_rad)
        return qutip.Qobj(
            np.diag(diagonal),
            dims=[[control_levels, target_levels], [control_levels, target_levels]],
        )

    def bell_state(
        self, flux_amplitude: float | None = None, duration_ns: float | None = None
    ):
        """``(|00⟩ + |11⟩)/√2`` built the way a circuit builds it: H, CZ, H.

        Returned as a density matrix over the joint register, so a caller can ask
        it for a concurrence or a population and get an answer that reflects the
        coupler rather than an assumption.
        """
        amplitude = (
            self.resonant_amplitude if flux_amplitude is None else flux_amplitude
        )
        duration = self.cz_duration_ns if duration_ns is None else duration_ns

        state = self.state(0, 0)
        state = self._pulse(90, 90, on_control=True) * state
        state = self._pulse(90, 90, on_control=False) * state
        density = self._evolve(state, amplitude, duration)
        recover = self._pulse(90, -90, on_control=False)
        return recover * density * recover.dag()


def computational_block(density) -> np.ndarray:
    """The 4×4 computational-subspace block of a joint density matrix.

    Two three-level transmons span nine states; the two-qubit language a Bell
    state is written in spans four. Anything outside the block is leakage, and
    :func:`leakage` is how much.
    """
    control_levels, target_levels = density.dims[0]
    matrix = density.full()
    indices = [
        control * target_levels + target for control in (0, 1) for target in (0, 1)
    ]
    return matrix[np.ix_(indices, indices)]


def leakage(density) -> float:
    """Population that left the computational subspace — ``|2⟩`` on either qubit."""
    return float(1.0 - np.real(np.trace(computational_block(density))))


def concurrence(density) -> float:
    """Entanglement of the computational block, 0 (separable) to 1 (Bell).

    Renormalised over the computational subspace first, so a little leakage
    lowers the answer through the state's mixedness rather than by silently
    discarding trace.
    """
    import qutip

    block = computational_block(density)
    trace = float(np.real(np.trace(block)))
    if trace <= 0:
        return 0.0
    return float(qutip.concurrence(qutip.Qobj(block / trace, dims=[[2, 2], [2, 2]])))
