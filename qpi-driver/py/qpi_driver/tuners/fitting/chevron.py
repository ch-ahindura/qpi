"""Two-qubit fits: the CZ chevron, and the conditional-phase sweep."""

import logging

import numpy as np

from .core import FitError, align, require_in_range

log = logging.getLogger(__name__)


#: Peak-to-peak swing an amplitude row must show before it counts as having
#: resolved the avoided crossing. Below this the sweep stepped over it — see
#: :func:`fit_chevron`.
MIN_CHEVRON_CONTRAST = 0.15


def fit_chevron(
    amplitudes: np.ndarray, durations: np.ndarray, signal: np.ndarray
) -> dict[str, float]:
    """Locate the CZ operating point on a flux-amplitude × duration chevron.

    *signal* is the control qubit's population after preparing ``|11⟩`` and
    playing the flux pulse, flattened row-major over ``(amplitude, duration)``.

    **The operating point is not the brightest pixel.** On resonance population
    leaves ``|11⟩`` for ``|02⟩`` and comes back, and the CZ is the *full return*
    — where the control is excited again, having picked up its phase. But the
    control also reads ≈1 everywhere the flux pulse did nothing at all, so the
    maximum of this surface is as likely to sit on an off-resonant row as on the
    gate. What distinguishes resonance is not level but *motion*: only near the
    crossing does the population swing at all.

    So the amplitude is found from the swing, and only then the duration from
    the returning edge of that row:

    1. the resonant amplitude is the row of greatest peak-to-peak contrast,
       refined across its neighbours;
    2. along it, the CZ duration is the first return to the top after the first
       trough — one full ``|11⟩ → |02⟩ → |11⟩`` round trip.

    Neither step assumes the coupler's analytic form, which is what the original
    centre-of-mass approach was reaching for and is worth keeping.

    Returns ``{'cz_amplitude', 'cz_duration', 'contrast', 'transfer'}``.

    Raises:
        FitError: if no row swings by :data:`MIN_CHEVRON_CONTRAST`. An avoided
            crossing is only a few MHz wide, and a flux sweep coarse enough to
            step over it produces a surface that is flat to within its noise —
            from which a peak-finder would still return a confident, invented
            answer. Refusing is the same guard qubit spectroscopy applies to a
            line narrower than its own step size.
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

    contrasts = np.nanmax(grid, axis=1) - np.nanmin(grid, axis=1)
    row = int(np.nanargmax(contrasts))
    contrast = float(contrasts[row])
    if contrast < MIN_CHEVRON_CONTRAST:
        raise FitError(
            f"no flux amplitude drove a |11>-|02> exchange: the largest population "
            f"swing over the sweep is {contrast:.3f}, below {MIN_CHEVRON_CONTRAST}. "
            "The avoided crossing is only a few MHz wide, so a coarse amplitude "
            "sweep steps over it entirely — narrow the range around the "
            "flux-spectroscopy estimate, or add points."
        )

    amplitude = _refine_amplitude(contrasts, amps, row)
    duration = _return_duration(grid[row, :], durs)

    require_in_range(
        amplitude, float(np.min(amps)), float(np.max(amps)), what="CZ amplitude"
    )
    require_in_range(
        duration, float(np.min(durs)), float(np.max(durs)), what="CZ duration"
    )
    return {
        "cz_amplitude": amplitude,
        "cz_duration": duration,
        "contrast": contrast,
        "transfer": float(np.nanmax(grid[row, :]) - np.nanmin(grid[row, :])),
    }


def _refine_amplitude(contrasts: np.ndarray, amps: np.ndarray, row: int) -> float:
    """Sub-grid resonance position, as a centre of mass of the contrast profile.

    Contrast peaks at the crossing and falls away either side, so its centroid
    over the best row and its neighbours locates the resonance between grid
    points — which matters, because the grid step is normally far wider than the
    crossing.
    """
    low, high = max(row - 1, 0), min(row + 2, len(amps))
    floor = float(np.nanmin(contrasts))
    weights = contrasts[low:high] - floor
    total = float(np.sum(weights))
    if total <= 0:
        return float(amps[row])
    return float(np.sum(weights * amps[low:high]) / total)


#: Fractions of a resonant row's full swing that count as having left ``|11⟩``
#: and as having come back to it. Deliberately far apart: what lies between them
#: is the exchange in flight, and anything that only wanders around inside the
#: band has not completed either leg.
_DEPARTED = 0.25
_RETURNED = 0.75


def _no_exchange_level(row: np.ndarray) -> float:
    """The level this row reads when the pair is still ``|11>``.

    Of the row's two extremes, the one nearer its *shortest* duration. At the start of
    the sweep the least exchange has happened, so the row is on the ``|11>`` side of
    its own oscillation — and that is true whichever direction the readout's magnitude
    moves, which is the point.

    Read from the resonant row rather than from an off-resonant one, even though an
    off-resonant row is the more obvious reference. A chevron sweep is deliberately
    narrow — the avoided crossing is a few MHz wide, so a sweep wide enough to contain
    a row with no exchange is too coarse to resolve the crossing at all. Every row in a
    useful sweep is partly resonant, and taking the quietest one's median put the
    reference part-way up the swing and the duration 10% short.

    Assumes the shortest duration is less than half a round trip, which is implied by
    wanting to observe the round trip at all.
    """
    finite = row[np.isfinite(row)]
    low, high = float(np.nanmin(finite)), float(np.nanmax(finite))
    first = float(finite[0])
    return low if abs(first - low) <= abs(first - high) else high


def _return_duration(row: np.ndarray, durs: np.ndarray) -> float:
    """The first full ``|11⟩ → |02⟩ → |11⟩`` round trip along a resonant row.

    Measured as *distance from* the no-exchange level — see `_no_exchange_level`.
    Population leaves ``|11⟩`` and comes back, so that distance rises from nothing,
    peaks at full transfer, and returns to nothing, and the CZ is where it returns.

    **Reference rather than direction, because the direction is not knowable here.**
    Whether the control's ``|z|`` rises or falls as it empties depends on which side of
    the resonator's line the readout sits, and `resonator_spectroscopy` puts it on the
    ground-state resonance, where an excited qubit reflects *less*. An earlier version
    hunted a maximum and, once the readout was modelled dispersively, found the wrong
    extreme: 55 ns, half the round trip, which is a complete population swap — a
    perfectly good gate and the wrong one. Anchoring on a measured no-exchange level
    removes the assumption rather than flipping it.

    By level, not by turning point, for a second reason. Walking to the first local
    turn is the obvious reading of "there and back" and is also wrong: the exchange
    beats against the other transitions the flux pulse sits near, so the far side of
    the round trip carries a wiggle a few per cent of the swing and a turning-point
    walk stops on it. The population has to actually cross back before anything counts
    as a return.

    The *first* return, not the strongest: on resonance the exchange rings for as long
    as the sweep runs, and a later round trip is a working gate only if nothing
    decohered in the meantime.
    """
    if not np.any(np.isfinite(row)):
        raise FitError("the resonant chevron row contains no finite data")

    away = np.abs(np.asarray(row, dtype=float) - _no_exchange_level(row))
    span = float(np.nanmax(away))
    if span <= 0:
        raise FitError(
            "the resonant chevron row never departs from the no-exchange level, so "
            "no exchange completed and there is no CZ to calibrate"
        )

    departed = _crossing(away, _RETURNED * span, above=True)
    if departed is None:
        raise FitError(
            "the chevron's population never leaves |11> within the duration "
            "sweep — no exchange completed, so there is no CZ to calibrate"
        )

    returned = _crossing(away, _DEPARTED * span, above=False, start=departed)
    if returned is None:
        raise FitError(
            "the chevron's population never recovers after its trough — the "
            "duration sweep ends before the exchange completes"
        )

    # On to where the return actually closes: the minimum distance from the
    # no-exchange level, which is the round trip completing.
    settled = returned
    while settled + 1 < away.size and float(away[settled + 1]) < float(away[settled]):
        settled += 1
    return _parabolic_vertex(away, durs, settled)


def _crossing(
    values: np.ndarray, level: float, *, above: bool, start: int = 0
) -> int | None:
    """First index at or after *start* on the far side of *level*."""
    for index in range(start, len(values)):
        value = float(values[index])
        crossed = value > level if above else value < level
        if crossed:
            return index
    return None


def _parabolic_vertex(row: np.ndarray, durs: np.ndarray, index: int) -> float:
    """Turning point of the parabola through *index* and its two neighbours."""
    if index <= 0 or index >= len(row) - 1:
        return float(durs[index])
    before, at, after = float(row[index - 1]), float(row[index]), float(row[index + 1])
    denominator = before - 2.0 * at + after
    if denominator == 0:
        return float(durs[index])
    shift = 0.5 * (before - after) / denominator
    shift = float(np.clip(shift, -1.0, 1.0))
    step = float(durs[index + 1] - durs[index])
    return float(durs[index] + shift * step)


def fit_conditional_phase(
    phases: np.ndarray, ground: np.ndarray, excited: np.ndarray
) -> dict[str, float]:
    """Find the phase correction that brings the conditional phase to 180°.

    The control's Ramsey fringe is swept against the phase of its second π/2,
    once with the target in ``|0⟩`` and once in ``|1⟩``. The conditional phase is
    the **offset between the two fringes**, and this fits each one for its own
    phase and subtracts.

    **Why not the difference of the two.** Differencing them and taking a zero
    crossing looks equivalent and is not. Writing the fringes as
    ``A + B·cos(φ − φ₀)`` and ``A + B·cos(φ − φ₀ − φ_cz)``, their difference is
    ``−2B·sin(φ_cz/2)·sin(φ − φ₀ − φ_cz/2)``, whose crossings sit at
    ``φ₀ + φ_cz/2`` — half the wanted angle, and displaced by ``φ₀``. That ``φ₀``
    is the control's own dynamical phase from being detuned throughout the flux
    pulse: hundreds of radians, and nothing to do with the coupling. It is common
    to both fringes and cancels in their offset, which is the entire reason the
    experiment measures two of them.

    Everything is in **degrees**, because that is what the routine sweeps and
    what ``Rxy(phi=...)`` takes on both schedulers. A CZ is correct at 180°, so
    the correction is ``180 − conditional_phase``, wrapped to ``(−180, 180]``.

    Returns ``{'conditional_phase', 'phase_correction', 'contrast'}``.
    """
    x, low = align(phases, ground, what="conditional phase (target |0>)")
    _x, high = align(phases, excited, what="conditional phase (target |1>)")

    low_phase, low_amplitude, low_residual = _fringe_phase(x, low)
    high_phase, high_amplitude, high_residual = _fringe_phase(x, high)

    contrast = min(low_amplitude, high_amplitude)
    # Against the scatter about the fit rather than against zero: a fringe that
    # did not oscillate still fits some vanishing amplitude out of its own noise,
    # and an angle read off that is arbitrary.
    if contrast <= max(low_residual, high_residual):
        raise FitError(
            f"a conditional-phase fringe is flat — amplitude {contrast:.2e} does "
            f"not exceed the scatter about it. The control's Ramsey did not "
            "oscillate, so no phase can be read from it."
        )

    conditional = (high_phase - low_phase) % 360.0
    correction = (180.0 - conditional + 180.0) % 360.0 - 180.0
    return {
        "conditional_phase": float(conditional),
        "phase_correction": float(correction),
        "contrast": float(contrast),
        # The ground fringe's own phase is the *single-qubit* phase the CZ left
        # on the measured qubit — the thing the edge's virtual-Z corrections
        # exist to cancel, and a different quantity from the conditional phase.
        # Reported rather than discarded because nothing else measures it.
        "reference_phase": float(low_phase),
    }


def _fringe_phase(phases: np.ndarray, signal: np.ndarray) -> tuple[float, float, float]:
    """Phase, amplitude and residual scatter of ``c + B·cos(φ − ψ)``, in degrees.

    A linear least squares over ``[cos φ, sin φ, 1]`` rather than a curve fit:
    the fringe's frequency is known exactly — the routine swept the phase itself,
    so it is one cycle by construction — which makes the model linear in its
    remaining parameters and leaves nothing for an optimiser to get stuck in.

    Least squares rather than a bare projection because the sweep is not the
    clean orthogonal basis a projection assumes. ``linear_setpoints(0, 360, n)``
    includes both endpoints, so 0° and 360° are the same phase measured twice,
    and that one duplicated sample is enough to bias a projection by most of a
    degree. Solving for the coefficients is exact whatever the sampling.
    """
    radians = np.deg2rad(np.asarray(phases, dtype=float))
    values = np.asarray(signal, dtype=float)
    design = np.column_stack([np.cos(radians), np.sin(radians), np.ones_like(radians)])
    coefficients = np.linalg.lstsq(design, values, rcond=None)[0]
    cosine, sine, _offset = coefficients
    phase = float(np.rad2deg(np.arctan2(sine, cosine)) % 360.0)

    # The floor an amplitude must clear to mean anything: the scatter about the
    # fit, or — for a fringe so flat that the scatter is float dust too — a small
    # fraction of the signal's own scale.
    scatter = float(np.std(values - design @ coefficients))
    dust = 1e-9 * float(np.max(np.abs(values)) or 1.0)
    return phase, float(np.hypot(cosine, sine)), max(scatter, dust)
