"""Exponential fits: T1, T2 echo, and randomized-benchmarking decay."""

import logging
import math

import numpy as np
from scipy.optimize import curve_fit

from .core import (
    FitError,
    NOISE_FAKEABLE_SPAN,
    OutOfRange,
    align,
    fit_summary,
    require_in_range,
    require_positive,
    require_resolved_curve,
)

log = logging.getLogger(__name__)


def exponential_decay(
    t: np.ndarray | float, amplitude: float, tau: float, offset: float
) -> np.ndarray | float:
    """``A·exp(-t/τ) + c``."""
    return amplitude * np.exp(-t / tau) + offset


def _fit_exponential(x: np.ndarray, y: np.ndarray, *, what: str) -> tuple[float, ...]:
    span = float(x[-1] - x[0]) or 1.0
    offset_guess = float(y[-1])
    amplitude_guess = float(y[0]) - offset_guess or 1.0

    last_error: Exception | None = None
    for tau_guess in (span / 3.0, span, span / 10.0):
        try:
            popt, _ = curve_fit(
                exponential_decay,
                x,
                y,
                p0=[amplitude_guess, tau_guess, offset_guess],
                maxfev=20000,
            )
            return tuple(float(v) for v in popt)
        except Exception as exc:  # noqa: BLE001 - reported below if all seeds fail
            last_error = exc

    raise FitError(f"could not fit {what}: {last_error}")


def _fit_coherence(
    delays: np.ndarray, signal: np.ndarray, *, key: str, what: str
) -> dict[str, float]:
    """Shared body of :func:`fit_t1` and :func:`fit_t2` — same curve, different name."""
    x, y = align(delays, signal, what=what)
    amplitude, tau, offset = _fit_exponential(x, y, what=what)

    value = require_positive(abs(tau), what=what)
    # A time constant far beyond the window was never observed, only extrapolated — and
    # naming the axis is what turns that from a verdict into an instruction. The guard
    # below says the same thing about a flat curve and has always been escalatable; this
    # one is reached first whenever the extrapolation lands on a number rather than on
    # noise, and without an axis it stopped `T2Echo.measure` before it could widen. The
    # August 2026 B chip fitted 2.12 ms of T2 over a 100 us window and failed there, on a
    # chip whose T1 was 56 us.
    require_in_range(value, 0.0, float(np.max(x)) * 10, what=what, axis="delays")
    require_resolved_curve(
        y,
        exponential_decay(x, amplitude, tau, offset),
        what=f"{what} decay",
        consequence=(
            f"the decay was never seen in this window — a {what} read off a curve "
            "the data cannot tell from a flat line is not a coherence time. Lengthen "
            "the delays, or average more shots"
        ),
        # Escalatable: "lengthen the delays" is an instruction, and a caller that can
        # follow it should not have to parse prose to know that.
        axis="delays",
    )
    return {
        key: value,
        "amplitude": float(amplitude),
        "fit": fit_summary(
            x,
            y,
            exponential_decay(x, amplitude, tau, offset),
            x_label="delay (s)",
            y_label="signal",
        ),
    }


def fit_t1(delays: np.ndarray, signal: np.ndarray) -> dict[str, float]:
    """Fit a T1 relaxation curve. Returns ``{'t1', 'amplitude'}``."""
    return _fit_coherence(delays, signal, key="t1", what="T1")


#: How far past ``2*T1`` a fitted T2 may sit before it counts as an unconstrained fit
#: rather than as a long-lived qubit.
#:
#: A Hahn echo refocuses static dephasing and nothing else, so ``2*T1`` is a hard ceiling
#: rather than a typical value — a qubit with no pure dephasing left sits *at* it. Both
#: times are fitted, though, so the ratio carries both fits' error and a genuinely
#: T1-limited echo can read a little high.
#:
#: A little. 1.5 was chosen to leave room and left far too much: it puts the bar at
#: ``3*T1``, half again past a bound nothing can exceed, and the 2026-08-16 B chip walked
#: under it by six hundred picoseconds — 148.17 us of T2 against a 49.41 us T1, exactly
#: three times it, reported unflagged as though it were a coherence time. A decay fitted
#: over two time constants does not carry 50% of error; 20% is generous for one that does.
#:
#: What sits past this is not a long-lived qubit but a fit describing something else, and
#: drift across a sweep that takes minutes is the usual candidate — it looks exactly like
#: a slow decay and has no reason to respect T1.
MAX_T2_OVER_T1 = 1.2


def fit_t2(delays: np.ndarray, signal: np.ndarray, t1: float = 0.0) -> dict[str, float]:
    """Fit a T2 echo curve. Returns ``{'t2', 'amplitude', 'unresolved'}``.

    *t1* is the relaxation time measured on the same qubit, zero when it never was. Given
    one, a T2 past ``2*T1`` is *reported* and flagged — see :data:`MAX_T2_OVER_T1`.

    Raises:
        FitError: if the curve cannot be fitted at all.
    """
    fitted = _fit_coherence(delays, signal, key="t2", what="T2")
    ceiling = 2.0 * float(t1)
    unresolved = bool(t1) and fitted["t2"] > ceiling * MAX_T2_OVER_T1
    if unresolved:
        # Said, not raised. `t2_echo` writes nothing, so an unconstrained coherence time
        # corrupts no later node — and a Hahn echo that outruns 2*T1 is still a measurement
        # of something, namely that the window did not contain the decay. Refusing it
        # reported nothing at all about the qubit's dephasing, which is worse than
        # reporting a bound with the reason it is only a bound.
        log.warning(
            "T2 fitted to %.4g s, above the %.4g s that 2*T1 allows a Hahn echo — %.2fx "
            "it, from a T1 of %.4g s. An echo cannot outlast twice the relaxation it "
            "refocuses through, so this is a lower bound set by the window rather than a "
            "coherence time. Lengthen the delays, or check the T1 it is measured against",
            fitted["t2"],
            ceiling,
            fitted["t2"] / ceiling,
            float(t1),
        )
    return {**fitted, "unresolved": float(unresolved)}


#: How far past the observed span the fitted amplitude may reach before the fit counts as
#: unidentified, as a multiple of that span.
#:
#: The far end of the trade-off :func:`fit_rb_decay` describes. Leaving ``A`` unbounded is
#: right — bounding it tightly pins every good chip near 0.98 — and it has a limit nothing
#: was checking: as ``|A|`` grows the exponential flattens into its own linear limit,
#: ``a*r^m + b -> a*(1 + m*ln r) + b``, and a straight line through RB data is fitted by
#: pinning ``r`` at one. The fidelity then comes off the boundary rather than off the chip.
#:
#: Twice on the August 2026 B chip, which reported 0.9999887 and 0.9999978 — an error per
#: gate of 1.1e-05 and 2.2e-06, thirty to three hundred times below what its 56 us T1
#: allows a 56 ns gate. ``A`` came out at -807 and -4109 on a survival normalised to
#: ``[0, 1]``. Not only there: the simulated chip's own RB fitted ``A = 4180`` against a
#: configured 0.001 per gate, reporting three nines it did not have through every full-DAG
#: run this repository had made.
#:
#: A *bound* alone only moves the wall — both of those then pin against it. What separates
#: them from a real decay is landing *on* it: a real one fits ``A`` near the span it spans,
#: and a fit that still reaches the wall was stopped rather than found.
#:
#: A hundred rather than the two hundred this began at, because two hundred left room to
#: squeeze *under*. The 2026-08-15 B chip fitted ``A`` at 199 times its span — one part in
#: two hundred inside the bound, so nothing fired — with an asymptote of -16.94 for a
#: survival, and reported 3.0e-05 per Clifford against the 9.5% AllXY measured on the same
#: chip in the same run. Measured against clean decays fitted over the same depths: ``p =
#: 0.9`` lands at 1.1, ``p = 0.99`` at 2.2 and ``p = 0.9998`` — slow enough that the curve
#: has barely bent — at 50. A hundred is twice the slowest of those and half the degenerate
#: one, and the gap between them is two orders wide.
MAX_AMPLITUDE_REACH = 100.0

#: How far the deepest RB sequence must have decayed before the fit's rate means anything.
#:
#: `a` and `r` separate only once the curve bends. Below this the exponential is the
#: straight line through it, only the product ``a*(1-r)`` sets the slope, and `r` — with
#: the fidelity read off it — is assumed rather than measured. It equals ``span / |a|``,
#: so this is the bound :data:`MAX_AMPLITUDE_REACH` was reaching for, at a value a
#: degenerate fit cannot sit under: the 2026-08-15 B chip fitted ``a = 17.85`` on a
#: survival spanning 0.090, one part in two hundred inside that cap, and reported 3.0e-05
#: per gate against the 9.5% AllXY measured on the same chip in the same run.
#:


def fit_rb_decay(
    depths: np.ndarray, survival: np.ndarray, n_qubits: int = 1
) -> dict[str, float]:
    """Fit a randomized-benchmarking decay.

    Fits ``p(m) = A·r^m + B`` and converts the depolarising parameter to an
    average gate fidelity ``F = 1 - (1-r)(d-1)/d`` with ``d = 2^n``, per Magesan
    et al., PRA 85, 042311 (2012) (arXiv:1109.6887).

    Only ``r`` is bounded. It is the one parameter with a physical range, and
    the one the fidelity comes from; ``A`` and ``B`` are the readout's scale and
    offset, in whatever units the acquisition arrived in. Bounding those assumes
    a normalisation nothing guarantees, and gets the answer wrong rather than
    refusing it: a decay that has not reached its asymptote by the deepest
    sequence has an ``A`` far larger than the range actually observed, so a
    bounded ``A`` is met by pulling ``r`` down instead. That pins the reported
    fidelity near 0.98 for every chip better than about 3% error per Clifford —
    which is every chip worth benchmarking, and is below the default drift
    threshold, so the drift check would recalibrate forever.

    Returns ``{'fidelity', 'error_per_gate', 'decay_rate'}``.
    """
    x, y = align(depths, survival, what="RB decay")
    floor = 1.0 / (2**n_qubits)

    # The asymptote is not fitted. A depolarised n-qubit state survives with probability
    # ``1/2^n``, and `rb` normalises against measured ``|0>`` and ``X|0>`` references, so
    # that number is where this curve ends — by construction, not by assumption.
    #
    # Pinning it is what makes the rate measurable at all. With `b` free the model is
    # degenerate below one bend: only the product ``a*(1-r)`` sets the slope, so the fit
    # slides along that direction until a bound stops it, and the bound then decides the
    # answer. On the 2026-08-16 B chip every bound tried pinned — 100x the span, 10x, 3x,
    # 1.5x — reporting 3.6e-05 through 3.4e-03 per Clifford as it went, with `b` running to
    # -20.3 at the loosest. A survival does not decay to minus twenty.
    #
    # Fixed, the same data gives 2.19e-03 per Clifford with ``a = 0.443``, so survival at
    # zero depth is 0.94 — which is where a readout of 0.87 fidelity puts it. The physics
    # then agrees with itself: `allxy_check` measured 6.6e-02 per gate on that run and RB
    # 1.1e-03, and the gap is what RB is *for* — random sequences average a coherent
    # miscalibration into the depolarising rate, so the two measure different errors.
    #
    # This became available only when `rb` stopped min-max normalising its survival. Under
    # that scheme the asymptote was wherever the sweep's extremes happened to fall.
    def rb_model(m, a, r):
        return a * np.power(r, m) + floor

    span = float(np.max(y) - np.min(y)) or 1.0
    reach = MAX_AMPLITUDE_REACH * span
    last_error: Exception | None = None
    for r_guess in (0.99, 0.9, 0.999):
        try:
            popt, _ = curve_fit(
                rb_model,
                x,
                y,
                p0=[float(y[0]) - floor or 0.5, r_guess],
                bounds=([-reach, 0.0], [reach, 1.0]),
                maxfev=20000,
            )
            break
        except Exception as exc:  # noqa: BLE001 - reported below if all seeds fail
            last_error = exc
    else:
        raise FitError(f"could not fit RB decay: {last_error}")

    decay = float(popt[1])
    if not 0.0 < decay <= 1.0:
        raise FitError(f"RB decay parameter {decay:.6g} is outside (0, 1]")

    # Reported, never refused. `rb` writes nothing — it exists to say what the gates do,
    # and "0.9% per Clifford, poorly constrained" is that answer. Withholding it because the
    # error bar is wide is withholding the measurement, and there is no downstream parameter
    # it could corrupt: a calibration is a picture of the chip, not a verdict on it.
    #
    # Two things make a rate unconstrained and they are separate. The curve may be lost in
    # scatter, which more circuits per depth fixes. Or `a` and `r` may be unidentifiable —
    # below one bend only their product sets the slope, so the fit slides along the
    # degeneracy until the amplitude bound stops it. The 2026-08-16 B chip did the second at
    # every bound tried: 100x span, 20x, 5x and 3x each pinned, the answer moving from
    # 3.7e-05 to 1.4e-03 per Clifford while the residual went 0.0123 to 0.0131. There is no
    # minimum there to find, and any number a tighter bound produced would be an artefact of
    # the bound.
    residual = float(np.sqrt(np.mean((y - rb_model(x, *popt)) ** 2)))
    pinned = abs(float(popt[0])) >= reach * (1.0 - 1e-6)
    # Against the span pure noise fakes over this many points, not a flat multiple of the
    # scatter: a seven-point sweep of noise spans about six times its own residual, so a
    # bare 3x lets it through — which is how two of the three dead-readout runs on record
    # still came back looking resolved.
    ratio = float(np.ptp(y)) / residual if residual > 0 else float("inf")
    unresolved = pinned or ratio < NOISE_FAKEABLE_SPAN / math.sqrt(max(x.size, 1))
    if unresolved:
        log.warning(
            "RB decay is not constrained by these depths: amplitude %.4g against a survival "
            "spanning %.3g, scatter %.4g. The %.3g per Clifford reported is the fit's best "
            "guess along a direction the data does not pin down, and its error bar spans "
            "orders. Average more circuits per depth, or extend the depths until the "
            "deepest sequence has visibly decayed",
            float(popt[0]),
            span,
            residual,
            1.0 - decay,
        )

    dimension = 2**n_qubits
    error_per_gate = (1.0 - decay) * (dimension - 1) / dimension
    fidelity = 1.0 - error_per_gate

    require_in_range(fidelity, 0.0, 1.0, what="RB fidelity")
    return {
        "fidelity": fidelity,
        "error_per_gate": error_per_gate,
        "decay_rate": decay,
        # How much of the decay the deepest sequence actually saw. `r` is fitted from this
        # much of the curve and extrapolated from the rest, so it says how far the reported
        # fidelity is a measurement — a time constant is normally quoted from at least the
        # 1/e a decay reaches at ``m = 1/(1-r)``.
        #
        # Reported rather than bounded, because no bound here separates the cases yet. The
        # simulated chip sees 0.29 to 0.31 at its deepest 64 and the August 2026 B chip saw
        # 0.176, only 1.6x apart — and that chip's 0.0015 per gate was below the 0.0025 its
        # own 22.9 us T1 allows a 56 ns gate, against an `allxy_check` reading 34x higher.
        # A threshold between two numbers that close would refuse healthy chips.
        "decay_observed": 1.0 - decay ** float(np.max(x)),
        # 1 when the depths did not pin the rate down — see the warning above. A number
        # beside the fidelity rather than in place of it, so a report can show both.
        "unresolved": float(unresolved),
        # Log x: RB depths double, and linearly the decay hugs the axis.
        "fit": fit_summary(
            x,
            y,
            rb_model(x, *popt),
            x_label="sequence length",
            y_label="survival",
            x_scale="log",
        ),
    }
