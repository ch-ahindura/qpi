from qpi_driver.compat.quantify import (
    ClockResource,
    CompositeSquareEdge,
    InstrumentChannel,
    ManualParameter,
    Numbers,
    Schedule,
    ShiftClockPhase,
    SoftSquarePulse,
)

#: The parking current an S4g may be asked for, in amperes. The module's own
#: range is wider; this is the range this chip's couplers are validated over.
#: Typical operating values are 0 to ±1.2 mA, and most non-zero ones fall
#: between about 100 µA and 1.1 mA.
MAX_PARKING_CURRENT_A = 3.1e-3


class EdgeClockFrequencies(InstrumentChannel):
    def __init__(self, parent, name, **kwargs):
        super().__init__(parent, name)

        self.add_parameter(
            "cz",
            parameter_class=ManualParameter,
            unit="Hz",
            initial_value=kwargs.get("cz", 0.0),
            vals=Numbers(min_value=-1e12, max_value=1e12, allow_nan=True),
        )


class EdgeBias(InstrumentChannel):
    """The coupler's static flux bias — its DC sweet-spot parking point.

    Deliberately *not* part of the CZ schedule. The bias is a seconds-scale DC
    current, currently supplied by an SPI rack's S4g module over qcodes, and the
    CZ is a microwave pulse played on top of wherever that parked the coupler.
    Scheduling it would be a category error: quantify compiles nanosecond pulse
    programmes, and quantify-scheduler has no notion of an SPI rack at all.

    It lives on the edge because it is a property of the coupler rather than of
    either qubit, and it belongs in the device config because it is calibrated —
    a coupler parked at the wrong current has the wrong effective coupling, and
    every CZ across it is wrong in a way no pulse parameter can fix.
    """

    def __init__(self, parent, name, **kwargs):
        super().__init__(parent, name)

        self.add_parameter(
            "parking_current",
            parameter_class=ManualParameter,
            unit="A",
            initial_value=kwargs.get("parking_current", 0.0),
            vals=Numbers(
                min_value=-MAX_PARKING_CURRENT_A,
                max_value=MAX_PARKING_CURRENT_A,
            ),
        )
        #: Which mechanism delivers the bias. ``spi`` is an S4g current source
        #: driven out of band; ``qcm`` holds the same offset on a baseband
        #: module output, which puts it inside the cluster and removes an
        #: instrument from the rack.
        self.add_parameter(
            "source",
            parameter_class=ManualParameter,
            initial_value=kwargs.get("source", "spi"),
        )
        # Which output the coupler is wired to, for whichever source is in use.
        # Wiring is a property of the rack, and the device file is where the
        # rest of this coupler's setup already lives.
        for wiring, default in (
            ("spi_module", 0),
            ("spi_output", 0),
            ("qcm_module", 0),
            ("qcm_output", 0),
        ):
            self.add_parameter(
                wiring,
                parameter_class=ManualParameter,
                initial_value=kwargs.get(wiring, default),
                vals=Numbers(min_value=0, max_value=64),
            )
        #: A QCM holds a voltage, not a current, so turning one into the other
        #: needs the line's resistance. Zero means "not measured", and the QCM
        #: path refuses rather than guessing.
        self.add_parameter(
            "line_resistance_ohm",
            parameter_class=ManualParameter,
            unit="Ohm",
            initial_value=kwargs.get("line_resistance_ohm", 0.0),
            vals=Numbers(min_value=0, max_value=1e6),
        )


class FluxTunableCoupler(CompositeSquareEdge):
    """An edge for a flux tunable coupler, labeled as {control_qubit}_{target_qubit}"""

    def __init__(self, parent_element_name: str, child_element_name: str, **kwargs):
        clock_freqs_data = kwargs.pop("clock_freqs", {})
        bias_data = kwargs.pop("bias", {})
        super().__init__(parent_element_name, child_element_name, **kwargs)
        self.clock_freqs = EdgeClockFrequencies(self, "clock_freqs", **clock_freqs_data)
        self.add_submodule("clock_freqs", self.clock_freqs)
        self.bias = EdgeBias(self, "bias", **bias_data)
        self.add_submodule("bias", self.bias)

    def generate_edge_config(self) -> dict:
        config = super().generate_edge_config()

        if self.name in config:
            if "CZ" in config[self.name]:
                op_config = config[self.name]["CZ"]
                op_config.factory_func = compile_cz
                if hasattr(op_config, "factory_kwargs"):
                    op_config.factory_kwargs["square_port"] = f"{self.name}:fl"
                    op_config.factory_kwargs["square_clock"] = f"{self.name}.cz"
                    op_config.factory_kwargs["square_freq"] = self.clock_freqs.cz()

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
    sched = Schedule("CZ")
    sched.add_resource(ClockResource(name=square_clock, freq=square_freq))

    # Soft-edged rather than hard. A square pulse's discontinuities are broadband
    # — they carry power at every frequency, including the 1↔2 transition a CZ
    # spends its whole duration trying not to drive — so the same nominal gate
    # leaks more with sharp edges than with smoothed ones.
    pulse = SoftSquarePulse(
        amp=square_amp,
        duration=square_duration,
        port=square_port,
        clock=square_clock,
        t0=t0,
    )

    pulse.add_pulse(
        ShiftClockPhase(
            phase_shift=virt_z_parent_qubit_phase,
            clock=virt_z_parent_qubit_clock,
            t0=t0,
        )
    )
    pulse.add_pulse(
        ShiftClockPhase(
            phase_shift=virt_z_child_qubit_phase,
            clock=virt_z_child_qubit_clock,
            t0=t0,
        )
    )

    sched.add(pulse)
    return sched
