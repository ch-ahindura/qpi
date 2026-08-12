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
    # A time constant far beyond the window was never observed, only extrapolated.
    require_in_range(value, 0.0, float(np.max(x)) * 10, what=what)
    require_resolved_curve(
        y,
        exponential_decay(x, amplitude, tau, offset),
        what=f"{what} decay",
        consequence=(
            f"the decay was never seen in this window — a {what} read off a curve "
            "the data cannot tell from a flat line is not a coherence time. Lengthen "
            "the delays, or average more shots"
        ),
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

    last_error: Exception | None = None
    for r_guess in (0.99, 0.9, 0.999):
        try:
            popt, _ = curve_fit(
                rb_model,
                x,
                y,
                p0=[float(y[0]) - float(y[-1]) or 0.5, r_guess, float(y[-1])],
                bounds=([-np.inf, 0.0, -np.inf], [np.inf, 1.0, np.inf]),
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
