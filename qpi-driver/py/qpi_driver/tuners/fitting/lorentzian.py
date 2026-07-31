"""Lorentzian fits: resonator and qubit spectroscopy, and readout punchout."""

import logging

import numpy as np
from scipy.optimize import curve_fit

from .core import FitError, align, require_in_range, require_positive

log = logging.getLogger(__name__)


def lorentzian(
    f: np.ndarray | float,
    amplitude: float,
    centre: float,
    width: float,
    offset: float,
) -> np.ndarray | float:
    """``A·(γ/2)² / ((f-f₀)² + (γ/2)²) + c`` — a peak of FWHM ``γ`` at ``f₀``."""
    half = width / 2.0
    return amplitude * half**2 / ((f - centre) ** 2 + half**2) + offset


def _fit_lorentzian(
    frequencies: np.ndarray, signal: np.ndarray, *, what: str
) -> dict[str, float]:
    """Fit a Lorentzian, trying both a peak and a dip seed.

    Which way a spectroscopy feature points depends on the acquisition: a
    resonator scanned in transmission dips, one scanned in reflection peaks. The
    fit should not care, so both seeds are tried and the better residual wins.
    """
    x, y = align(frequencies, signal, what=what)
    span = float(x[-1] - x[0]) or 1.0
    offset_guess = float(np.median(y))
    width_guess = span / 10.0

    best: tuple[float, tuple[float, ...]] | None = None
    for extremum in (int(np.argmax(y)), int(np.argmin(y))):
        amplitude_guess = float(y[extremum]) - offset_guess
        if amplitude_guess == 0:
            continue
        try:
            popt, _ = curve_fit(
                lorentzian,
                x,
                y,
                p0=[amplitude_guess, float(x[extremum]), width_guess, offset_guess],
                maxfev=20000,
            )
            residual = float(np.sum((y - lorentzian(x, *popt)) ** 2))
            if best is None or residual < best[0]:
                best = (residual, tuple(float(v) for v in popt))
        except Exception:  # noqa: BLE001 - the other seed may still fit
            continue

    if best is None:
        raise FitError(f"could not fit {what}: neither a peak nor a dip converged")

    _residual, (amplitude, centre, width, _offset) = best
    centre = require_in_range(
        centre, float(np.min(x)), float(np.max(x)), what=f"{what} centre frequency"
    )
    linewidth = require_positive(abs(width), what=f"{what} linewidth")
    return {
        "frequency": centre,
        "linewidth": linewidth,
        "amplitude": float(amplitude),
        "quality_factor": centre / linewidth if linewidth else float("inf"),
    }


def fit_resonator_spectroscopy(
    frequencies: np.ndarray, signal: np.ndarray
) -> dict[str, float]:
    """Fit a resonator scan. Returns ``{'readout_frequency', 'linewidth', ...}``."""
    fitted = _fit_lorentzian(frequencies, signal, what="resonator spectroscopy")
    return {
        "readout_frequency": fitted["frequency"],
        "linewidth": fitted["linewidth"],
        "quality_factor": fitted["quality_factor"],
    }


def fit_qubit_spectroscopy(
    frequencies: np.ndarray, signal: np.ndarray
) -> dict[str, float]:
    """Fit a two-tone scan. Returns ``{'clock_freq_01', 'linewidth', ...}``."""
    fitted = _fit_lorentzian(frequencies, signal, what="qubit spectroscopy")
    return {
        "clock_freq_01": fitted["frequency"],
        "linewidth": fitted["linewidth"],
        "quality_factor": fitted["quality_factor"],
    }


def fit_punchout(powers: np.ndarray, frequencies: np.ndarray) -> dict[str, float]:
    """Find the readout power at which the resonator stops shifting.

    Punchout sweeps drive power and watches the fitted resonator frequency move
    from its dressed (low-power) value to its bare (high-power) one. The useful
    readout power is the highest one still in the dressed regime, taken here as
    the last power before the frequency has crossed halfway across the shift.

    **And the frequency at that power**, which is the whole reason this is a 2D
    sweep. The resonance moves *because* the power changed, so choosing a new
    power invalidates whatever frequency was measured at the old one — and the
    row this routine already fitted at the chosen power is the corrected value.
    Reporting only the power leaves the caller to drive a resonance that has
    since walked away from it.

    Returns ``{'readout_power', 'readout_frequency', 'dressed_frequency',
    'bare_frequency'}``.
    """
    x, y = align(powers, frequencies, what="punchout")
    order = np.argsort(x)
    x, y = x[order], y[order]

    dressed, bare = float(y[0]), float(y[-1])
    shift = bare - dressed
    if abs(shift) < 1e-9:
        raise FitError(
            "punchout shows no resonator shift across the power sweep — "
            "the range is too narrow to locate the dressed regime"
        )

    midpoint = dressed + shift / 2.0
    crossed = np.nonzero((y - midpoint) * np.sign(shift) >= 0)[0]
    index = max(int(crossed[0]) - 1, 0) if crossed.size else len(x) - 1

    return {
        "readout_power": float(x[index]),
        "readout_frequency": float(y[index]),
        "dressed_frequency": dressed,
        "bare_frequency": bare,
    }
