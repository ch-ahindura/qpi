"""The pi/2 amplitude and the interpolation that honours it (RFC 0007 §11.5)."""

import math

import pytest

from qpi_driver.compat.qblox import IS_QBLOX_SCHEDULER_INSTALLED
from qpi_driver.compat.quantify import IS_QUANTIFY_INSTALLED
from qpi_driver.executors.base.rotations import amplitude_for_angle

THETAS = (0.0, 1.0, 30.0, 45.0, 90.0, 120.0, 180.0, 270.0, -45.0, -90.0, -180.0)


class TestTheFallbackIsTheLineItReplaces:
    """An uncalibrated element has to compile exactly as it did before this existed.

    Not approximately: the field defaults to zero on every element that has never run
    `fine_amplitude_90`, which is every element on every chip until it does, so any
    difference here is a change to how a working chip's gates are played.
    """

    @pytest.mark.parametrize("theta", THETAS)
    def test_an_unmeasured_amp90_reproduces_the_straight_line(self, theta):
        assert amplitude_for_angle(theta, 0.5757) == 0.5757 * theta / 180

    @pytest.mark.parametrize("theta", THETAS)
    def test_a_nan_amp90_is_unmeasured_too(self, theta):
        """qcodes reads an uncalibrated parameter as NaN, which is not zero."""
        assert amplitude_for_angle(theta, 0.5757, math.nan) == 0.5757 * theta / 180

    @pytest.mark.parametrize("theta", THETAS)
    def test_an_amp90_of_exactly_half_reproduces_it_as_well(self, theta):
        """The measurement agreeing with the interpolation must be a no-op."""
        assert amplitude_for_angle(theta, 0.5757, 0.5757 / 2) == pytest.approx(
            0.5757 * theta / 180, abs=1e-15
        )


class TestBothMeasurementsAreHonoured:
    def test_the_two_measured_angles_get_the_amplitudes_measured_for_them(self):
        assert amplitude_for_angle(90, 0.5757, 0.3) == 0.3
        assert amplitude_for_angle(180, 0.5757, 0.3) == 0.5757

    def test_it_is_odd_in_the_angle(self):
        """Rxy(-90) is Rxy(90) about the opposite axis, not a different amplitude."""
        for theta in THETAS:
            assert amplitude_for_angle(-theta, 0.5757, 0.3) == -amplitude_for_angle(
                theta, 0.5757, 0.3
            )

    def test_it_stays_monotonic_where_a_fitted_curve_would_not(self):
        """0.3 against an interpolated 0.288 is a compression of only 4%, and a
        quadratic through the two points already turns back on itself before 180."""
        amplitudes = [amplitude_for_angle(t, 0.5757, 0.3) for t in range(0, 361)]
        assert all(b >= a for a, b in zip(amplitudes, amplitudes[1:]))

    def test_past_a_half_turn_the_upper_segment_continues(self):
        assert amplitude_for_angle(270, 0.5757, 0.3) == pytest.approx(
            0.3 + 2 * (0.5757 - 0.3)
        )


@pytest.mark.parametrize("scheduler", ["quantify", "qblox"])
def test_the_element_plays_rxy_at_the_interpolated_amplitude(scheduler):
    """The wiring, which the arithmetic above cannot check: that the element really
    replaces the factory and really passes its own ``amp90`` to it."""
    if scheduler == "qblox":
        if not IS_QBLOX_SCHEDULER_INSTALLED:
            pytest.skip("qblox-scheduler is not installed")
        from qpi_driver.executors.qblox.elements.calibrated_transmon import (
            CalibratedTransmon,
        )

        element = CalibratedTransmon(name="q0")
        element.rxy.amp180 = 0.5757
        element.fine.amp90 = 0.3
        # qblox's `pulse_info` is one mapping where quantify's is a list of them.
        amplitude_of = lambda pulse: pulse.data["pulse_info"]["amplitude"]  # noqa: E731
    else:
        if not IS_QUANTIFY_INSTALLED:
            pytest.skip("quantify-scheduler is not installed")
        from qpi_driver.compat.quantify import Instrument
        from qpi_driver.executors.quantify.elements.calibrated_transmon import (
            CalibratedTransmon,
        )

        Instrument.close_all()
        element = CalibratedTransmon("q0")
        element.rxy.amp180(0.5757)
        element.fine.amp90(0.3)
        amplitude_of = lambda pulse: pulse.data["pulse_info"][0]["G_amp"]  # noqa: E731

    rxy = element._generate_config()["q0"]["Rxy"]
    played = {
        theta: amplitude_of(rxy.factory_func(theta=theta, phi=0, **rxy.factory_kwargs))
        for theta in (90, 180)
    }
    assert played[90] == pytest.approx(0.3)
    assert played[180] == pytest.approx(0.5757)
