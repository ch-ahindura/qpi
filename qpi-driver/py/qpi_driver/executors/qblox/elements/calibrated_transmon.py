"""The qblox-scheduler twin of the quantify `CalibratedTransmon` (RFC 0005 §13).

Same parameters, same meaning, different mechanism: qblox's device elements are
pydantic models where quantify's are qcodes instruments. The two files are kept
deliberately parallel — a parameter present on one and missing on the other is how the
qblox tuner came to be unable to complete a calibration at all (RFC 0004 §11), and
quantify-scheduler is the one being deprecated, so this is the side that has to work.

See the quantify module for why these parameters exist.
"""

from typing import Any, Literal

from pydantic import Field

from qpi_driver.compat.qblox import (
    BasicTransmonElement,
    Numbers,
    Parameter,
    SchedulerSubmodule,
)
from qpi_driver.executors.base.rotations import amplitude_for_angle

#: Kept identical to the quantify element's bound: it describes what a pulse amplitude
#: can be, not which scheduler is emitting it.
MAX_SPECTROSCOPY_AMPLITUDE = 1.0

#: The gate whose amplitude interpolation this element replaces.
RXY_OPERATION = "Rxy"


class SpectroscopySettings(SchedulerSubmodule):
    """How hard to drive a qubit while looking for its transitions."""

    amplitude: float = Parameter(
        docstring="Drive amplitude for 0-1 spectroscopy. 0 if uncalibrated.",
        unit="",
        initial_value=0.0,
        vals=Numbers(
            min_value=0.0, max_value=MAX_SPECTROSCOPY_AMPLITUDE, allow_nan=True
        ),
    )
    amplitude_12: float = Parameter(
        docstring="Drive amplitude for 1-2 spectroscopy. 0 if uncalibrated.",
        unit="",
        initial_value=0.0,
        vals=Numbers(
            min_value=0.0, max_value=MAX_SPECTROSCOPY_AMPLITUDE, allow_nan=True
        ),
    )


class ResonatorSettings(SchedulerSubmodule):
    """What `resonator_spectroscopy` measured about the resonator. See the quantify twin."""

    linewidth: float = Parameter(
        docstring="Resonator FWHM in Hz, as fitted. 0 if not measured.",
        unit="Hz",
        initial_value=0.0,
        vals=Numbers(min_value=0.0, max_value=1e9, allow_nan=True),
    )
    contrast: float = Parameter(
        docstring="Peak-to-peak |0>-to-|1> magnitude, as `rabi` fitted it. 0 if not measured.",
        unit="",
        initial_value=0.0,
        vals=Numbers(min_value=0.0, max_value=1e3, allow_nan=True),
    )


class CoherenceTimes(SchedulerSubmodule):
    """What `t1` measured, as the ceiling `t2_echo` checks. See the quantify twin."""

    t1: float = Parameter(
        docstring="Relaxation time in s, as fitted. 0 if not measured.",
        unit="s",
        initial_value=0.0,
        vals=Numbers(min_value=0.0, max_value=1.0, allow_nan=True),
    )


class TwoStateReadout(SchedulerSubmodule):
    """The readout operating point used for discriminating. See the quantify twin."""

    frequency: float = Parameter(
        docstring="Readout frequency for discriminated shots. 0 if uncalibrated.",
        unit="Hz",
        initial_value=0.0,
        vals=Numbers(min_value=0.0, max_value=1e12, allow_nan=True),
    )
    pulse_amp: float = Parameter(
        docstring="Readout amplitude for discriminated shots. 0 if uncalibrated.",
        unit="",
        initial_value=0.0,
        vals=Numbers(min_value=0.0, max_value=1.0, allow_nan=True),
    )
    acq_rotation: float = Parameter(
        docstring="Rotation applied before thresholding, in the hardware's [0, 360).",
        unit="degrees",
        initial_value=0.0,
        vals=Numbers(min_value=0.0, max_value=360.0, allow_nan=True),
    )
    acq_threshold: float = Parameter(
        docstring="Threshold the rotated real part is compared against.",
        unit="",
        initial_value=0.0,
        vals=Numbers(min_value=-1e12, max_value=1e12, allow_nan=True),
    )


class ThreeStateReadout(SchedulerSubmodule):
    """The readout point that resolves all three levels. See the quantify twin."""

    frequency: float = Parameter(
        docstring="Readout frequency for three-state shots. 0 if uncalibrated.",
        unit="Hz",
        initial_value=0.0,
        vals=Numbers(min_value=0.0, max_value=1e12, allow_nan=True),
    )
    pulse_amp: float = Parameter(
        docstring="Readout amplitude for three-state shots. 0 if uncalibrated.",
        unit="",
        initial_value=0.0,
        vals=Numbers(min_value=0.0, max_value=1.0, allow_nan=True),
    )


class EFDrive(SchedulerSubmodule):
    """The pulse that drives ``|1>`` to ``|2>``. See the quantify twin."""

    ef_amp180: float = Parameter(
        docstring="Amplitude of a pi pulse on the 1-2 transition. 0 if uncalibrated.",
        unit="",
        initial_value=0.0,
        vals=Numbers(min_value=0.0, max_value=1.0, allow_nan=True),
    )
    ef_motzoi: float = Parameter(
        docstring="DRAG coefficient for the 1-2 pulse.",
        unit="",
        initial_value=0.0,
        vals=Numbers(min_value=-1.0, max_value=1.0, allow_nan=True),
    )
    ef_duration: float = Parameter(
        docstring="Length of the 1-2 pulse. 0 takes rxy.duration.",
        unit="s",
        initial_value=0.0,
        vals=Numbers(min_value=0.0, max_value=1e-3, allow_nan=True),
    )


class FineRotation(SchedulerSubmodule):
    """A separately measured pi/2 amplitude. See the quantify twin."""

    amp90: float = Parameter(
        docstring="Amplitude of a pi/2 pulse. 0 falls back to half of amp180.",
        unit="",
        initial_value=0.0,
        vals=Numbers(min_value=0.0, max_value=1.0, allow_nan=True),
    )


class CalibratedTransmon(BasicTransmonElement):
    """A transmon with somewhere to put every parameter the graph calibrates."""

    element_type: Literal["CalibratedTransmon"] = "CalibratedTransmon"
    spec: SpectroscopySettings = Field(
        default_factory=lambda: SpectroscopySettings(name="spec")
    )
    resonator: ResonatorSettings = Field(
        default_factory=lambda: ResonatorSettings(name="resonator")
    )
    coherence: CoherenceTimes = Field(
        default_factory=lambda: CoherenceTimes(name="coherence")
    )
    measure_2state: TwoStateReadout = Field(
        default_factory=lambda: TwoStateReadout(name="measure_2state")
    )
    r12: EFDrive = Field(default_factory=lambda: EFDrive(name="r12"))
    measure_3state: ThreeStateReadout = Field(
        default_factory=lambda: ThreeStateReadout(name="measure_3state")
    )
    fine: FineRotation = Field(default_factory=lambda: FineRotation(name="fine"))

    def _generate_config(self) -> dict[str, dict[str, Any]]:
        """The base element's config with ``Rxy`` re-pointed at :func:`rxy_drag_pulse`."""
        config = super()._generate_config()
        rxy = config[self.name][RXY_OPERATION]
        rxy.factory_func = rxy_drag_pulse
        rxy.factory_kwargs["amp90"] = self.fine.amp90
        return config


def rxy_drag_pulse(
    amp180: float,
    amp90: float,
    beta: float,
    theta: float,
    phi: float,
    port: str,
    duration: float,
    clock: str,
    reference_magnitude: Any = None,
) -> Any:
    """qblox's ``rxy_drag_pulse``, with the amplitude off :func:`amplitude_for_angle`.

    ``beta`` where quantify says ``motzoi`` and ``amplitude`` where it says ``G_amp``:
    the same DRAG pulse, renamed upstream. See the quantify twin.
    """
    from qblox_scheduler.operations import pulse_library

    return pulse_library.DRAGPulse(
        amplitude=amplitude_for_angle(theta, amp180, amp90),
        beta=beta,
        phase=phi,
        port=port,
        duration=duration,
        clock=clock,
        reference_magnitude=reference_magnitude,
    )
