"""Lorentzian fits: resonator and qubit spectroscopy, and readout punchout."""

import logging

import numpy as np
from scipy.optimize import curve_fit

from .core import FitError, align, fit_summary, require_in_range, require_positive

log = logging.getLogger(__name__)

#: How much broader than the narrowest line a drive power may make it before it
#: counts as power broadening. Generous: at 2x the linewidth the fitted centre is
#: still sound, and the point is to exclude the powers where it is not.
MAX_BROADENING = 2.0


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

    residual, (amplitude, centre, width, offset) = best
    centre = require_in_range(
        centre, float(np.min(x)), float(np.max(x)), what=f"{what} centre frequency"
    )
    linewidth = require_positive(abs(width), what=f"{what} linewidth")
    return {
        "frequency": centre,
        "linewidth": linewidth,
        "amplitude": float(amplitude),
        "quality_factor": centre / linewidth if linewidth else float("inf"),
        # Contrast over residual scatter, for *choosing between* drive powers — which a
        # bare height cannot do, since height rises with power right through the point
        # where the line stops being a measurement of anything.
        #
        # Deliberately not an absolute test of whether there is a line at all; that is
        # `reach`. ``amplitude`` is a free parameter, so an optimiser handed noise can
        # report any ratio it likes: across 1800 pure-noise fits that cleared the
        # linewidth test, this had a 99th percentile of 570 to 2700 and a maximum of 7048.
        "snr": abs(float(amplitude)) / max(float(np.sqrt(residual / x.size)), 1e-18),
        # How far the fitted *curve* actually travels across the sweep, over the scatter
        # left around it. Bounded on noise where `snr` is not, because the numerator is
        # the curve's realised span rather than a fitted parameter — an optimiser cannot
        # inflate it without the residuals growing to match. Measured on those same 1800
        # noise fits: 99th percentile 3.4 to 3.6, maximum 4.2. A real line reaches 93 on
        # this chip's resonator and 104 to 139 on the simulated qubit, at spans from
        # 4 MHz to 600 MHz.
        "reach": _curve_reach(y, lorentzian(x, amplitude, centre, width, offset)),
        "fit": fit_summary(
            x,
            y,
            lorentzian(x, amplitude, centre, width, offset),
            x_label="frequency (Hz)",
            y_label="signal",
        ),
    }


def fit_resonator_spectroscopy(
    frequencies: np.ndarray, signal: np.ndarray
) -> dict[str, float]:
    """Fit a resonator scan. Returns ``{'readout_frequency', 'linewidth', 'snr', ...}``."""
    fitted = _fit_lorentzian(frequencies, signal, what="resonator spectroscopy")
    return {
        "readout_frequency": fitted["frequency"],
        "linewidth": fitted["linewidth"],
        "quality_factor": fitted["quality_factor"],
        # Both forwarded because a caller cannot judge the fit without them, and they
        # answer different questions — see `require_resolved_line`. `snr` ranks drive
        # powers against each other; `reach` says whether there is a line at all.
        "snr": fitted["snr"],
        "reach": fitted["reach"],
        "fit": fitted["fit"],
    }


def fit_qubit_spectroscopy(
    frequencies: np.ndarray, signal: np.ndarray
) -> dict[str, float]:
    """Fit a two-tone scan. Returns ``{'clock_freq_01', 'linewidth', 'snr', ...}``."""
    fitted = _fit_lorentzian(frequencies, signal, what="qubit spectroscopy")
    return {
        "clock_freq_01": fitted["frequency"],
        "linewidth": fitted["linewidth"],
        "quality_factor": fitted["quality_factor"],
        "snr": fitted["snr"],
        "reach": fitted["reach"],
        "fit": fitted["fit"],
    }


def fit_spectroscopy_power(
    amplitudes: np.ndarray, frequencies: np.ndarray, signal: np.ndarray
) -> dict[str, float]:
    """Pick the drive power that shows the transition most clearly, and fit it there.

    A two-tone sweep at a single fixed power is a guess about the chip. Too weak and
    the line is inside the readout noise; too strong and it power-broadens, saturates,
    and eventually the two-photon 0-2 transition appears half an anharmonicity below
    the real one. RFC 0004 §11 records what the guess cost: a 1% default is a
    signal-to-noise of about five, and the same sweep returned centres 14 MHz low,
    12 MHz high and 62 MHz low on nothing but noise.

    So sweep power too, and choose. Not by peak height — height climbs with power
    straight through saturation, so the tallest peak is reliably the most broadened
    one. Rows broader than :data:`MAX_BROADENING` times the narrowest are dropped as
    broadened, and the best signal-to-noise among what is left wins. ``f01`` comes from
    that same row, because a centre fitted at a power that is not the chosen one is a
    measurement of a different line.

    Rows that do not fit, and rows whose fitted line is narrower than the sweep's own
    step, are dropped before any of that. At the bottom of a power sweep there is often
    no visible line, and `curve_fit` will still return one: a tidy, confident, very
    narrow peak fitted to the noise between two setpoints. Such a row is not merely a
    poor candidate, it is the *narrowest* one, so leaving it in makes it the reference
    every real row is then rejected against — which is how a 600 MHz sweep came back
    with a 12.9 kHz line and no answer at all.

    Returns ``{'clock_freq_01', 'drive_amplitude', 'linewidth', 'snr', ...}``.
    """
    powers = np.asarray(amplitudes, dtype=float).reshape(-1)
    rows = np.asarray(signal, dtype=float)
    if rows.ndim != 2 or rows.shape[0] != powers.size:
        raise FitError(
            f"spectroscopy power sweep expected one signal row per amplitude, got "
            f"signal shape {rows.shape} for {powers.size} amplitudes"
        )

    setpoints = np.asarray(frequencies, dtype=float).reshape(-1)
    step = abs(float(setpoints[1] - setpoints[0])) if setpoints.size > 1 else 0.0

    fits: list[tuple[float, dict[str, float]]] = []
    skipped: list[str] = []
    for power, row in zip(powers, rows):
        try:
            fit = _fit_lorentzian(frequencies, row, what="qubit spectroscopy")
        except FitError as error:
            skipped.append(f"{power:g}: {error}")
            continue
        if fit["linewidth"] < step:
            skipped.append(
                f"{power:g}: linewidth {fit['linewidth']:.4g} Hz below the "
                f"{step:.4g} Hz sweep step, so no line was resolved"
            )
            continue
        fits.append((float(power), fit))

    if not fits:
        raise FitError(
            "no drive power in the sweep resolved a line; the range may be entirely "
            "below the noise, or the sweep too coarse for this chip's linewidth — "
            + "; ".join(skipped)
        )

    narrowest = min(fit["linewidth"] for _power, fit in fits)
    resolved = [
        (power, fit)
        for power, fit in fits
        if fit["linewidth"] <= MAX_BROADENING * narrowest
    ]
    # `narrowest` is one of its own rows, so this is never empty.
    power, fit = max(resolved, key=lambda item: item[1]["snr"])
    log.debug(
        "spectroscopy power %g of %d fitted, snr %.1f, linewidth %.3g Hz",
        power,
        len(fits),
        fit["snr"],
        fit["linewidth"],
    )
    return {
        "clock_freq_01": fit["frequency"],
        "drive_amplitude": power,
        "linewidth": fit["linewidth"],
        "quality_factor": fit["quality_factor"],
        "snr": fit["snr"],
        "reach": fit["reach"],
        # The chosen row only. A summary per power would be a picture of the power
        # sweep, and the answer came from one row of it.
        "fit": fit["fit"],
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


def _curve_reach(signal: np.ndarray, curve: np.ndarray) -> float:
    """A fitted curve's span over the scatter left around it.

    The same quantity `require_resolved_curve` judges for a Rabi or a decay, computed
    here so a Lorentzian carries it too. Infinite when nothing is left over, which is a
    synthetic fit rather than a measurement and is left for the caller to allow.
    """
    scatter = float(np.sqrt(np.mean((np.asarray(signal) - np.asarray(curve)) ** 2)))
    span = float(np.max(curve) - np.min(curve))
    if scatter <= 0.0:
        return float("inf")
    return span / scatter
