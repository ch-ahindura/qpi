"""Exponential fitting utilities."""

import logging

import numpy as np
from scipy.optimize import curve_fit

logger = logging.getLogger(__name__)


def exponential_decay(
    t: np.ndarray | float, A: float, tau: float, offset: float
) -> np.ndarray | float:
    """A * exp(-t/tau) + offset"""
    return A * np.exp(-t / tau) + offset


def fit_t1(delays: np.ndarray, signal: np.ndarray) -> dict[str, float]:
    """Fit exponential decay to T1 data.
    Returns: {'t1': float, 'amplitude': float}"""
    try:
        offset_guess = float(signal[-1]) if len(signal) > 0 else 0.0
        A_guess = float(signal[0]) - offset_guess if len(signal) > 0 else 1.0
        tau_guess = float(delays[-1]) / 3.0 if len(delays) > 0 else 1.0

        p0 = [A_guess, tau_guess, offset_guess]
        popt, _ = curve_fit(exponential_decay, delays, signal, p0=p0)

        return {"t1": float(popt[1]), "amplitude": float(popt[0])}
    except Exception as e:
        logger.error(f"Failed to fit T1 data: {e}")
        return {"t1": 0.0, "amplitude": 0.0}


def fit_t2(delays: np.ndarray, signal: np.ndarray) -> dict[str, float]:
    """Fit exponential decay to T2 echo data.
    Returns: {'t2': float, 'amplitude': float}"""
    try:
        offset_guess = float(signal[-1]) if len(signal) > 0 else 0.0
        A_guess = float(signal[0]) - offset_guess if len(signal) > 0 else 1.0
        tau_guess = float(delays[-1]) / 3.0 if len(delays) > 0 else 1.0

        p0 = [A_guess, tau_guess, offset_guess]
        popt, _ = curve_fit(exponential_decay, delays, signal, p0=p0)

        return {"t2": float(popt[1]), "amplitude": float(popt[0])}
    except Exception as e:
        logger.error(f"Failed to fit T2 data: {e}")
        return {"t2": 0.0, "amplitude": 0.0}


def _rb_decay(m: np.ndarray, A: float, r: float, B: float) -> np.ndarray:
    return A * (r**m) + B


def fit_rb_decay(depths: np.ndarray, survival: np.ndarray) -> dict[str, float]:
    """Fit A * r^m + B to RB survival probability.
    Returns: {'depolarizing_param': float, 'fidelity': float, 'error_per_gate': float}
    fidelity = 1 - (1-r)(d-1)/d where d=2 for single qubit"""
    try:
        B_guess = float(survival[-1]) if len(survival) > 0 else 0.5
        A_guess = float(survival[0]) - B_guess if len(survival) > 0 else 0.5
        r_guess = 0.9

        p0 = [A_guess, r_guess, B_guess]
        # bounds to ensure 0 <= r <= 1
        popt, _ = curve_fit(
            _rb_decay,
            depths,
            survival,
            p0=p0,
            bounds=([-np.inf, 0, -np.inf], [np.inf, 1, np.inf]),
        )

        r_fit = float(popt[1])
        d = 2  # dimension for single qubit
        fidelity = 1 - (1 - r_fit) * (d - 1) / d
        epg = 1 - fidelity

        return {
            "depolarizing_param": r_fit,
            "fidelity": fidelity,
            "error_per_gate": epg,
        }
    except Exception as e:
        logger.error(f"Failed to fit RB data: {e}")
        return {"depolarizing_param": 0.0, "fidelity": 0.0, "error_per_gate": 1.0}
