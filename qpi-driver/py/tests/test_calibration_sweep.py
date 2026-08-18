"""What a target's `Sweep` carries, and what it says when asked for what it has not."""

import pytest
from qpi_driver.tuners.base.sweep import Sweep


def test_it_carries_setpoints_by_axis_name():
    sweep = Sweep("q0")
    sweep["delays"] = [0.0, 1e-6]

    assert sweep["delays"] == [0.0, 1e-6]
    assert "delays" in sweep
    assert "amplitudes" not in sweep


def test_axes_can_be_supplied_at_construction():
    sweep = Sweep("q1", depths=[1, 2, 4])

    assert sweep["depths"] == [1, 2, 4]
    assert sweep.target == "q1"


def test_a_missing_axis_names_the_target_and_what_was_swept():
    """`_widened` reads an axis by the name a refusal gave it, so a typo has to say so
    rather than raising a bare KeyError against a name nobody can place."""
    sweep = Sweep("q2")
    sweep["frequencies"] = [1.0]

    with pytest.raises(KeyError, match="q2 swept no 'motzois'; it swept frequencies"):
        sweep["motzois"]


def test_an_empty_sweep_says_it_swept_nothing():
    with pytest.raises(KeyError, match="swept nothing"):
        Sweep("q0")["delays"]


def test_get_falls_back_rather_than_raising():
    """Several routines read an axis they may not have swept — a check schedule that
    reuses the calibration's grid only when there is one."""
    assert Sweep("q0").get("delays") is None
    assert Sweep("q0").get("delays", ()) == ()


def test_it_reprs_its_target_and_axes():
    """It appears in test failures and log lines, so it has to name the target."""
    assert repr(Sweep("q0", delays=[1], amplitudes=[2])) == (
        "Sweep('q0', amplitudes, delays)"
    )
