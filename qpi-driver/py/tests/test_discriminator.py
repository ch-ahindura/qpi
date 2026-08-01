"""Each qubit is assigned against its own line (RFC 0005 §7).

Scheduler-free: the resolution is plain attribute reading over whichever shape a
device exposes, so it is testable without either extra installed. What it guards is
the defect it was written for — the executors read the rotation and threshold from
whichever element had them first and applied that to every qubit in the circuit.
That was invisible for as long as nothing measured them, because an uncalibrated chip
carries zero everywhere and one qubit's zero is as good as another's.
"""

import pytest
from qpi_driver.executors.utils.counts import build_discriminator
from qpi_driver.executors.utils.discriminator import (
    discriminators_by_qubit,
    qubit_index,
    resolve_discriminators,
)


class _Measure:
    def __init__(self, rotation, threshold):
        self.acq_rotation = rotation
        self.acq_threshold = threshold


class _Element:
    def __init__(self, rotation, threshold):
        self.measure = _Measure(rotation, threshold)


class _Device:
    """Shaped like qblox's: ``elements`` a dict rather than a method."""

    def __init__(self, elements):
        self.elements = elements

    def get_element(self, name):
        return self.elements[name]


def _chip(**lines):
    return _Device({name: _Element(*pair) for name, pair in lines.items()})


def test_each_qubit_keeps_its_own_line():
    device = _chip(q0=(10.0, -1.0), q1=(200.0, 5.0), q2=(300.0, 9.0))

    assert discriminators_by_qubit(device) == {
        0: {"acq_rotation": 10.0, "acq_threshold": -1.0},
        1: {"acq_rotation": 200.0, "acq_threshold": 5.0},
        2: {"acq_rotation": 300.0, "acq_threshold": 9.0},
    }


def test_an_uncalibrated_qubit_is_absent_rather_than_zero():
    """Zero is a line, and a wrong one: it assigns by which side of the origin a
    cloud lands, which is a property of the cable rather than of the qubit."""
    device = _chip(q0=(10.0, -1.0), q1=(None, None))
    found, complete = resolve_discriminators(device, None, None)

    assert 1 not in found
    assert not complete, "one calibrated qubit is not a calibrated chip"


def test_the_payload_overrides_every_qubit_not_just_the_calibrated_ones():
    device = _chip(q0=(10.0, -1.0), q1=(None, None))
    found, complete = resolve_discriminators(device, rotation=45.0, threshold=0.1)

    assert complete
    assert all(
        entry == {"acq_rotation": 45.0, "acq_threshold": 0.1}
        for entry in found.values()
    )


def test_edges_are_not_qubits():
    assert qubit_index("q0") == 0
    assert qubit_index("q10") == 10
    assert qubit_index("q0_q1") is None
    assert qubit_index("coupler") is None


class _Dataset:
    def __init__(self, attrs):
        self.attrs = attrs


def test_software_discrimination_uses_each_qubits_own_line():
    """The same shot, assigned two ways, because the two qubits' lines differ."""
    discriminate = build_discriminator(
        _Dataset(
            {
                "acq_discriminators": {
                    "0": {"acq_rotation": 0.0, "acq_threshold": 0.0},
                    "1": {"acq_rotation": 0.0, "acq_threshold": 5.0},
                }
            }
        ),
        "SSBIntegrationComplex",
        lambda: {},
    )

    assert discriminate(2 + 0j, 0) == "1"  # clears q0's threshold of 0
    assert discriminate(2 + 0j, 1) == "0"  # but not q1's of 5


def test_software_discrimination_turns_the_plane_the_way_the_fit_defines_it():
    """`fit_readout_discrimination` reports the direction between the cloud centres
    and projects with the *negative* of it, so the separation lands on the real axis.
    Turning the other way is the same rotation only when it is zero, which is why an
    uncalibrated chip could never show the difference."""
    discriminate = build_discriminator(
        _Dataset(
            {"acq_discriminators": {"0": {"acq_rotation": 90.0, "acq_threshold": 0.5}}}
        ),
        "SSBIntegrationComplex",
        lambda: {},
    )

    # A shot at +90 degrees: turning back by 90 puts it on the positive real axis.
    assert discriminate(1j, 0) == "1"
    # And its opposite goes the other side, rather than both landing together.
    assert discriminate(-1j, 0) == "0"


def test_a_dataset_from_before_the_per_qubit_map_still_discriminates():
    """One pair in the attrs meant "this line, for everything", and still does."""
    discriminate = build_discriminator(
        _Dataset({"acq_rotation": 0.0, "acq_threshold": 1.0}),
        "SSBIntegrationComplex",
        lambda: {},
    )

    assert discriminate(2 + 0j, 0) == "1"
    assert discriminate(2 + 0j, 7) == "1"
    assert discriminate(0 + 0j, 3) == "0"


@pytest.mark.parametrize("qubit", [0, 1, 9])
def test_thresholded_acquisition_ignores_the_qubit_because_the_instrument_did_it(qubit):
    discriminate = build_discriminator(
        _Dataset({}), "ThresholdedAcquisition", lambda: {}
    )
    assert discriminate(1 + 0j, qubit) == "1"
    assert discriminate(0 + 0j, qubit) == "0"
