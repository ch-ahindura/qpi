"""Exponential fits: T1, T2 echo, and randomized-benchmarking decay."""

import logging

import numpy as np
from scipy.optimize import curve_fit

from .core import (
    FitError,
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


def fit_t2(delays: np.ndarray, signal: np.ndarray) -> dict[str, float]:
    """Fit a T2 echo curve. Returns ``{'t2', 'amplitude'}``."""
    return _fit_coherence(delays, signal, key="t2", what="T2")


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
#: so 200 leaves four hundred times the room a legitimate unreached asymptote needs, and a
#: fit that still reaches it was stopped rather than found.
MAX_AMPLITUDE_REACH = 200.0


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

    def rb_model(m, a, r, b):
        return a * np.power(r, m) + b

    span = float(np.max(y) - np.min(y)) or 1.0
    reach = MAX_AMPLITUDE_REACH * span
    last_error: Exception | None = None
    for r_guess in (0.99, 0.9, 0.999):
        try:
            popt, _ = curve_fit(
                rb_model,
                x,
                y,
                p0=[float(y[0]) - float(y[-1]) or 0.5, r_guess, float(y[-1])],
                bounds=(
                    [-reach, 0.0, float(np.min(y)) - reach],
                    [reach, 1.0, float(np.max(y)) + reach],
                ),
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

    # The decay has to be deeper than the scatter it was drawn through, or the
    # fidelity is a number read off the noise. Compared as a span rather than by the
    # sign of the amplitude: the `rb` routine rescales its acquisition to [0, 1]
    # without orienting it, so a chip whose readout brightens with excitation returns
    # a rising survival, and that is a readout convention rather than a bad fit.
    require_resolved_curve(
        y,
        rb_model(x, *popt),
        what="RB decay",
        consequence=(
            "there is no decay here to take a fidelity from. Average more circuits "
            "per depth, or extend the depths until it is visible above the noise"
        ),
        # Escalatable, and on the averaging axis rather than the reach: what this guard
        # compares is the decay's span against the *scatter* around it, and scatter is
        # what more circuits per depth buys down. Depth is the other half of the same
        # sentence and stays advice, since a chip whose decay is simply too slow is a
        # different problem from one whose points are too noisy to see it.
        axis="circuits_per_depth",
        # The commonest refusal in the graph, and the one whose shape most wants seeing.
        fit=fit_summary(
            x,
            y,
            rb_model(x, *popt),
            x_label="sequence length",
            y_label="survival",
            x_scale="log",
        ),
    )

    # After the noise check, not before: unresolved scatter and a stopped fit both end
    # here, and only one of them is fixed by deeper sequences.
    if abs(float(popt[0])) >= reach * (1.0 - 1e-6):
        raise FitError(
            f"the fitted amplitude reached {popt[0]:.4g}, the widest this fit allows for a "
            f"survival spanning {span:.3g} — so it was stopped there rather than found, "
            f"and the r of {decay:.7g} it trades against is the one that fits a straight "
            f"line, not the one the gates set. There is no resolved decay in these depths. "
            f"Average more circuits per depth, or extend the depths until the deepest "
            f"sequence has visibly decayed",
            fit=fit_summary(
                x,
                y,
                rb_model(x, *popt),
                x_label="sequence length",
                y_label="survival",
                x_scale="log",
            ),
        )

    dimension = 2**n_qubits
    error_per_gate = (1.0 - decay) * (dimension - 1) / dimension
    fidelity = 1.0 - error_per_gate

    require_in_range(fidelity, 0.0, 1.0, what="RB fidelity")
    return {
        "fidelity": fidelity,
        "error_per_gate": error_per_gate,
        "decay_rate": decay,
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
