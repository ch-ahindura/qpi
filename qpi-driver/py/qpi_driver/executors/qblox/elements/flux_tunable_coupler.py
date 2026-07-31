from typing import Literal

from pydantic import Field

from qpi_driver.compat.qblox import (
    ClockResource,
    CompositeSquareEdge,
    Numbers,
    Parameter,
    SchedulerSubmodule,
    ShiftClockPhase,
    SoftSquarePulse,
    TimeableSchedule,
)

#: The parking current an S4g may be asked for, in amperes. Kept identical to
#: the quantify element's limit — it describes the chip's couplers, not the
#: scheduler driving them.
MAX_PARKING_CURRENT_A = 3.1e-3


class EdgeClockFrequencies(SchedulerSubmodule):
    cz: float = Parameter(
        docstring="Clock frequency for the CZ gate square pulse.",
        unit="Hz",
        initial_value=0.0,
        vals=Numbers(min_value=-1e12, max_value=1e12, allow_nan=True),
    )
    # The transition the CZ drive is meant to bridge, as opposed to `cz` which
    # is the frequency it is played at. Keeping them separate is what lets a
    # mistuned drive be a detectable mistake rather than a definition.
    sideband_gap: float = Parameter(
        docstring="The |11>-|02> transition the CZ drive bridges. 0 if uncharacterised.",
        unit="Hz",
        initial_value=0.0,
        vals=Numbers(min_value=0, max_value=1e12, allow_nan=True),
    )


class EdgeBias(SchedulerSubmodule):
    """The coupler's static flux bias — its DC sweet-spot parking point.

    Deliberately not part of the CZ schedule: the bias is a seconds-scale DC
    current from an SPI rack's S4g (or a QCM output), and neither scheduler has
    any way to express one. It lives on the edge because it is a property of the
    coupler rather than of either qubit, and in the device config because it is
    calibrated — a coupler parked at the wrong current has the wrong effective
    coupling, and every CZ across it is wrong in the way that looks like drift.
    """

    parking_current: float = Parameter(
        docstring="DC current holding the coupler at its operating point.",
        unit="A",
        initial_value=0.0,
        vals=Numbers(min_value=-MAX_PARKING_CURRENT_A, max_value=MAX_PARKING_CURRENT_A),
    )
    source: str = Parameter(
        docstring="Which mechanism delivers the bias: 'spi' or 'qcm'.",
        initial_value="spi",
    )
    spi_module: float = Parameter(
        docstring="S4g module slot the coupler is wired to.",
        initial_value=0,
        vals=Numbers(min_value=0, max_value=64),
    )
    spi_output: float = Parameter(
        docstring="S4g output on that module.",
        initial_value=0,
        vals=Numbers(min_value=0, max_value=64),
    )
    qcm_module: float = Parameter(
        docstring="Cluster module holding the offset, when the source is a QCM.",
        initial_value=0,
        vals=Numbers(min_value=0, max_value=64),
    )
    qcm_output: float = Parameter(
        docstring="Output on that module.",
        initial_value=0,
        vals=Numbers(min_value=0, max_value=64),
    )
    line_resistance_ohm: float = Parameter(
        docstring="Flux line resistance, for turning a current into a QCM voltage.",
        unit="Ohm",
        initial_value=0.0,
        vals=Numbers(min_value=0, max_value=1e6),
    )


class FluxTunableCoupler(CompositeSquareEdge):
    """An edge for a flux tunable coupler, labeled as {control_qubit}_{target_qubit}"""

    edge_type: Literal["FluxTunableCoupler"] = "FluxTunableCoupler"
    clock_freqs: EdgeClockFrequencies = Field(
        default_factory=lambda: EdgeClockFrequencies(name="clock_freqs")
    )
    bias: EdgeBias = Field(default_factory=lambda: EdgeBias(name="bias"))

    def generate_edge_config(self) -> dict:
        config = super().generate_edge_config()

        if self.name in config:
            if "CZ" in config[self.name]:
                op_config = config[self.name]["CZ"]
                op_config.factory_func = compile_cz
                if hasattr(op_config, "factory_kwargs"):
                    op_config.factory_kwargs["square_port"] = f"{self.name}:fl"
                    op_config.factory_kwargs["square_clock"] = f"{self.name}.cz"
                    op_config.factory_kwargs["square_freq"] = self.clock_freqs.cz

        return config


def compile_cz(
    square_amp: float,
    square_duration: float,
    square_port: str,
    square_clock: str,
    square_freq: float,
    virt_z_parent_qubit_phase: float,
    virt_z_parent_qubit_clock: str,
    virt_z_child_qubit_phase: float,
    virt_z_child_qubit_clock: str,
    t0: float = 0,
):
    sched = TimeableSchedule("CZ")
    sched.add_resource(ClockResource(name=square_clock, freq=square_freq))

    # Soft-edged rather than hard, matching the quantify element and the tergite
    # instruction both were ported from: a square pulse's discontinuities are
    # broadband and carry power at the 1-2 transition a CZ spends its whole
    # duration trying not to drive.
    pulse = sched.add(
        SoftSquarePulse(
            amplitude=square_amp,
            duration=square_duration,
            port=square_port,
            clock=square_clock,
            t0=t0,
        )
    )

    sched.add(
        ShiftClockPhase(
            phase_shift=virt_z_parent_qubit_phase,
            clock=virt_z_parent_qubit_clock,
            t0=t0,
        ),
        ref_op=pulse,
        ref_pt="start",
    )
    sched.add(
        ShiftClockPhase(
            phase_shift=virt_z_child_qubit_phase,
            clock=virt_z_child_qubit_clock,
            t0=t0,
        ),
        ref_op=pulse,
        ref_pt="start",
    )

    return sched
