"""Lorentzian fitting utilities."""

import logging

import numpy as np
from scipy.optimize import curve_fit

logger = logging.getLogger(__name__)


def lorentzian(
    f: np.ndarray | float, f0: float, gamma: float, A: float, offset: float
) -> np.ndarray | float:
    """Lorentzian lineshape: A * (gamma/2)^2 / ((f - f0)^2 + (gamma/2)^2) + offset"""
    return A * (gamma / 2) ** 2 / ((f - f0) ** 2 + (gamma / 2) ** 2) + offset


def fit_resonator_spectroscopy(
    frequencies: np.ndarray, amplitudes: np.ndarray
) -> dict[str, float]:
    """Fit Lorentzian to find resonator frequency.
    Returns: {'f_r': float, 'linewidth': float, 'amplitude': float}"""
    try:
        # Initial guesses
        A_guess = float(np.max(amplitudes) - np.min(amplitudes))
        offset_guess = float(np.min(amplitudes))
        idx_max = np.argmax(amplitudes)
        f0_guess = float(frequencies[idx_max])
        gamma_guess = float(np.max(frequencies) - np.min(frequencies)) / 10.0

        p0 = [f0_guess, gamma_guess, A_guess, offset_guess]
        popt, _ = curve_fit(lorentzian, frequencies, amplitudes, p0=p0)

        return {
            "f_r": float(popt[0]),
            "linewidth": float(abs(popt[1])),
            "amplitude": float(popt[2]),
        }
    except Exception as e:
        logger.error(f"Failed to fit resonator spectroscopy: {e}")
        return {
            "f_r": float(frequencies[np.argmax(amplitudes)]),
            "linewidth": 0.0,
            "amplitude": 0.0,
        }


def fit_qubit_spectroscopy(
    frequencies: np.ndarray, amplitudes: np.ndarray
) -> dict[str, float]:
    """Fit inverted Lorentzian (dip) to find qubit frequency.
    Returns: {'f01': float, 'linewidth': float}"""
    try:
        # Initial guesses for a dip
        A_guess = float(np.min(amplitudes) - np.max(amplitudes))  # Negative amplitude
        offset_guess = float(np.max(amplitudes))
        idx_min = np.argmin(amplitudes)
        f0_guess = float(frequencies[idx_min])
        gamma_guess = float(np.max(frequencies) - np.min(frequencies)) / 10.0

        p0 = [f0_guess, gamma_guess, A_guess, offset_guess]
        popt, _ = curve_fit(lorentzian, frequencies, amplitudes, p0=p0)

        return {"f01": float(popt[0]), "linewidth": float(abs(popt[1]))}
    except Exception as e:
        logger.error(f"Failed to fit qubit spectroscopy: {e}")
        return {"f01": float(frequencies[np.argmin(amplitudes)]), "linewidth": 0.0}
