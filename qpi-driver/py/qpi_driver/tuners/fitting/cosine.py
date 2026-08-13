"""Oscillatory fits: Rabi, Ramsey, DRAG and fine-amplitude."""

import logging

import numpy as np
from scipy.optimize import curve_fit

from .core import (
    FitError,
    align,
    estimate_frequency,
    fit_summary,
    OutOfRange,
    require_in_range,
    require_positive,
    require_resolved_curve,
)

log = logging.getLogger(__name__)


def decaying_cosine(
    t: np.ndarray | float,
    amplitude: float,
    freq: float,
    phase: float,
    tau: float,
    offset: float,
) -> np.ndarray | float:
    """``A·cos(2π·f·t + φ)·exp(-t/τ) + c``."""
    return amplitude * np.cos(2 * np.pi * freq * t + phase) * np.exp(-t / tau) + offset


def _fit_decaying_cosine(
    x: np.ndarray, y: np.ndarray, *, what: str, decays: bool
) -> tuple[float, ...]:
    """Fit :func:`decaying_cosine`, returning the optimal parameters.

    *decays* says whether the envelope is expected to decay over the window. A
    Rabi sweep over pulse amplitude does not — the decay term is there only to
    absorb drift — so its τ is seeded long and left loose.
    """
    span = float(x[-1] - x[0]) or 1.0
    amplitude_guess = (float(np.max(y)) - float(np.min(y))) / 2 or 1.0
    offset_guess = float(np.mean(y))
    freq_guess = estimate_frequency(x, y)
    tau_guess = span / 2.0 if decays else span * 100.0

    last_error: Exception | None = None
    for phase_guess in (0.0, np.pi / 2, np.pi):
        try:
            popt, _ = curve_fit(
                decaying_cosine,
                x,
                y,
                p0=[amplitude_guess, freq_guess, phase_guess, tau_guess, offset_guess],
                maxfev=20000,
            )
            return tuple(float(v) for v in popt)
        except Exception as exc:  # noqa: BLE001 - reported below if all seeds fail
            last_error = exc

    raise FitError(f"could not fit {what}: {last_error}")


def fit_rabi(amplitudes: np.ndarray, signal: np.ndarray) -> dict[str, float]:
    """Fit a Rabi amplitude sweep.

    The π-pulse amplitude is half the oscillation period: a full period drives
    the qubit through ``|0⟩ → |1⟩ → |0⟩``.

    Returns ``{'amp180', 'rabi_frequency', 'contrast'}``.

    Raises:
        FitError: if the fit fails, or ``amp180`` lands outside the swept range.
    """
    x, y = align(amplitudes, signal, what="Rabi")
    amplitude, freq, phase, tau, offset = _fit_decaying_cosine(
        x, y, what="Rabi oscillation", decays=False
    )

    rabi_frequency = require_positive(abs(freq), what="Rabi frequency")
    amp180 = 1.0 / (2.0 * rabi_frequency)

    high = float(np.max(x))
    if amp180 > high * 1.1:
        # Above the sweep, which is the one direction `require_in_range` cannot usefully
        # report: it fires the same way for a value too small and one that is missing
        # because the pi pulse is off the top, and only the second is fixable by sweeping
        # differently. Escalatable, so a routine can reach further rather than an operator
        # reading prose — a chip whose working amp180 was 0.5683 against a sweep stopping
        # at 0.5 returned a flat Rabi every run.
        raise OutOfRange(
            f"amp180 fitted to {amp180:.4g}, above the {high:.4g} this sweep reached — "
            "the pi pulse is past the top of the range, so there is more amplitude to try",
            axis="amplitudes",
            direction="wider",
        )
    require_in_range(
        amp180,
        float(np.min(x)),
        high,
        what="amp180",
        tolerance=0.1,
    )

    require_resolved_curve(
        y,
        decaying_cosine(x, amplitude, freq, phase, tau, offset),
        what="Rabi oscillation",
        consequence=(
            "there is no pi amplitude to take from it. The readout is not resolving "
            "the qubit, and writing this would leave every later X pulse driving "
            "nothing"
        ),
    )

    return {
        "amp180": amp180,
        "rabi_frequency": rabi_frequency,
        "contrast": abs(amplitude) * 2,
        "fit": fit_summary(
            x,
            y,
            decaying_cosine(x, amplitude, freq, phase, tau, offset),
            x_label="pulse amplitude",
            y_label="signal",
        ),
    }


def fit_ramsey(
    delays: np.ndarray, signal: np.ndarray, artificial_detuning: float = 0.0
) -> dict[str, float]:
    """Fit a Ramsey fringe.

    The fringe oscillates at ``|Δ + δ|`` where ``δ`` is the deliberate
    artificial detuning; the qubit's true detuning is the fitted frequency less
    that. The sign is not recoverable from a magnitude fit, which is exactly why
    an artificial detuning is applied — it biases the fringe away from zero so
    the correction has a known direction.

    Returns ``{'detuning', 't2_star', 'fringe_frequency'}``.
    """
    x, y = align(delays, signal, what="Ramsey")
    amplitude, freq, phase, tau, offset = _fit_decaying_cosine(
        x, y, what="Ramsey fringe", decays=True
    )

    fringe = require_positive(abs(freq), what="Ramsey fringe frequency")
    t2_star = require_positive(abs(tau), what="T2*")
    window = float(np.max(x))
    if t2_star > window * 10:
        # Two different failures reach here, and they want opposite sweeps.
        #
        # A T2* modestly past the window is a decay the window was too short to contain,
        # and wants a longer one. A T2* *absurdly* past it is an aliased fringe: the step
        # was coarser than half the fringe period, so the cosine fitted a slow beat that
        # is not there, and its envelope came out flat. The August 2026 acceptance test hit
        # this at 1361 seconds against a 100 us window — seven orders of magnitude, which
        # no window length explains.
        #
        # A hundred windows is well clear of either: a genuine too-short sweep overruns by
        # single digits, and an alias by orders of magnitude.
        raise OutOfRange(
            f"T2* fitted to {t2_star:.4g} s against a {window:.4g} s window — "
            + (
                "far enough past it that the fringe was aliased rather than merely "
                "unfinished, so the delays need sampling more finely"
                if t2_star > window * 100
                else "the decay did not finish inside it, so the delays need to reach "
                "further"
            ),
            axis="delays",
            direction="finer" if t2_star > window * 100 else "wider",
        )
    require_resolved_curve(
        y,
        decaying_cosine(x, amplitude, freq, phase, tau, offset),
        what="Ramsey fringe",
        consequence=(
            "there is no detuning to take from it, and writing one would move f01 by "
            "a number read off the noise. Average more shots, or check that the pi/2 "
            "pulses are reaching the qubit at all"
        ),
        # Escalatable, and *finer* rather than wider — which is the opposite of what a
        # plain decay wants, because the model is different. A flat exponential means the
        # window ended before the decay did. A flat *oscillation* usually means the step
        # was too coarse to show it: the fringe is folded down to something slow, the
        # cosine fits a beat that is not there, and the envelope comes out level. Asking
        # for a longer window there makes it worse, since the step grows with it.
        axis="delays",
        direction="finer",
    )
    return {
        "detuning": fringe - artificial_detuning,
        "t2_star": t2_star,
        "fringe_frequency": fringe,
        "fit": fit_summary(
            x,
            y,
            decaying_cosine(x, amplitude, freq, phase, tau, offset),
            x_label="delay (s)",
            y_label="signal",
        ),
    }


def fit_drag(
    betas: np.ndarray, signal: np.ndarray, *, axis: str | None = None
) -> dict[str, float]:
    """Fit a DRAG (Motzoi) sweep.

    The standard sequence gives a signal linear in β near the optimum, crossing
    zero where phase error vanishes, so the fit is a straight line and the
    answer is its root.

    Returns ``{'motzoi', 'slope'}``.
    """
    x, y = align(betas, signal, what="DRAG")
    slope, intercept = np.polyfit(x, y, 1)
    if abs(slope) < 1e-12:
        raise FitError("DRAG sweep is flat in beta — no optimum to find")

    motzoi = float(-intercept / slope)
    require_in_range(
        motzoi,
        float(np.min(x)),
        float(np.max(x)),
        what="motzoi",
        tolerance=0.1,
        axis=axis,
    )
    return {
        "motzoi": motzoi,
        "slope": float(slope),
        "fit": fit_summary(
            x, y, slope * x + intercept, x_label="beta", y_label="signal"
        ),
    }


#: How far past its own bound the demodulated fine-amplitude sweep may reach before
#: the contrast it was normalised by counts as no contrast at all — see
#: :func:`fit_fine_amplitude`. Three, against a model bound of one: the simulated chip
#: reaches 0.66 at worst, and the two hardware runs that wrote a wrong amp180 reached
#: 8.9 and 144.3.
MAX_DEMODULATED = 3.0

#: How much of the demodulating quadrature a setpoint may sit off before the sweep is
#: refused as incompatible with the angle it claims to amplify — see
#: :func:`fit_fine_amplitude`. Exactly zero is what the model wants and what every
#: correct setpoint gives to within float error; this is a rounding tolerance, not a
#: budget.
MAX_QUADRATURE_LEAK = 1e-9


def fit_fine_amplitude(
    repetitions: np.ndarray,
    signal: np.ndarray,
    amplitude: float,
    ground: float,
    excited: float,
    turn: float = np.pi,
    pre_rotation: float = np.pi / 2,
) -> dict[str, float]:
    """Fit a fine-amplitude (amplification) sweep.

    The sequence is a π/2 pre-rotation followed by ``n`` repetitions of the π
    pulse under test. With a per-pulse rotation error ``δ`` the excited-state
    population is

        ``P(n) = ½·(1 + (-1)ⁿ·sin(n·δ))``

    so demodulating by ``(-1)ⁿ`` leaves ``½·sin(n·δ)``, a straight line through
    the origin for small ``δ`` whose slope *is* ``δ``.

    The pre-rotation is what makes the sign recoverable. Without it the signal
    is ``cos(n(π+δ))``, and at integer ``n`` that is identical for ``+δ`` and
    ``-δ`` — the fit would report the size of the error and guess its direction.

    *ground* and *excited* are reference measurements of |0⟩ and |1⟩, taken in
    the same schedule. They are required, not optional: the demodulated signal
    is ``A·sin(nδ)``, and without knowing the full contrast ``A`` only the
    product ``A·δ`` is recoverable. Normalising against the observed range
    instead would divide by however much of the contrast this particular sweep
    happened to reach, and report an error inflated by exactly that fraction.

    *turn* is the angle the pulse under test is meant to turn, and *pre_rotation* the
    exact rotation in front of the repetitions. The defaults are a pi pulse behind a
    pi/2, which is what `fine_amplitude` plays; `fine_amplitude_90` amplifies the pi/2
    itself, with no pre-rotation and every fourth repetition count.

    Returns ``{'amplitude', 'amplitude_error', 'error_per_pulse'}`` — the corrected
    amplitude for whichever angle *turn* names, the error as a fraction of it, and the
    error in radians per pulse.
    """
    x, y = align(repetitions, signal, what="fine amplitude")
    counts = np.rint(x)
    if not np.allclose(counts, x):
        raise FitError("fine amplitude is swept over whole pulse repetitions")

    contrast = float(excited) - float(ground)
    if abs(contrast) < 1e-12:
        raise FitError(
            "fine-amplitude calibration points are indistinguishable — the qubit "
            "is not responding, so there is no contrast to normalise against"
        )
    centre = (float(excited) + float(ground)) / 2
    total = pre_rotation + counts * turn
    _require_amplifying_setpoints(counts, total, turn)
    demodulated = ((y - centre) / (contrast / 2)) * np.sin(total)

    # `demodulated` is sin(n*delta), so the model bounds it at one. Far outside that
    # and the contrast it was divided by was not the |0>-|1> contrast: the two
    # reference points came back nearly equal, the quotient blows up, and the slope
    # through it is fitted from noise — then written to `amp180`, the amplitude every
    # X pulse afterwards uses. The simulated chip stays under 0.66; a chip whose
    # readout had gone off resonance returned 8.9, and 144.3 on the run that wrote a
    # wrong amp180 and broke everything downstream of it.
    reach = float(np.max(np.abs(demodulated)))
    if reach > MAX_DEMODULATED:
        raise FitError(
            f"the demodulated sweep reaches {reach:.3g}, past the {MAX_DEMODULATED:g} a "
            f"signal bounded at one can plausibly show — the |0>-|1> contrast it was "
            f"normalised by ({contrast:.4g}) is not resolving the qubit, so the slope "
            f"is noise and the amplitude it implies is not a calibration"
        )

    # Slope through the origin: the offset is fixed by the model, so fitting one
    # would let a baseline shift masquerade as a rotation error.
    denominator = float(np.sum(counts**2))
    if denominator <= 0:
        raise FitError("fine amplitude needs at least one non-zero repetition count")
    error_per_pulse = float(np.sum(counts * demodulated) / denominator)

    if abs(error_per_pulse) >= turn / 2:
        raise FitError(
            f"fine-amplitude error of {error_per_pulse:.4g} rad/pulse is out of "
            "range — the starting amplitude is too far off for this refinement"
        )

    # The pulse turns by `turn` + δ where it should turn by `turn`, so scale it back.
    corrected = amplitude * turn / (turn + error_per_pulse)
    return {
        "amplitude": require_positive(corrected, what="corrected amplitude"),
        "amplitude_error": error_per_pulse / turn,
        "error_per_pulse": error_per_pulse,
        # The demodulated signal rather than the raw one: the straight line through
        # the origin is the thing being fitted, and the raw sweep alternates about
        # the centre so a chart of it shows nothing.
        "fit": fit_summary(
            counts,
            demodulated,
            error_per_pulse * counts,
            x_label="pulses",
            y_label="demodulated",
        ),
    }


def _require_amplifying_setpoints(
    counts: np.ndarray, total: np.ndarray, turn: float
) -> None:
    """Refuse repetition counts at which the error does not show up linearly.

    The signal after *n* pulses of nominal angle ``turn`` and error ``d``, behind a
    pre-rotation, is ``-cos(total + n*d)``, which expands to
    ``-cos(total)*cos(n*d) + sin(total)*sin(n*d)``. Only the second term carries the
    sign of the error, so demodulating by ``sin(total)`` recovers ``sin(n*d)`` — but
    only where ``cos(total)`` vanishes. Where it does not, a second-order term in the
    error leaks in at full strength and the slope through it is not the error.

    For a pi pulse behind a pi/2 pre-rotation that holds at every integer *n*, which is
    why nothing needed to check it before. For a pi/2 pulse it holds only at
    ``n = 1, 5, 9, ...``: at ``n = 3`` the pulse has turned three quarters and the
    quadratures have swapped, at ``n = 2`` the response is flat in the error to first
    order. Sweeping 1..25 there would fit a straight line through three quarters noise
    and write the result to an amplitude every gate afterwards uses.
    """
    leak = float(np.max(np.abs(np.cos(total))))
    if leak <= MAX_QUADRATURE_LEAK:
        return
    period = int(round(2 * np.pi / turn)) if turn > 0 else 0
    wanted = (
        f" — for a {np.degrees(turn):.0f} degree pulse sweep every {period}th count, "
        f"starting at one"
        if period > 1
        else ""
    )
    raise FitError(
        f"these repetition counts do not amplify a {np.degrees(turn):.0f} degree "
        f"pulse's error: {leak:.3g} of the signal is in the quadrature the fit "
        f"discards, where the model wants none, so the slope would be second order "
        f"in the error rather than first{wanted}. Counts: "
        f"{', '.join(str(int(c)) for c in counts[:8])}"
        f"{'...' if counts.size > 8 else ''}"
    )
