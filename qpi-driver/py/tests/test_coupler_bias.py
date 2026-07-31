"""Which parking current goes where, and by which mechanism (RFC 0004 §7).

The interesting part of coupler biasing is decided long before any instrument is
touched: which edges are biased, at what current, and through an SPI rack or a
cluster output. That is what these cover. Opening a real S4g is not — there is
no rack in CI, and a test that mocked one would only assert that qcodes was
called the way the mock expected.
"""

import pytest
from qpi_driver.executors.quantify.coupler_bias import (
    QCM,
    SPI,
    RecordingBias,
    apply_coupler_bias,
    resolve_bias_source,
)


class FakeParameter:
    def __init__(self, value):
        self._value = value

    def __call__(self):
        return self._value


class FakeBias:
    def __init__(self, **values):
        self.parameters = {k: FakeParameter(v) for k, v in values.items()}


class FakeEdge:
    def __init__(self, bias=None):
        if bias is not None:
            self.bias = bias


class FakeDevice:
    def __init__(self, edges):
        self._edges = edges

    def edges(self):
        return list(self._edges)

    def get_edge(self, name):
        return self._edges[name]


def device_with(**edges) -> FakeDevice:
    return FakeDevice(edges)


def test_every_biased_coupler_is_parked():
    device = device_with(
        q0_q1=FakeEdge(FakeBias(parking_current=0.0009, source=SPI)),
        q1_q2=FakeEdge(FakeBias(parking_current=-0.0011, source=SPI)),
    )
    source = RecordingBias()

    assert apply_coupler_bias(device, source) == {
        "q0_q1": 0.0009,
        "q1_q2": -0.0011,
    }
    assert source.applied == {"q0_q1": 0.0009, "q1_q2": -0.0011}


def test_an_edge_with_no_coupler_is_left_alone():
    """A stock CompositeSquareEdge has no bias submodule and needs none.

    Its CZ puts flux on a qubit's own port; there is no coupler to park. Reading
    a parking current off it would be inventing one.
    """
    device = device_with(
        q0_q1=FakeEdge(),  # no bias at all
        q1_q2=FakeEdge(FakeBias(parking_current=0.0005, source=SPI)),
    )
    source = RecordingBias()

    assert apply_coupler_bias(device, source) == {"q1_q2": 0.0005}


def test_a_zero_current_is_not_driven():
    """Zero is the default, and means "not biased" rather than "source 0 A"."""
    device = device_with(q0_q1=FakeEdge(FakeBias(parking_current=0.0, source=SPI)))
    source = RecordingBias()

    assert apply_coupler_bias(device, source) == {}
    assert source.applied == {}


def test_a_chip_with_no_biased_couplers_needs_no_rack():
    """No SPI address required when nothing asks to be parked.

    Most chips here have no tunable coupler at all, and a driver that demanded a
    serial port before it would start would be unusable on them.
    """
    device = device_with(q0_q1=FakeEdge())
    assert isinstance(resolve_bias_source(device), RecordingBias)


def test_spi_biasing_needs_an_address():
    device = device_with(q0_q1=FakeEdge(FakeBias(parking_current=0.001, source=SPI)))
    with pytest.raises(ValueError, match="spi_rack_address"):
        resolve_bias_source(device, spi_address="")


def test_qcm_biasing_needs_a_cluster():
    device = device_with(q0_q1=FakeEdge(FakeBias(parking_current=0.001, source=QCM)))
    with pytest.raises(ValueError, match="no cluster"):
        resolve_bias_source(device, cluster=None)


def test_couplers_may_not_disagree_about_the_mechanism():
    """One rack cannot be two things.

    The source is a property of how the fridge is wired, not of a single
    coupler, so a config where two edges disagree is a mistake rather than a
    setup to support — and silently picking one would park half the chip
    through hardware that is not connected to it.
    """
    device = device_with(
        q0_q1=FakeEdge(FakeBias(parking_current=0.001, source=SPI)),
        q1_q2=FakeEdge(FakeBias(parking_current=0.001, source=QCM)),
    )
    with pytest.raises(ValueError, match="disagree"):
        resolve_bias_source(device, spi_address="/dev/ttyACM0")


def test_an_unknown_mechanism_is_refused():
    device = device_with(
        q0_q1=FakeEdge(FakeBias(parking_current=0.001, source="carrier-pigeon"))
    )
    with pytest.raises(ValueError, match="unknown coupler bias source"):
        resolve_bias_source(device, spi_address="/dev/ttyACM0")


def test_the_qcm_path_refuses_to_guess_a_line_resistance():
    """A QCM holds a voltage; turning a current into one needs the resistance.

    Guessing it would park the coupler at the wrong point and every CZ across
    it would be wrong, which is exactly the failure that looks like drift.
    """
    from qpi_driver.executors.quantify.coupler_bias import QcmBias

    with pytest.raises(ValueError, match="line_resistance_ohm"):
        QcmBias(cluster=object()).apply(
            "q0_q1", 0.001, {"qcm_module": 4, "qcm_output": 0}
        )


def test_the_qcm_path_refuses_unmapped_wiring():
    from qpi_driver.executors.quantify.coupler_bias import QcmBias

    with pytest.raises(ValueError, match="which module"):
        QcmBias(cluster=object()).apply("q0_q1", 0.001, {"line_resistance_ohm": 1000})
