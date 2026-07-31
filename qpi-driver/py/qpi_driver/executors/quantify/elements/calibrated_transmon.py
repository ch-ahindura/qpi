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


class CalibratedTransmon(BasicTransmonElement):
    """A transmon with somewhere to put every parameter the graph calibrates."""

    def __init__(self, name: str, **kwargs):
        super().__init__(name, **kwargs)
        self.add_submodule("spec", SpectroscopySettings(self, "spec"))
