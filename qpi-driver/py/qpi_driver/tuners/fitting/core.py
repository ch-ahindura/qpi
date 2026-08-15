"""Shared fitting machinery: the failure type, the range guard, and dataset access."""

import logging
import math
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

#: The most points a fit summary keeps per trace (RFC 0006 §7). Every sweep in
#: ``calibration.example.yml`` is 41 points or fewer, so this is a ceiling rather
#: than a loss today — it is here to stop a wide sweep on someone else's chip
#: turning a report into something that will not save.
MAX_FIT_POINTS = 200

#: The largest widening a single refusal may ask for, as a multiple of the current extent.
#:
#: Escalation compounds — three rounds at this cap is 512x — and `_widened` scales the
#: point count with the span to hold the step size, so an uncapped factor buys reach by
#: spending the sequencer's acquisition budget on resolution nobody asked for.
MAX_REACH_FACTOR = 8.0


class CarriesFit:
    """Mixin for an error that hands back the sweep it refused.

    A guard rejecting a fit is the moment that fit is most worth looking at, and raising
    used to throw it away: the report kept the sentence and lost the trace. On the August
    2026 B chip that left three consecutive runs in which `rabi_12`'s ladder guard said the
    amplitude was 3.8x off and nothing could show whether the sweep behind it was a real
    oscillation or a harmonic of one — a question one glance at the trace settles.

    Opt-in per guard, because only some have a fit to give: a refusal that fires before
    anything was fitted has nothing to attach, and `fit=None` is then the honest answer.

    The DAG recovers it duck-typed, off `getattr(exc, "fit", None)`, which is why this can
    be a mixin on two unrelated exception hierarchies rather than a base class for both.
    """

    def __init__(self, *args: object, fit: dict | None = None) -> None:
        super().__init__(*args)
        self.fit = fit


class FitError(CarriesFit, Exception):
    """The data could not be fitted, or the fit is not physically usable."""


class OutOfRange(FitError):
    """A fit failed in a way that names what to sweep differently (RFC 0007 §6.1).

    A guard that refuses a curve is usually saying one of two things, and they want
    opposite responses: *this chip is dead*, or *you looked in the wrong place*. Prose
    cannot be acted on, so the second case raises this instead — carrying which axis was
    wrong and which way — and a caller may widen and try again rather than give up.

    Subclasses `FitError` deliberately: every existing `except FitError` keeps working, so
    a routine that does not know about escalation behaves exactly as it did.

    Attributes:
        axis: the sweep to change, named as the routine's config key — ``"delays"``.
        direction: ``"wider"`` for more reach, ``"finer"`` for more resolution over the
            same reach, ``"shorter"`` for less reach. They are different failures: a decay
            that never appeared wants a longer window, a fringe that aliased wants a denser
            one, and an amplified rotation that ran past its own linearisation wants fewer
            repetitions. Only the first two are generic — ``"shorter"`` is handled by the
            routine, because the sweeps that need it have a shape a stretch would break.
        factor: how much, as a multiplier on the extent or on the point count.
    """

    def __init__(
        self,
        message: str,
        *,
        axis: str,
        direction: str = "wider",
        factor: float = 4.0,
        fit: dict | None = None,
    ) -> None:
        super().__init__(message, fit=fit)
        self.axis = axis
        self.direction = direction
        self.factor = factor


def require_in_range(
    value: float,
    low: float,
    high: float,
    *,
    what: str,
    tolerance: float = 0.0,
    axis: str | None = None,
) -> float:
    """Return *value*, or raise if it falls outside ``[low, high]``.

    A fitted parameter outside the window that produced it is an extrapolation,
    not a measurement — the fitter wandered. Widening by *tolerance* (a fraction
    of the span) allows for a peak sitting exactly on the last setpoint.

    *axis* names the config key a caller may widen and try again with. Supplying it turns
    the refusal into an `OutOfRange`, which is the difference between "this chip is dead"
    and "you looked in the wrong place" — and a fitted centre outside its own window is
    nearly always the second. Left ``None`` it raises a plain `FitError`, so a caller with
    no sweep to widen, or one whose axis is a list of setpoints rather than a span,
    behaves exactly as it did.

    Raises:
        FitError: naming the value, the bound it broke and the window.
        OutOfRange: the same message, when *axis* says which sweep to widen.
    """
    if low > high:
        low, high = high, low
    span = high - low
    margin = abs(span) * tolerance
    if not np.isfinite(value):
        raise FitError(f"{what} is not finite ({value})")
    if value < low - margin or value > high + margin:
        message = (
            f"{what} fitted to {value:.6g}, outside the swept range "
            f"[{low:.6g}, {high:.6g}] — treating as a failed fit"
        )
        if axis is None:
            raise FitError(message)
        raise OutOfRange(
            message,
            axis=axis,
            direction="wider",
            factor=_reach_factor(value, low, high),
        )
    return float(value)


def _reach_factor(value: float, low: float, high: float) -> float:
    """How much wider a sweep must be before *value* could sit inside it, doubled.

    Doubled on purpose. A fitted centre outside its own window is an extrapolation, so it
    says the line is *past this edge* without saying how far past: the August 2026 B chip
    put a resonator 2.8 MHz below a 20 MHz window when the true offset was 12.8 MHz, and a
    sweep widened to contain the extrapolation exactly would have missed it a second time.
    """
    half = (high - low) / 2.0
    if half <= 0:
        return MAX_REACH_FACTOR
    excursion = max(low - value, value - high, 0.0)
    return min(2.0 * (half + excursion) / half, MAX_REACH_FACTOR)


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


#: The fewest points any fit here will take, and so the floor on any sweep that resizes
#: itself.
#:
#: Named because a routine that *shrinks* a sweep has to know it — `_shortened` cut a pi/2
#: ladder to two points, which `align` then refused, so the routine gave up its own retry
#: to produce a refusal about the retry rather than about the chip.
MIN_FIT_POINTS = 4


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
    if n < MIN_FIT_POINTS:
        raise FitError(
            f"{what} needs at least {MIN_FIT_POINTS} points to fit, got {n} "
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


#: How far a fitted curve must rise above the scatter it was drawn through before the
#: parameter taken from it counts as measured — see :func:`require_resolved_curve`.
#:
#: Three, from the spread of what has been measured rather than from theory. On the
#: simulated chip a Rabi sweep reaches 211, a Ramsey fringe 194, a T1 decay 117, and an
#: RB decay 128; a T1 through 5% noise still reaches 20. The four hardware fits that
#: wrote nonsense to the device reached 1.41 and 1.78 (Rabi), 0.83 (RB) and 0.47 (T1).
#:
#: The asymmetry sets it more than the gap does. A refused fit leaves the last good
#: value in place and says why; an accepted one overwrites it, and on this chip a single
#: flat Rabi sweep wrote an `amp180`36x too small and cost six runs before anything
#: noticed.
MIN_CURVE_TO_SCATTER = 3.0

#: What a five-parameter oscillatory fit can fake on *pure noise*, as a span-to-scatter
#: ratio times the square root of the number of points — see :func:`require_resolved_curve`,
#: which divides this by ``sqrt(n)`` to get the floor below which a fit is refused outright.
#:
#: Sixteen, measured the way :data:`MIN_LINE_REACH` was and for the same reason: a floor
#: taken from whichever chip last misbehaved is a floor that fits that chip. Six hundred
#: decaying-cosine fits through unit Gaussian noise at each length gave a 99th percentile
#: of 3.53 at 21 points, 2.36 at 41 and 1.84 at 81 — which is ``16/sqrt(n)`` to within 6%.
#:
#: The scaling is the point. A fixed floor is wrong at both ends: at 21 points noise
#: reaches 3.53, so the 3.0 above *admits* it, while at 81 points noise tops out at 1.84
#: and 3.0 throws away fits that are three standard errors clear of it. What noise can
#: counterfeit depends on how many points it had to counterfeit through, and on nothing
#: about the chip.
NOISE_FAKEABLE_SPAN = 16.0
#: How far a fitted line's *curve* must travel, against the scatter left around it,
#: before its centre counts as a frequency — the ``reach`` a Lorentzian fit reports.
#:
#: Five, from 1800 fits of pure noise that had already cleared the linewidth test below.
#: Their reach had a 99th percentile of 3.4 to 3.6 and a maximum of 4.2, at sweeps from
#: 51 to 301 points. A real line is nowhere near: 93 on this chip's resonator, and 104
#: to 139 on the simulated qubit at spans from 4 MHz to 600 MHz. So five refuses every
#: noise fit measured and passes every line measured by a factor of nineteen.
#:
#: This replaced a floor of 3.0 on ``snr``, which cannot do the job at any setting.
#: ``snr`` divides a *fitted parameter* by the residual, so an optimiser handed noise
#: can return whatever it likes — over those same 1800 fits its 99th percentile was 570
#: to 2700 and its maximum 7048, and **16% to 55% of them cleared 3.0**. No rescaling
#: separates a distribution with that tail from a real line at 127. ``snr`` is still
#: what ranks one drive power against another, which is a comparison rather than a
#: threshold, and is sound.
#:
#: The asymmetry is what sets the number rather than the gap: a refused fit leaves the
#: last good frequency in place and says why, while an accepted one overwrites it and
#: breaks every node downstream.
MIN_LINE_REACH = 5.0


def require_resolved_curve(
    y: np.ndarray,
    curve: np.ndarray,
    *,
    what: str,
    consequence: str,
    factor: float = MIN_CURVE_TO_SCATTER,
    axis: str | None = None,
    direction: str = "wider",
    fit: dict | None = None,
) -> None:
    """Refuse a fit whose curve is no taller than the noise it was fitted through.

    `curve_fit` always returns parameters. On data with no feature in it the ones it
    returns are read off the noise, and every routine here writes its answer to the
    device — so the failure is not a bad number in a report, it is a bad number in the
    calibration that the next run then builds on.

    Compared as a span rather than by any parameter's sign or magnitude: which way a
    feature points depends on the acquisition, and a *small* fitted parameter is
    sometimes exactly what success looks like — `fine_amplitude`'s slope, for one. What
    is never right is a curve the data cannot distinguish from a flat line.

    Raises:
        FitError: naming both numbers, their ratio and *consequence*, since what to do
            about it differs per fit — more averaging, a wider sweep, or a readout that
            resolves the qubit at all.
    """
    scatter = float(np.sqrt(np.mean((np.asarray(y) - np.asarray(curve)) ** 2)))
    if scatter <= 0.0:
        return
    span = float(np.max(curve) - np.min(curve))
    ratio = span / scatter
    points = int(np.size(y))
    floor = NOISE_FAKEABLE_SPAN / math.sqrt(points) if points > 0 else factor
    if floor <= ratio < factor:
        # Poor but real, so it is reported rather than refused. Above the floor the span
        # is further from the sweep than noise of that length reaches, which makes it a
        # measurement — an imprecise one, and the node reading it can say so on its own
        # evidence. Refusing here instead used to take every node downstream with it,
        # none of which had been given the chance: one `rabi_12` at 2.5 against a fixed
        # bar of 3 cost `ef_ladder` and the whole three-state chain, four nodes that each
        # carry their own guard.
        log.warning(
            "the fitted %s spans %.4g against a residual scatter of %.4g — %.1fx, under "
            "the %.1fx a well-resolved %s shows but over the %.1fx noise fakes at %d "
            "points. Taken as measured and degraded, not refused",
            what,
            span,
            scatter,
            ratio,
            factor,
            what,
            floor,
            points,
        )
        return
    if ratio < factor:
        message = (
            f"the fitted {what} spans {span:.4g} against a residual scatter of "
            f"{scatter:.4g} — {ratio:.1f}x, under the {floor:.1f}x that pure noise fakes "
            f"over {points} points, so this is not a {what} at all — {consequence}"
        )
        # With an *axis*, the caller has said which sweep could be wrong, so this becomes
        # something a routine can act on rather than only report — see `OutOfRange`. A
        # curve flatter than its own noise is the signature of a window that missed, and
        # for a decay the window is nearly always too short rather than too long.
        if axis is not None:
            raise OutOfRange(message, axis=axis, direction=direction, fit=fit)
        raise FitError(message, fit=fit)
