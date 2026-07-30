"""Two-qubit fits: the CZ chevron, and the conditional-phase sweep."""

import logging

import numpy as np

from .core import FitError, align, require_in_range

log = logging.getLogger(__name__)


def fit_chevron(
    amplitudes: np.ndarray, durations: np.ndarray, signal: np.ndarray
) -> dict[str, float]:
    """Locate the CZ operating point on a flux-amplitude × duration chevron.

    A CZ is calibrated at the amplitude and duration that complete one full
    ``|11⟩ ↔ |02⟩`` exchange, which is the point of maximum population transfer
    on the chevron. The fit is a centre-of-mass around the strongest pixel
    rather than a curve fit: the chevron's analytic form depends on the coupler
    model, and its maximum does not.

    *signal* is the measured population, flattened in row-major order over
    ``(amplitude, duration)``.

    Returns ``{'cz_amplitude', 'cz_duration', 'transfer'}``.
    """
    amps = np.asarray(amplitudes, dtype=float).reshape(-1)
    durs = np.asarray(durations, dtype=float).reshape(-1)
    values = np.asarray(signal, dtype=float).reshape(-1)

    expected = amps.size * durs.size
    if values.size < expected:
        raise FitError(
            f"chevron needs {expected} points for a {amps.size}×{durs.size} grid, "
            f"got {values.size}"
        )

    grid = values[:expected].reshape(amps.size, durs.size)
    if not np.any(np.isfinite(grid)):
        raise FitError("chevron acquisition contains no finite data")

    peak = np.unravel_index(int(np.nanargmax(grid)), grid.shape)
    amplitude, duration = _refine_peak(grid, amps, durs, peak)

    require_in_range(
        amplitude, float(np.min(amps)), float(np.max(amps)), what="CZ amplitude"
    )
    require_in_range(
        duration, float(np.min(durs)), float(np.max(durs)), what="CZ duration"
    )
    return {
        "cz_amplitude": amplitude,
        "cz_duration": duration,
        "transfer": float(grid[peak]),
    }


def _refine_peak(
    grid: np.ndarray, amps: np.ndarray, durs: np.ndarray, peak: tuple[int, ...]
) -> tuple[float, float]:
    """Sub-grid peak position by a centre of mass over the peak's neighbours.

    The grid step is usually coarser than the precision a CZ needs, so taking
    the peak pixel alone quantises the answer to the sweep resolution.
    """
    row, col = int(peak[0]), int(peak[1])
    floor = float(np.nanmin(grid))

    def centroid(values: np.ndarray, coords: np.ndarray, index: int) -> float:
        low, high = max(index - 1, 0), min(index + 2, len(coords))
        window = values[low:high] - floor
        axis = coords[low:high]
        total = float(np.sum(window))
        if total <= 0:
            return float(coords[index])
        return float(np.sum(window * axis) / total)

    return centroid(grid[:, col], amps, row), centroid(grid[row, :], durs, col)


def fit_conditional_phase(phases: np.ndarray, signal: np.ndarray) -> dict[str, float]:
    """Find the phase correction that brings the conditional phase to π.

    The control qubit's Ramsey fringe is measured against a swept phase with the
    target in ``|0⟩`` and again in ``|1⟩``; the offset between the two fringes is
    the conditional phase. Here *signal* is the already-differenced fringe, whose
    zero crossing is the correction to apply.

    Returns ``{'conditional_phase', 'phase_correction'}``.
    """
    x, y = align(phases, signal, what="conditional phase")

    centred = y - float(np.mean(y))
    crossings = np.nonzero(np.diff(np.signbit(centred)))[0]
    if crossings.size == 0:
        raise FitError(
            "conditional-phase sweep has no zero crossing — widen the phase range"
        )

    index = int(crossings[0])
    y0, y1 = float(centred[index]), float(centred[index + 1])
    x0, x1 = float(x[index]), float(x[index + 1])
    crossing = x0 if y1 == y0 else x0 - y0 * (x1 - x0) / (y1 - y0)

    crossing = require_in_range(
        crossing, float(np.min(x)), float(np.max(x)), what="conditional phase"
    )
    return {
        "conditional_phase": crossing,
        "phase_correction": float(np.pi - crossing),
    }
