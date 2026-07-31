"""The readout resonator: a line to find, and a power at which it moves.

Without this the simulator's readout was two fixed points in the IQ plane. That
is enough to tell ``|0>`` from ``|1>``, and not enough for the two routines whose
whole job is the readout chain itself — `resonator_spectroscopy` sweeps the
readout clock looking for a response, and `resonator_punchout` sweeps its power
watching that response move. Against fixed blobs both swept a flat line.

Two effects, and they are the two those routines measure:

**A lineshape.** The resonator responds over a linewidth ``kappa`` about its
resonance and falls away outside it, so there is a peak to fit — and driving away
from it costs contrast, which is why a wrong ``clock_freqs.readout`` degrades
every measurement downstream rather than only the one that found it.

**Punchout.** The qubit pulls the resonance by the dispersive shift ``chi``. Push
enough photons in and that pull collapses and the resonance walks to its bare
value, which is what makes readout power a thing to calibrate rather than turn
up. The crossover is set by :attr:`ReadoutResonator.punchout_amplitude`.

**Dispersive, and it has to be.** Each qubit level pulls the resonance to its own
frequency, so at any one drive frequency the levels return *different complex
numbers* — differing in phase as much as in magnitude. That is where the two clouds
in an IQ plane come from, and it is why they are no longer two constants the
coordinator carries: a model with hand-placed clouds cannot be asked where the best
readout point is, or what rotation and threshold separate them, because both answers
were built into the placement.

The consequence is that `measure.acq_rotation` and `measure.acq_threshold` are now
*wrong* at their defaults of zero, exactly as they are on a chip nobody has
calibrated. `readout_discrimination` is what measures them.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ReadoutResonator:
    """One qubit's readout resonator.

    Attributes:
        frequency_ghz: the resonance with the qubit in its ground state and
            vanishing readout power — the dressed, low-power value a device
            config's ``clock_freqs.readout`` is an estimate of.
        linewidth_ghz: FWHM ``kappa``. Also what the fitted linewidth should
            come out as, since the response below is Lorentzian in power.
        dispersive_shift_ghz: ``chi``, how far the qubit pulls the resonance.
            Punchout walks the resonance this far and then stops.
        punchout_amplitude: the readout amplitude at which half the dispersive
            pull has collapsed. Above this the resonator is on its way to bare
            and the readout is losing the thing it measures.
    """

    frequency_ghz: float
    linewidth_ghz: float = 0.002
    dispersive_shift_ghz: float = 0.003
    punchout_amplitude: float = 0.7

    def punched_through(self, amplitude: float) -> float:
        """How far towards the bare resonator a drive of *amplitude* has pushed, 0 to 1.

        In the photon number rather than the amplitude, which is why this is
        ``A²/(A² + A_c²)`` and not a ratio of amplitudes.
        """
        photons = float(amplitude) ** 2
        critical = self.punchout_amplitude**2
        if critical <= 0:
            return 1.0
        return photons / (photons + critical)

    @property
    def bare_frequency_ghz(self) -> float:
        """Where the resonance sits with the qubit's pull removed.

        :attr:`frequency_ghz` is the *ground-state* resonance, because that is what a
        device config's ``clock_freqs.readout`` means and what
        `resonator_spectroscopy` finds. The bare frequency is one dispersive shift
        below it, and is where every level's resonance converges once punchout has
        washed the pull out.
        """
        return self.frequency_ghz - self.dispersive_shift_ghz

    def resonance_ghz(self, amplitude: float, level: int = 0) -> float:
        """Where the resonance sits for a qubit in *level*, read out at *amplitude*.

        The dispersive pull is ``chi * (1 - 2n)`` about the bare frequency, so the
        ladder is ``+chi``, ``-chi``, ``-3chi`` for the ground, first and second
        excited states. Linear in the excitation number, which is the leading order
        and — more to the point here — *monotone*: a three-state discriminator needs
        the levels to come out in an order, and a model where ``|2>`` landed between
        ``|0>`` and ``|1>`` would make one unmeasurable.

        Power collapses the pull towards bare, which is punchout. At full collapse
        every level sits at the same place and readout distinguishes nothing, which is
        why turning the power up is not free.
        """
        pull = self.dispersive_shift_ghz * (1.0 - 2.0 * int(level))
        return self.bare_frequency_ghz + pull * (1.0 - self.punched_through(amplitude))

    def reflection(self, drive_ghz: float, amplitude: float, level: int = 0) -> complex:
        """The complex response at *drive_ghz* for a qubit in *level*.

        ``(kappa/2) / (kappa/2 + i*detuning)`` — one at resonance, falling away with a
        phase that swings through ±90° across the line. **The phase is the point.**
        Two levels sit at two resonances, so at any one drive frequency they return
        two different complex numbers, and it is that difference a discriminator
        separates. A magnitude-only model has the two states differing only in how
        much comes back, which is true at the resonance and false everywhere else, and
        it cannot pose the question of where the best readout point is.
        """
        half = self.linewidth_ghz / 2.0
        if half <= 0:
            return 1.0 + 0j
        detuning = float(drive_ghz) - self.resonance_ghz(amplitude, level)
        return half / (half + 1j * detuning)

    def response(self, drive_ghz: float, amplitude: float, level: int = 0) -> float:
        """Fraction of the *power* that comes back, 0 to 1.

        ``|reflection|^2``, so a Lorentzian of FWHM ``kappa`` and a fit of it recovers
        ``kappa`` itself rather than some multiple. This is what
        `resonator_spectroscopy` sweeps.
        """
        return float(abs(self.reflection(drive_ghz, amplitude, level)) ** 2)
