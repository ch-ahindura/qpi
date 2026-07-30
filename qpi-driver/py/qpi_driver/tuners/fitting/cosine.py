"""Cosine fitting utilities."""

import logging

import numpy as np
from scipy.optimize import curve_fit

logger = logging.getLogger(__name__)


def decaying_cosine(
    t: np.ndarray | float,
    A: float,
    freq: float,
    phase: float,
    tau: float,
    offset: float,
) -> np.ndarray | float:
    """A * cos(2π * freq * t + phase) * exp(-t/tau) + offset"""
    return A * np.cos(2 * np.pi * freq * t + phase) * np.exp(-t / tau) + offset


def fit_rabi(amplitudes: np.ndarray, signal: np.ndarray) -> dict[str, float]:
    """Fit cosine to Rabi oscillation data.
    Returns: {'amp180': float, 'rabi_frequency': float}"""
    try:
        # Initial guesses
        A_guess = float(np.max(signal) - np.min(signal)) / 2
        offset_guess = float(np.mean(signal))
        # Estimate frequency using FFT
        N = len(amplitudes)
        dt = amplitudes[1] - amplitudes[0] if N > 1 else 1.0
        yf = np.fft.fft(signal - offset_guess)
        xf = np.fft.fftfreq(N, dt)
        idx_max = np.argmax(np.abs(yf[1 : N // 2])) + 1 if N > 2 else 0
        freq_guess = (
            float(np.abs(xf[idx_max]))
            if idx_max > 0
            else 1.0 / (np.max(amplitudes) - np.min(amplitudes) or 1.0)
        )
        phase_guess = 0.0
        tau_guess = float(np.max(amplitudes) - np.min(amplitudes)) * 10  # Slow decay

        p0 = [A_guess, freq_guess, phase_guess, tau_guess, offset_guess]
        popt, _ = curve_fit(decaying_cosine, amplitudes, signal, p0=p0)

        rabi_freq = abs(float(popt[1]))
        amp180 = 1.0 / (2 * rabi_freq) if rabi_freq > 0 else 0.0

        return {"amp180": amp180, "rabi_frequency": rabi_freq}
    except Exception as e:
        logger.error(f"Failed to fit Rabi oscillations: {e}")
        return {"amp180": 0.0, "rabi_frequency": 0.0}


def fit_ramsey(
    delays: np.ndarray, signal: np.ndarray, artificial_detuning: float = 0.0
) -> dict[str, float]:
    """Fit decaying cosine to Ramsey fringe.
    Returns: {'detuning': float, 't2_star': float}"""
    try:
        # Initial guesses
        A_guess = float(np.max(signal) - np.min(signal)) / 2
        offset_guess = float(np.mean(signal))

        N = len(delays)
        dt = delays[1] - delays[0] if N > 1 else 1.0
        yf = np.fft.fft(signal - offset_guess)
        xf = np.fft.fftfreq(N, dt)
        idx_max = np.argmax(np.abs(yf[1 : N // 2])) + 1 if N > 2 else 0
        freq_guess = float(np.abs(xf[idx_max])) if idx_max > 0 else artificial_detuning

        phase_guess = 0.0
        tau_guess = float(np.max(delays)) / 2.0

        p0 = [A_guess, freq_guess, phase_guess, tau_guess, offset_guess]
        popt, _ = curve_fit(decaying_cosine, delays, signal, p0=p0)

        freq_fit = abs(float(popt[1]))
        detuning = freq_fit - artificial_detuning

        return {"detuning": detuning, "t2_star": abs(float(popt[3]))}
    except Exception as e:
        logger.error(f"Failed to fit Ramsey fringe: {e}")
        return {"detuning": 0.0, "t2_star": 0.0}
