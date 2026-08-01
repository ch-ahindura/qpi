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

from qpi_driver.compat.quantify import (
    BasicTransmonElement,
    InstrumentChannel,
    ManualParameter,
    Numbers,
)

#: Drive amplitude a spectroscopy sweep may be asked for. The same full-scale bound the
#: schedulers put on any pulse amplitude: past one the waveform clips and the schedule
#: will not compile.
MAX_SPECTROSCOPY_AMPLITUDE = 1.0


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
        self.add_parameter(
            "ef_duration",
            parameter_class=ManualParameter,
            unit="s",
            initial_value=20e-9,
            vals=Numbers(min_value=0.0, max_value=1e-3, allow_nan=True),
        )


class CalibratedTransmon(BasicTransmonElement):
    """A transmon with somewhere to put every parameter the graph calibrates."""

    def __init__(self, name: str, **kwargs):
        super().__init__(name, **kwargs)
        self.add_submodule("spec", SpectroscopySettings(self, "spec"))
        self.add_submodule("measure_2state", TwoStateReadout(self, "measure_2state"))
        self.add_submodule("r12", EFDrive(self, "r12"))
        self.add_submodule("measure_3state", ThreeStateReadout(self, "measure_3state"))
