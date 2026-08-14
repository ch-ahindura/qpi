"""A transmon element carrying the parameters a calibration produces (RFC 0005 §13).

`BasicTransmonElement` has `clock_freqs`, `measure`, `ports`, `pulse_compensation`,
`reset` and `rxy` — and no home for several things the calibration graph measures. A
routine with nowhere to write its result is a routine that cannot exist, so those
parameters went unmeasured and were typed in by hand instead.

The first of them is the spectroscopy drive amplitude. `qubit_spectroscopy` drives a
weak pulse and fits a Lorentzian, and how weak is a property of the chip: too weak and
the line is lost in readout noise, too strong and it power-broadens or reaches the
two-photon 0–2 transition. RFC 0004 §11 records that the routine's 1% default is a
signal-to-noise of about five, that the same sweep returned centres 14 MHz low, 12 MHz
high and 62 MHz low on nothing but noise, and that a full-DAG test was passing on that
margin. It is a calibrated parameter, not a constant.

**Opt-in, per element.** A device config selects this by naming it in
`element_type.path`, exactly as an edge selects `FluxTunableCoupler`. Configs that keep
`BasicTransmonElement` go on working and simply do not get the routines that need these
fields — which is why every such routine resolves the parameter through the device
rather than assuming it, the way `drag_parameter_name` and `phase_correction_names`
already do.
"""

from typing import Any

from qpi_driver.compat.quantify import (
    BasicTransmonElement,
    InstrumentChannel,
    ManualParameter,
    Numbers,
)
from qpi_driver.executors.base.rotations import amplitude_for_angle

#: Drive amplitude a spectroscopy sweep may be asked for. The same full-scale bound the
#: schedulers put on any pulse amplitude: past one the waveform clips and the schedule
#: will not compile.
MAX_SPECTROSCOPY_AMPLITUDE = 1.0

#: The gate whose amplitude interpolation this element replaces.
RXY_OPERATION = "Rxy"


class SpectroscopySettings(InstrumentChannel):
    """How hard to drive a qubit while looking for its transitions.

    Two amplitudes, because the two transitions are not equally easy to see. The 0–1
    line is driven from the ground state and needs only enough power to move the
    population out of it; the 1–2 line is driven from ``|1>``, which relaxes while the
    pulse plays, so it generally wants more.

    Zero means "not calibrated", and a routine reading it falls back to its own default
    rather than driving with no amplitude at all.
    """

    def __init__(self, parent, name):
        super().__init__(parent, name)

        for amplitude in ("amplitude", "amplitude_12"):
            self.add_parameter(
                amplitude,
                parameter_class=ManualParameter,
                unit="",
                initial_value=0.0,
                vals=Numbers(
                    min_value=0.0, max_value=MAX_SPECTROSCOPY_AMPLITUDE, allow_nan=True
                ),
            )


class ResonatorSettings(InstrumentChannel):
    """What `resonator_spectroscopy` measured about the resonator itself.

    The linewidth is not a *calibration* — nothing is tuned to it — but three nodes need
    it to size their own sweeps, and until this existed they used a 2 MHz constant. On a
    chip whose resonator is 370 kHz wide that constant put `readout_operating_point`'s
    outer setpoints 2.7 linewidths off resonance, and it chose one of them; readout had to
    be hand-tuned to recover (RFC 0007 §1). The number was measured two nodes earlier and
    thrown away, which is what this fixes — RFC 0005 §13 asked for it.

    Zero means "not measured", and a routine reading it falls back to its own default
    rather than sizing a sweep from nothing.
    """

    def __init__(self, parent, name):
        super().__init__(parent, name)

        self.add_parameter(
            "linewidth",
            parameter_class=ManualParameter,
            unit="Hz",
            initial_value=0.0,
            vals=Numbers(min_value=0.0, max_value=1e9, allow_nan=True),
        )


class CoherenceTimes(InstrumentChannel):
    """What `t1` measured about relaxation, for the one guard that needs a ceiling.

    Not a calibration — nothing is tuned to T1 — but `t2_echo` cannot tell a fitted
    coherence time from an unconstrained one without it. A Hahn echo refocuses static
    dephasing and nothing else, so ``T2 <= 2*T1`` is a hard bound rather than a typical
    value; with T1 out of reach a T2 three times over it reads as a long-lived qubit, and
    gets written as one. The August 2026 B chip fitted 201 us of T2 against a 32.8 us T1
    over a 100 us window, and cleared every other guard in `fit_t2` doing it.

    The same case RFC 0005 §13 makes for the resonator linewidth, one node along: a number
    measured here and thrown away, which another node then has to do without.

    Zero means "not measured", and `fit_t2` skips the ceiling rather than comparing
    against nothing.
    """

    def __init__(self, parent, name):
        super().__init__(parent, name)

        self.add_parameter(
            "t1",
            parameter_class=ManualParameter,
            unit="s",
            initial_value=0.0,
            vals=Numbers(min_value=0.0, max_value=1.0, allow_nan=True),
        )


class TwoStateReadout(InstrumentChannel):
    """The readout operating point used for *discriminating*, as opposed to measuring.

    Separate from ``measure`` because the two want different things and the difference
    is not small. `resonator_spectroscopy` and `resonator_punchout` find where the most
    signal comes back, which is what every calibration routine needs: those reduce an
    acquisition to a magnitude, and the magnitude contrast is largest on resonance. A
    discriminator uses the *complex* separation between the two clouds, and most of
    that is phase once the drive is off resonance — so its best point is somewhere
    else. Measured on the simulated chip: moving to it gains 2.6% of separation and
    costs 14% of magnitude contrast, which moved the CZ chevron's answer by 10 ns when
    the two shared one operating point.

    Zero frequency means "not calibrated", and the executor leaves the readout clock
    where the device config put it.
    """

    def __init__(self, parent, name):
        super().__init__(parent, name)

        self.add_parameter(
            "frequency",
            parameter_class=ManualParameter,
            unit="Hz",
            initial_value=0.0,
            vals=Numbers(min_value=0.0, max_value=1e12, allow_nan=True),
        )
        self.add_parameter(
            "pulse_amp",
            parameter_class=ManualParameter,
            unit="",
            initial_value=0.0,
            vals=Numbers(min_value=0.0, max_value=1.0, allow_nan=True),
        )
        self.add_parameter(
            "acq_rotation",
            parameter_class=ManualParameter,
            unit="degrees",
            initial_value=0.0,
            # The instrument's own range, and it says so: "Attempting to configure
            # acq_rotation to -153.73 ... while the hardware requires it to be
            # between 0 and 360."
            vals=Numbers(min_value=0.0, max_value=360.0, allow_nan=True),
        )
        self.add_parameter(
            "acq_threshold",
            parameter_class=ManualParameter,
            unit="",
            initial_value=0.0,
            vals=Numbers(min_value=-1e12, max_value=1e12, allow_nan=True),
        )


class ThreeStateReadout(InstrumentChannel):
    """The readout point that resolves ``|0>``, ``|1>`` and ``|2>`` at once.

    A third point rather than a variant of `TwoStateReadout`, because the two solve
    different problems. Tuned for ``|0>`` against ``|1>``, the readout sits where
    ``|1>`` and ``|2>`` both return almost nothing and their clouds collapse together
    — 2.95 sigma apart on the simulated chip, against 39 where the three-state point
    puts them. One setting cannot be best at both.

    No rotation and no threshold: three clouds have no line between them, so shots are
    classified by nearest centre instead. See `fit_three_state_discrimination`.
    """

    def __init__(self, parent, name):
        super().__init__(parent, name)

        self.add_parameter(
            "frequency",
            parameter_class=ManualParameter,
            unit="Hz",
            initial_value=0.0,
            vals=Numbers(min_value=0.0, max_value=1e12, allow_nan=True),
        )
        self.add_parameter(
            "pulse_amp",
            parameter_class=ManualParameter,
            unit="",
            initial_value=0.0,
            vals=Numbers(min_value=0.0, max_value=1.0, allow_nan=True),
        )


class EFDrive(InstrumentChannel):
    """The pulse that drives ``|1>`` to ``|2>``, as ``rxy`` is for ``|0>`` to ``|1>``.

    Named ``ef_`` after the transition rather than the levels, which is what the
    reference pipelines and most of the literature call it — and it keeps the two sets
    of parameters from reading as variants of each other when both are on one element.

    A separate drive rather than a scaled `rxy`. The matrix element between ``|1>``
    and ``|2>`` is larger than between ``|0>`` and ``|1>`` by about root two, but the
    interesting difference is not the factor: it is that the pulse plays on a
    different clock, at a different frequency, into a state that relaxes while it
    plays. Its amplitude is measured, not derived.
    """

    def __init__(self, parent, name):
        super().__init__(parent, name)

        self.add_parameter(
            "ef_amp180",
            parameter_class=ManualParameter,
            unit="",
            initial_value=0.0,
            vals=Numbers(min_value=0.0, max_value=1.0, allow_nan=True),
        )
        self.add_parameter(
            "ef_motzoi",
            parameter_class=ManualParameter,
            unit="",
            initial_value=0.0,
            vals=Numbers(min_value=-1.0, max_value=1.0, allow_nan=True),
        )
        # Zero, not a length: a default that disagreed with `rxy.duration` would be
        # silently wrong on any chip whose 0-1 pulse is not 20 ns. See `ef_duration`.
        self.add_parameter(
            "ef_duration",
            parameter_class=ManualParameter,
            unit="s",
            initial_value=0.0,
            vals=Numbers(min_value=0.0, max_value=1e-3, allow_nan=True),
        )


class FineRotation(InstrumentChannel):
    """A separately measured pi/2 amplitude, for when half a pi pulse is not one.

    Not on ``rxy`` beside ``amp180``, though that is where it belongs, because that
    submodule is the base element's and adding to it would change what a
    `BasicTransmonElement` serialises. Its own submodule keeps the opt-in the same
    shape as every other field here.

    Zero means "not measured", and the interpolation falls back to the straight line
    through ``amp180`` that both schedulers already draw — so an element that has
    never run `fine_amplitude_90` compiles bit-for-bit as it did before.
    """

    def __init__(self, parent, name):
        super().__init__(parent, name)

        self.add_parameter(
            "amp90",
            parameter_class=ManualParameter,
            unit="",
            initial_value=0.0,
            vals=Numbers(min_value=0.0, max_value=1.0, allow_nan=True),
        )


class CalibratedTransmon(BasicTransmonElement):
    """A transmon with somewhere to put every parameter the graph calibrates."""

    def __init__(self, name: str, **kwargs):
        super().__init__(name, **kwargs)
        self.add_submodule("spec", SpectroscopySettings(self, "spec"))
        self.add_submodule("resonator", ResonatorSettings(self, "resonator"))
        self.add_submodule("coherence", CoherenceTimes(self, "coherence"))
        self.add_submodule("measure_2state", TwoStateReadout(self, "measure_2state"))
        self.add_submodule("r12", EFDrive(self, "r12"))
        self.add_submodule("measure_3state", ThreeStateReadout(self, "measure_3state"))
        self.add_submodule("fine", FineRotation(self, "fine"))

    def _generate_config(self) -> dict[str, dict[str, Any]]:
        """The base element's config with ``Rxy`` re-pointed at :func:`rxy_drag_pulse`.

        Surgical on purpose: everything else the base builds — reset, Rz, H, measure,
        the pulse-compensation entry — is untouched, so this element tracks upstream
        changes to all of them and diverges on exactly the one operation it means to.
        """
        config = super()._generate_config()
        rxy = config[self.name][RXY_OPERATION]
        rxy.factory_func = rxy_drag_pulse
        rxy.factory_kwargs["amp90"] = self.fine.amp90()
        return config


def rxy_drag_pulse(
    amp180: float,
    amp90: float,
    motzoi: float,
    theta: float,
    phi: float,
    port: str,
    duration: float,
    clock: str,
    reference_magnitude: Any = None,
) -> Any:
    """quantify's ``rxy_drag_pulse``, with the amplitude off :func:`amplitude_for_angle`.

    A wrapper rather than a patch: the upstream factory is what a stock element uses
    and has to keep using, and its signature is the contract this has to match — the
    keyword names here are the keys of ``factory_kwargs``.
    """
    from quantify_scheduler.operations import pulse_library

    return pulse_library.DRAGPulse(
        G_amp=amplitude_for_angle(theta, amp180, amp90),
        D_amp=motzoi,
        phase=phi,
        port=port,
        duration=duration,
        clock=clock,
        reference_magnitude=reference_magnitude,
    )
