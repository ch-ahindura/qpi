"""The qblox-scheduler twin of the quantify `CalibratedTransmon` (RFC 0005 §13).

Same parameters, same meaning, different mechanism: qblox's device elements are
pydantic models where quantify's are qcodes instruments. The two files are kept
deliberately parallel — a parameter present on one and missing on the other is how the
qblox tuner came to be unable to complete a calibration at all (RFC 0004 §11), and
quantify-scheduler is the one being deprecated, so this is the side that has to work.

See the quantify module for why these parameters exist.
"""

from typing import Literal

from pydantic import Field

from qpi_driver.compat.qblox import (
    BasicTransmonElement,
    Numbers,
    Parameter,
    SchedulerSubmodule,
)

#: Kept identical to the quantify element's bound: it describes what a pulse amplitude
#: can be, not which scheduler is emitting it.
MAX_SPECTROSCOPY_AMPLITUDE = 1.0


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


class CalibratedTransmon(BasicTransmonElement):
    """A transmon with somewhere to put every parameter the graph calibrates."""

    element_type: Literal["CalibratedTransmon"] = "CalibratedTransmon"
    spec: SpectroscopySettings = Field(
        default_factory=lambda: SpectroscopySettings(name="spec")
    )
