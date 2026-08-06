"""Shared fitting machinery: the failure type, the range guard, and dataset access."""

import logging
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

#: The most points a fit summary keeps per trace (RFC 0006 §7). Every sweep in
#: ``calibration.example.yml`` is 41 points or fewer, so this is a ceiling rather
#: than a loss today — it is here to stop a wide sweep on someone else's chip
#: turning a report into something that will not save.
MAX_FIT_POINTS = 200


class FitError(Exception):
    """The data could not be fitted, or the fit is not physically usable."""


def require_in_range(
    value: float, low: float, high: float, *, what: str, tolerance: float = 0.0
) -> float:
    """Return *value*, or raise if it falls outside ``[low, high]``.

    A fitted parameter outside the window that produced it is an extrapolation,
    not a measurement — the fitter wandered. Widening by *tolerance* (a fraction
    of the span) allows for a peak sitting exactly on the last setpoint.

    Raises:
        FitError: naming the value, the bound it broke and the window.
    """
    if low > high:
        low, high = high, low
    span = high - low
    margin = abs(span) * tolerance
    if not np.isfinite(value):
        raise FitError(f"{what} is not finite ({value})")
    if value < low - margin or value > high + margin:
        raise FitError(
            f"{what} fitted to {value:.6g}, outside the swept range "
            f"[{low:.6g}, {high:.6g}] — treating as a failed fit"
        )
    return float(value)


def require_positive(value: float, *, what: str) -> float:
    """Return *value*, or raise if it is not finite and strictly positive."""
    if not np.isfinite(value) or value <= 0:
        raise FitError(f"{what} fitted to {value:.6g}, which is not physical")
    return float(value)


def signal_of(dataset: Any) -> np.ndarray:
    """The measured signal as a real 1-D array, whatever shape the backend returned.

    Acquisition datasets differ between schedulers and between acquisition
    protocols: a bare array, an ``xarray`` Dataset with one data variable, or a
    complex IQ trace. All three reduce to the magnitude of a flat vector, which
    is what every fit in this package consumes.

    Raises:
        FitError: if there is no numeric data to read.
    """
    values: Any = dataset

    data_vars = getattr(dataset, "data_vars", None)
    if data_vars is not None:
        names = list(data_vars)
        if not names:
            raise FitError("acquisition dataset has no data variables")
        values = dataset[names[0]]

    values = np.asarray(getattr(values, "values", values))
    if values.size == 0:
        raise FitError("acquisition returned no data points")

    values = values.reshape(-1)
    if np.iscomplexobj(values):
        values = np.abs(values)
    else:
        values = values.astype(float, copy=False)

    if not np.all(np.isfinite(values)):
        finite = values[np.isfinite(values)]
        if finite.size == 0:
            raise FitError("acquisition returned no finite data points")
        values = np.nan_to_num(values, nan=float(np.mean(finite)))

    return values


def align(x: np.ndarray, y: np.ndarray, *, what: str) -> tuple[np.ndarray, np.ndarray]:
    """Trim *x* and *y* to a common length, or raise if there is nothing to fit.

    A schedule builds one acquisition per setpoint, so the two should already
    match; they diverge when a backend appends a calibration point of its own.
    Trimming is right where dropping the mismatch silently would not be.

    Raises:
        FitError: if either axis is too short to fit.
    """
    x = np.asarray(x, dtype=float).reshape(-1)
    y = np.asarray(y, dtype=float).reshape(-1)
    n = min(x.size, y.size)
    if n < 4:
        raise FitError(
            f"{what} needs at least 4 points to fit, got {n} "
            f"(setpoints={x.size}, acquisitions={y.size})"
        )
    return x[:n], y[:n]


def estimate_frequency(x: np.ndarray, y: np.ndarray) -> float:
    """A starting frequency for an oscillatory fit, from the dominant FFT bin.

    Falls back to one cycle across the window when the transform is
    uninformative, which is the right guess for a sweep chosen to show one.
    """
    n = len(x)
    span = float(x[-1] - x[0]) or 1.0
    if n < 4:
        return 1.0 / span

    spacing = span / (n - 1)
    spectrum = np.abs(np.fft.rfft(y - np.mean(y)))
    frequencies = np.fft.rfftfreq(n, d=spacing)
    if spectrum.size < 2:
        return 1.0 / span

    peak = int(np.argmax(spectrum[1:]) + 1)
    guess = float(frequencies[peak])
    return guess if guess > 0 else 1.0 / span


def fit_summary(
    x: Any,
    measured: Any,
    fitted: Any,
    *,
    x_label: str = "",
    y_label: str = "",
    x_scale: str = "linear",
) -> dict[str, Any]:
    """The sweep behind a fit, for the dashboard to draw (RFC 0006 §7).

    Two traces over one axis: what was measured, and the fitted curve evaluated on
    the same setpoints. No residuals — those are a subtraction the client can do.

    *x_scale* is ``"log"`` where the sweep is logarithmic, which is the difference
    between a readable RB decay and a line hugging the axis.

    Points where any of the three is not finite are dropped rather than sent: JSON
    has no NaN, and a report carrying one fails to unmarshal on the other side
    instead of showing a gap.
    """
    x = np.asarray(x, dtype=float).reshape(-1)
    measured = np.asarray(measured, dtype=float).reshape(-1)
    fitted = np.asarray(fitted, dtype=float).reshape(-1)

    size = min(x.size, measured.size, fitted.size)
    x, measured, fitted = x[:size], measured[:size], fitted[:size]
    usable = np.isfinite(x) & np.isfinite(measured) & np.isfinite(fitted)
    keep = _thinned(int(np.count_nonzero(usable)))

    return {
        "x": [float(v) for v in x[usable][keep]],
        "measured": [float(v) for v in measured[usable][keep]],
        "fitted": [float(v) for v in fitted[usable][keep]],
        "x_label": x_label,
        "y_label": y_label,
        "x_scale": x_scale,
    }


def _thinned(count: int) -> np.ndarray:
    """Indices keeping at most :data:`MAX_FIT_POINTS` of *count*, both ends included."""
    if count <= MAX_FIT_POINTS:
        return np.arange(count)
    return np.unique(np.linspace(0, count - 1, MAX_FIT_POINTS).round().astype(int))
