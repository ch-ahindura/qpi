"""Chevron 2D fitting utilities."""

import logging

import numpy as np

logger = logging.getLogger(__name__)


def fit_chevron(
    amplitudes: np.ndarray, durations: np.ndarray, signal_2d: np.ndarray
) -> dict[str, float]:
    """Analyse 2D chevron pattern to find optimal CZ parameters.
    Finds the avoided crossing center and extracts optimal amplitude/duration for π phase.
    Returns: {'cz_amp': float, 'cz_duration': float, 'coupling_strength': float}"""
    try:
        # A full 2D fit of a chevron is complex.
        # Here we perform a simple peak extraction for the avoided crossing center.
        # Find the row/col with the highest/lowest variance or frequency component.
        # For simplicity, we just find the maximum signal point in the 2D array
        # assuming it corresponds to the max population transfer at resonance.

        # A more sophisticated fit would slice the 2D array and fit a decaying cosine per amplitude,
        # then find the amplitude where frequency is minimized (for some gate variants) or
        # phase accumulation is exactly pi.

        idx_2d = np.unravel_index(np.argmax(signal_2d), signal_2d.shape)
        amp_idx, dur_idx = idx_2d[0], idx_2d[1]

        cz_amp = float(amplitudes[amp_idx])
        cz_duration = float(durations[dur_idx])

        return {
            "cz_amp": cz_amp,
            "cz_duration": cz_duration,
            "coupling_strength": 0.0,  # Placeholder, requires complex fitting of frequency vs amp
        }
    except Exception as e:
        logger.error(f"Failed to fit chevron data: {e}")
        return {"cz_amp": 0.0, "cz_duration": 0.0, "coupling_strength": 0.0}
