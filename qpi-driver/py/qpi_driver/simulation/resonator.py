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

What this is not: a dispersive model with the two qubit states pulling the
resonance to two different frequencies. Here the lineshape follows the
ground-state resonance and the discrimination stays in the blob geometry the
coordinator already had — see :data:`~qpi_driver.simulation.coordinator.GROUND_IQ`.
The consequence is that the best point to read out at is the resonance itself,
where a real chip has an optimum *between* the two pulled peaks. The routines
under test measure the lineshape and its power dependence, and those are real.
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

    def resonance_ghz(self, amplitude: float) -> float:
        """Where the resonance sits when read out at *amplitude*.

        Downwards with power, by up to one dispersive shift. The direction is a
        choice — which side the qubit pulls the resonator to depends on the sign
        of the detuning between them — but it has to be *a* direction, and a
        consistent one, or punchout has nothing monotone to find.
        """
        return self.frequency_ghz - self.dispersive_shift_ghz * self.punched_through(
            amplitude
        )

    def response(self, drive_ghz: float, amplitude: float) -> float:
        """Fraction of the signal that comes back, 0 to 1, at *drive_ghz*.

        A Lorentzian of FWHM ``kappa`` centred on :meth:`resonance_ghz` — the
        transmitted power, so a fit of this recovers ``kappa`` itself rather than
        some multiple of it.
        """
        half = self.linewidth_ghz / 2.0
        if half <= 0:
            return 1.0
        detuning = float(drive_ghz) - self.resonance_ghz(amplitude)
        return float(half**2 / (detuning**2 + half**2))
