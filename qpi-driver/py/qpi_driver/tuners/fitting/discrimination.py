"""The readout discriminator: which rotation and threshold separate the clouds.

Every other fit here reduces an acquisition to a magnitude and fits a curve. This
one keeps the *complex* shots, because the two clouds differ in phase as much as in
magnitude and the whole question is where to put a line between them.

What it produces is the rule the instrument itself applies to every
``meas_level=2`` shot: rotate the IQ plane by ``acq_rotation``, then call the shot
``|1>`` if the real part clears ``acq_threshold``. Nothing else in this package
produces those two numbers, and their defaults of zero are correct only for a chain
that happens to put the clouds either side of the imaginary axis.
"""

import logging
import math
from statistics import NormalDist

import numpy as np

from .core import FitError

log = logging.getLogger(__name__)

#: How often single shots must be assigned correctly for a discriminator to be worth
#: writing. Chance is 0.5.
#:
#: The failure it exists for: on the August 2026 B chip, whose qubit was never excited,
#: `readout_operating_point` reported an assignment fidelity of **0.53** and wrote the
#: operating point it came from — 45% of ground shots and 49% of excited ones on the wrong
#: side of the threshold. The significance test above passed it, because with 300 shots a
#: 0.57e-3 separation against a 3.5e-3 scatter is a statistically real difference of means
#: and a useless readout. `readout_discrimination` refused the same chip only because it
#: averages 2000 shots and so happened to sit the other side of a shot-count-dependent bar.
#:
#: 0.6 rather than higher because this also gates `readout_operating_point`, which runs
#: *before* the readout is optimised and may legitimately start poor — but if the best
#: setting in its grid cannot clear 0.6, the sweep found nothing worth writing.
MIN_ASSIGNMENT_FIDELITY = 0.6

#: How far apart the closest two of three readout clouds must be, in units of the scatter
#: within them, for an operating point to be worth writing.
#:
#: `fit_three_state_discrimination` refuses at one — below that a threshold is a coin toss —
#: so the point that *feeds* it needs margin above that, or a run whose noise differs
#: slightly writes a point its own consumer then refuses. Which is what the August 2026 B
#: chip did: `three_state_operating_point` reported 0.913 and succeeded,
#: `three_state_discrimination` measured 0.86 on the same readout and refused. 1.5 leaves
#: half a scatter of headroom.
MIN_THREE_STATE_SEPARATION = 1.5

#: The separation below which three clouds carry too little to be worth writing at all,
#: in units of the scatter within them. Derived from :data:`MIN_ASSIGNMENT_FIDELITY`
#: rather than chosen.
#:
#: Two Gaussians ``d`` scatters apart can be told apart at best ``Phi(d/2)`` of the time,
#: so the separation at which the *confusable* pair reaches the 0.6 this file already
#: calls the minimum worth writing is ``2*Phi^-1(0.6) = 0.51``. Below that the closest two
#: states are nearer a coin toss than a measurement and nothing downstream recovers them.
#:
#: Between here and :data:`MIN_THREE_STATE_SEPARATION` the point is written anyway and
#: marked degraded. A readout resolving its worst pair 68% of the time is poor, but it is
#: information — and refusing it takes four nodes down with it that are each capable of
#: judging their own data. The August 2026 B chip sat at 0.93 for six runs, which is
#: `Phi(0.465)` = 68%, while `three_state_discrimination`, `ramsey_12`, `drag_12` and
#: `fine_amplitude_12` never ran once.
MIN_USABLE_SEPARATION = 2.0 * float(NormalDist().inv_cdf(MIN_ASSIGNMENT_FIDELITY))


def fit_readout_discrimination(
    ground: np.ndarray, excited: np.ndarray
) -> dict[str, float]:
    """The rotation and threshold that best separate two clouds of single shots.

    *ground* and *excited* are complex IQ points, one per shot, from preparing
    ``|0>`` and ``|1>``.

    The rotation is the direction from one cloud's centre to the other, so that after
    rotating, all the information about which state a shot came from lies in its real
    part and none in its imaginary part. That is not a convention: it is what makes a
    single real threshold sufficient, and it is why the instrument offers a rotation
    at all rather than only a threshold.

    The threshold is then the midpoint *weighted by the clouds' spreads*, which is the
    maximum-likelihood boundary for two Gaussians of unequal width. Equal widths give
    the plain midpoint back, so nothing is lost when the two clouds are alike.

    Returns ``{'acq_rotation', 'acq_threshold', 'assignment_fidelity',
    'separation', 'ground_error', 'excited_error'}`` — the rotation in degrees within
    ``[0, 360)``, which is the range the instrument accepts, and the threshold in the
    acquisition's own units.

    Raises:
        FitError: if either cloud is empty, or if the two centres coincide to within
            their own scatter. The second is the one that matters: a readout with no
            separation has no discriminator, and inventing one from noise would put
            a confident, meaningless threshold on the device.
    """
    zero = np.asarray(ground, dtype=complex).reshape(-1)
    one = np.asarray(excited, dtype=complex).reshape(-1)
    if zero.size < 2 or one.size < 2:
        raise FitError(
            f"discrimination needs single shots of both states, got {zero.size} "
            f"and {one.size} — ask for BinMode.APPEND rather than an average"
        )

    centre_zero, centre_one = complex(zero.mean()), complex(one.mean())
    separation = abs(centre_one - centre_zero)
    # Scatter along the line joining them, which is the only direction the threshold
    # can be crossed by noise.
    spread = float(np.std(np.concatenate([zero, one])))
    # A *significance* test: are the two cloud means distinguishable at all. Kept
    # because it catches a degenerate fit cheaply, but it is not a usability test and
    # cannot be one — the 1/sqrt(n) means more averaging lowers the bar, so 2000 shots
    # accept a separation 2.6x smaller than 300 shots do. That is right for "do the means
    # differ" and backwards for "can a single shot be assigned", which is what a readout
    # has to do. The assignment-fidelity floor below is the test that answers that.
    if separation <= 2.0 * spread / np.sqrt(min(zero.size, one.size)):
        raise FitError(
            f"the two readout clouds are {separation:.4g} apart against a scatter of "
            f"{spread:.4g}, which is no separation at all — the readout is not "
            "resolving the qubit, so there is no discriminator to fit"
        )

    # Wrapped into [0, 360), because the instrument requires that and says so:
    # "Attempting to configure acq_rotation to -153.73 ... while the hardware requires
    # it to be between 0 and 360." `np.angle` returns (-180, 180], so half of all
    # chains produce a value the compiler refuses — and it refuses it in *every later
    # schedule*, not in the routine that wrote it. A rotation and that rotation plus a
    # turn are the same rotation, so nothing is lost.
    rotation = float(np.rad2deg(np.angle(centre_one - centre_zero)) % 360.0)
    turn = np.exp(-1j * np.deg2rad(rotation))
    projected_zero = np.real(zero * turn)
    projected_one = np.real(one * turn)

    threshold = _weighted_midpoint(projected_zero, projected_one)
    ground_error = float(np.mean(projected_zero >= threshold))
    excited_error = float(np.mean(projected_one < threshold))
    assignment_fidelity = 1.0 - 0.5 * (ground_error + excited_error)
    if assignment_fidelity < MIN_ASSIGNMENT_FIDELITY:
        raise FitError(
            f"single shots are assigned correctly {assignment_fidelity:.0%} of the time, "
            f"against the {MIN_ASSIGNMENT_FIDELITY:.0%} a usable readout clears — "
            f"{ground_error:.0%} of ground shots and {excited_error:.0%} of excited ones "
            "land the wrong side of the threshold, so this discriminator would label "
            "noise. Check that the qubit is being excited before optimising the readout"
        )
    return {
        "acq_rotation": rotation,
        "acq_threshold": threshold,
        "assignment_fidelity": assignment_fidelity,
        "separation": separation,
        # Separation in units of the scatter that has to be crossed to confuse the
        # two. This is what `fit_readout_operating_point` ranks settings on, and the
        # reason it does not rank on fidelity: fidelity saturates at 1 and then only
        # counts the handful of shots that landed the wrong side, so above about four
        # sigma it stops telling a good readout from a better one.
        "snr": separation / spread if spread > 0 else float("inf"),
        "ground_error": ground_error,
        "excited_error": excited_error,
    }


def fit_readout_operating_point(
    settings: list[tuple[float, float]], ground: np.ndarray, excited: np.ndarray
) -> dict[str, float]:
    """The (frequency, amplitude) whose two clouds are furthest apart, in scatter units.

    One sweep over both axes rather than one node each, because they are one operating
    point and not two numbers. The resonance moves with power — that is punchout — so
    a frequency chosen at one amplitude is stale at another; choosing them in sequence
    means the second choice invalidates the first. `resonator_punchout` learned this
    for the *magnitude* readout and reports the frequency it found at the power it
    picked; this is the same lesson for the discriminated one.

    *settings* is one ``(frequency, amplitude)`` per row, and *ground* and *excited*
    are one row of complex single shots each.

    Settings where the clouds do not separate are skipped rather than failing the
    sweep: at the edge of a frequency scan, or at a power that has punched the
    resonator through, there is genuinely nothing to discriminate, and that is the
    measurement working. Only a sweep where *nothing* separates is an error.
    """
    zeros = np.asarray(ground, dtype=complex)
    ones = np.asarray(excited, dtype=complex)
    if zeros.shape[0] != len(settings) or ones.shape[0] != len(settings):
        raise FitError(
            f"readout operating point expected one row of shots per setting, got "
            f"{zeros.shape[0]} and {ones.shape[0]} rows for {len(settings)} settings"
        )

    (frequency, amplitude), fitted = _best_separating(settings, zeros, ones)
    log.debug(
        "readout operating point %.6g Hz at %.4g, snr %.2f",
        frequency,
        amplitude,
        fitted["snr"],
    )
    return {
        "readout_frequency": frequency,
        "readout_amplitude": amplitude,
        **fitted,
        **_magnitude_contrast(settings, zeros, ones, chosen=(frequency, amplitude)),
    }


#: How many standard errors a longer window must beat the incumbent by to be worth taking.
#:
#: An SNR estimated from ``n`` shots carries a relative error of about ``1/sqrt(2n)``, so a
#: sweep across a landscape that is genuinely flat still has a winner — and picking it moves
#: the readout on noise. Every window in the sweep is a multiple of the one already in use,
#: so the incumbent is always in it and is the right thing to fall back to.
#:
#: This matters most where it is hardest to notice. A simulator that does not model the
#: acquisition window at all produces exactly a flat landscape, and without this the node
#: shortened the simulated chip's readout fourfold on the strength of nothing.
MIN_SNR_IMPROVEMENT_SIGMA = 3.0


def fit_readout_integration_time(
    windows: list[float],
    ground: np.ndarray,
    excited: np.ndarray,
    *,
    incumbent: float = 0.0,
) -> dict[str, float]:
    """The acquisition window whose two clouds separate best, in scatter units.

    Its own node rather than a third axis on `fit_readout_operating_point`, because it is
    the one readout parameter with an *interior* optimum that no other node can supply.
    Signal accumulates with the window and noise only with its square root, so separation
    climbs as ``sqrt(t)`` — until the qubit starts relaxing inside the window, after which
    a longer one only adds shots of the wrong state. Where the two meet depends on T1 and
    on chi, which is to say it is a property of the chip and has to be measured on it.

    The ring-up `resonator_relaxation` reports is a *floor* on this and not the answer:
    it says when the resonator has finished responding, which is a statement about the
    resonator and mentions neither noise nor relaxation.

    *windows* is one integration time in seconds per row of shots.
    """
    zeros = np.asarray(ground, dtype=complex)
    ones = np.asarray(excited, dtype=complex)
    if zeros.shape[0] != len(windows) or ones.shape[0] != len(windows):
        raise FitError(
            f"readout integration time expected one row of shots per window, got "
            f"{zeros.shape[0]} and {ones.shape[0]} rows for {len(windows)} windows"
        )

    settings = [(w, 0.0) for w in windows]
    (best, _), fitted = _best_separating(settings, zeros, ones)

    # Only if it clears the incumbent by more than the shot noise on the comparison.
    held = _incumbent_fit(settings, zeros, ones, incumbent)
    if held is not None:
        shots = int(np.size(zeros) // max(len(windows), 1))
        margin = MIN_SNR_IMPROVEMENT_SIGMA / math.sqrt(2.0 * max(shots, 1))
        if fitted["snr"] <= held["snr"] * (1.0 + margin):
            log.info(
                "readout integration time held at %.4g s: the best window %.4g s gains "
                "%.1f%% of SNR, under the %.1f%% that %g sigma of shot noise on %d shots "
                "covers",
                incumbent,
                best,
                100.0 * (fitted["snr"] / held["snr"] - 1.0),
                100.0 * margin,
                MIN_SNR_IMPROVEMENT_SIGMA,
                shots,
            )
            return {"integration_time": float(incumbent), **held}

    if len(windows) > 1 and best >= max(windows):
        # Not a refusal — it is the best of what was reachable, and writing it is right.
        # But it is the edge of the sweep rather than a bracketed optimum, and on this axis
        # the edge is the instrument's own limit, so nothing further can be swept.
        log.warning(
            "readout integration time chose %.4g s, the longest window the hardware "
            "integrates into one bin — so the separation was still improving where the "
            "sweep ran out and this is a ceiling rather than an optimum. More readout SNR "
            "on this chip needs a change of hardware, not of window",
            best,
        )
    log.debug("readout integration time %.4g s, snr %.2f", best, fitted["snr"])
    return {"integration_time": float(best), **fitted}


def _incumbent_fit(
    settings: list[tuple[float, float]],
    zeros: np.ndarray,
    ones: np.ndarray,
    incumbent: float,
) -> dict[str, float] | None:
    """The discrimination fit at the window already in use, if it was swept and separates."""
    if not incumbent:
        return None
    for (window, _), zero_row, one_row in zip(settings, zeros, ones):
        if math.isclose(window, incumbent, rel_tol=1e-9):
            try:
                return fit_readout_discrimination(zero_row, one_row)
            except FitError:
                return None
    return None


def _best_separating(
    settings: list[tuple[float, float]], zeros: np.ndarray, ones: np.ndarray
) -> tuple[tuple[float, float], dict[str, float]]:
    """The setting whose two clouds are furthest apart, and its discrimination fit.

    Settings where the clouds do not separate are skipped rather than failing the sweep:
    at the edge of a frequency scan, at a power that has punched the resonator through,
    or in a window too short to have accumulated anything, there is genuinely nothing to
    discriminate and that is the measurement working. Only a sweep where *nothing*
    separates is an error.
    """
    best: tuple[tuple[float, float], dict[str, float]] | None = None
    skipped: list[str] = []
    for setting, zero_row, one_row in zip(settings, zeros, ones):
        try:
            fitted = fit_readout_discrimination(zero_row, one_row)
        except FitError as error:
            skipped.append(f"{setting}: {error}")
            continue
        if best is None or fitted["snr"] > best[1]["snr"]:
            best = (setting, fitted)

    if best is None:
        raise FitError(
            "no readout setting in the sweep separated the two states — "
            + "; ".join(skipped)
        )
    return best


def _magnitude_contrast(
    settings: list[tuple[float, float]],
    zeros: np.ndarray,
    ones: np.ndarray,
    *,
    chosen: tuple[float, float],
) -> dict[str, float]:
    """Where in this same sweep the two states differ most in *magnitude*.

    Reported, not chosen: the point above is the right one for a discriminator, and this
    is a different question with a different answer. Nearly every other node in the graph
    reduces its acquisition to a magnitude — `signal_of` — and none of them can use a
    complex separation, most of which is phase once the drive is off resonance.

    The gap that makes this worth measuring is that nothing else does. `resonator_
    spectroscopy` picks the readout frequency by where the *most signal comes back*, which
    is not where the two states' magnitudes differ most, and those two coincide only when
    the dispersive shift is large against the linewidth. On the August 2026 B chip it was
    0.259 of it, and a magnitude sweep there put the pi pulse at 0.1647 against a working
    0.3446 — 0.508x, the pi/2 — because ``|S|`` peaked at half population and came back to
    the ``|0>`` level at the pi. A cosine fitted to that finds half the period.

    Costs no acquisition: these are the shots the discriminator was already graded on.
    """
    per_setting: dict[tuple[float, float], tuple[float, float]] = {}
    for setting, zero_row, one_row in zip(settings, zeros, ones):
        low, high = np.abs(zero_row), np.abs(one_row)
        separation = abs(float(high.mean()) - float(low.mean()))
        scatter = float(np.mean([low.std(), high.std()]))
        per_setting[setting] = (separation, separation / scatter if scatter else 0.0)

    best = max(per_setting, key=lambda setting: per_setting[setting][1])
    separation, snr = per_setting[best]
    return {
        "magnitude_frequency": best[0],
        "magnitude_amplitude": best[1],
        "magnitude_contrast": separation,
        "magnitude_snr": snr,
        # The one number that says whether the graph is reading blind: the magnitude
        # contrast at the point the *discriminator* chose, which is the point every
        # magnitude node inherits nothing from and every complex one uses.
        "magnitude_snr_at_chosen": per_setting.get(chosen, (0.0, 0.0))[1],
    }


def _cloud_geometry(states: list[np.ndarray]) -> tuple[np.ndarray, float, float]:
    """Cloud centres, the scatter *within* a cloud, and the closest pair of centres.

    The scatter is measured per cloud and averaged, not taken over all the shots at
    once. Over the union it is dominated by how far apart the clouds are, so a readout
    that separates the states beautifully reports a huge "scatter" and every ratio
    built on it says the opposite of the truth — which is how a first version of this
    rejected clouds it had resolved to 39 sigma.
    """
    centres = np.array([complex(cloud.mean()) for cloud in states])
    spread = float(np.mean([np.std(cloud - cloud.mean()) for cloud in states]))
    closest = min(
        abs(centres[i] - centres[j])
        for i in range(len(centres))
        for j in range(i + 1, len(centres))
    )
    return centres, spread, closest


def fit_three_state_discrimination(clouds: list[np.ndarray]) -> dict[str, float]:
    """Assign shots among three prepared states, and say how often it is wrong.

    A rotation and a threshold cannot do this. Two clouds have one line between them
    and every point falls on one side; three have no such line, because ``|2>`` sits
    off the axis joining the other two — the resonances are evenly spaced but the
    complex *responses* at one drive frequency are not, so the centres make a triangle
    rather than a row. This classifies by nearest centre, the maximum-likelihood rule
    for clouds of equal width, which needs no axis at all.

    What it is for is **leakage**. Every gate leaves a little population in ``|2>``,
    and a two-state readout does not lose those shots — it reports them as ``|0>`` or
    ``|1>``, so they arrive as answers and every fidelity built on them is quietly
    optimistic. ``leakage`` is the fraction of shots prepared in ``|1>`` that land
    nearest the ``|2>`` centre, which is the number no two-state measurement produces.

    Returns ``{'assignment_fidelity', 'leakage', 'separation', 'error_0', ...}``.

    Raises:
        FitError: if fewer than three clouds are given, or if any two centres sit
            closer than the scatter around them — three states that do not resolve
            have no classifier, and inventing one puts confident nonsense on a report.
    """
    states = [np.asarray(cloud, dtype=complex).reshape(-1) for cloud in clouds]
    if len(states) < 3:
        raise FitError(
            f"three-state discrimination needs three prepared states, got {len(states)}"
        )
    if any(cloud.size < 2 for cloud in states):
        raise FitError(
            "three-state discrimination needs single shots of every state — ask for "
            "BinMode.APPEND rather than an average"
        )

    centres, spread, closest = _cloud_geometry(states)
    separation = closest / spread if spread > 0 else 0.0
    if separation < MIN_USABLE_SEPARATION:
        raise FitError(
            f"the closest two of the three readout clouds are {closest:.4g} apart "
            f"against a scatter of {spread:.4g} — {separation:.2f} scatters, under the "
            f"{MIN_USABLE_SEPARATION:.2f} at which they reach even "
            f"{MIN_ASSIGNMENT_FIDELITY:g} — so there is no three-state classifier to fit"
        )
    if separation < MIN_THREE_STATE_SEPARATION:
        # Fitted rather than refused, for the reason `fit_three_state_operating_point`
        # gives: the confusion matrix below is the honest description of a poor readout,
        # and it is more use to whatever reads it than a refusal is. The fidelity it
        # reports is what says how far to trust it.
        log.warning(
            "three-state clouds resolve to only %.2f scatters, under the %.1f a good "
            "readout shows — the confusion matrix below is real but the classifier it "
            "describes is weak",
            separation,
            MIN_THREE_STATE_SEPARATION,
        )

    # Rows are what was prepared, columns what it was read as.
    confusion = np.zeros((len(states), len(centres)))
    for prepared, cloud in enumerate(states):
        assigned = np.argmin(np.abs(cloud[:, None] - centres[None, :]), axis=1)
        for read in range(len(centres)):
            confusion[prepared, read] = float(np.mean(assigned == read))

    correct = float(np.mean(np.diag(confusion)))
    log.debug("three-state fidelity %.4f, leakage %.4f", correct, confusion[1, 2])
    return {
        "assignment_fidelity": correct,
        # |1> read as |2>: population on the third rung that a two-state readout
        # would have counted as one of the first two.
        "leakage": float(confusion[1, 2]),
        "separation": float(closest),
        "error_0": float(1.0 - confusion[0, 0]),
        "error_1": float(1.0 - confusion[1, 1]),
        "error_2": float(1.0 - confusion[2, 2]),
    }


def fit_three_state_operating_point(
    settings: list[tuple[float, float]], clouds: list[list[np.ndarray]]
) -> dict[str, float]:
    """The (frequency, amplitude) that resolves all three states, not merely two.

    Ranked on the *closest* pair of the three, because a classifier is only as good as
    the two states it confuses most: a point that flings ``|0>`` far away while leaving
    ``|1>`` and ``|2>`` on top of each other discriminates nothing useful. Maximising
    the minimum is what that sentence means arithmetically.

    A separate point from the two-state one, and it has to be. Tuned for ``|0>``
    against ``|1>``, the readout sits where ``|1>`` and ``|2>`` both return almost
    nothing and their clouds collapse together — 2.95 sigma apart on the simulated
    chip, against 39 where this node puts it.

    *clouds* is one list of three complex shot arrays per setting.
    """
    if len(clouds) != len(settings):
        raise FitError(
            f"three-state operating point expected one set of clouds per setting, got "
            f"{len(clouds)} for {len(settings)} settings"
        )

    best: tuple[tuple[float, float], float, float] | None = None
    for setting, states in zip(settings, clouds):
        arrays = [np.asarray(cloud, dtype=complex).reshape(-1) for cloud in states]
        if len(arrays) < 3 or any(cloud.size < 2 for cloud in arrays):
            continue
        _centres, spread, closest = _cloud_geometry(arrays)
        if spread <= 0:
            continue
        separation = closest / spread
        if best is None or separation > best[1]:
            best = (setting, separation, closest)

    if best is None:
        raise FitError(
            "no readout setting in the sweep resolved three states — the sweep may "
            "not have prepared |2>, or every setting punched the resonator through"
        )

    (frequency, amplitude), separation, closest = best
    if separation < MIN_USABLE_SEPARATION:
        raise FitError(
            f"the best readout setting in the sweep put its closest two clouds "
            f"{separation:.2f} scatters apart, under the {MIN_USABLE_SEPARATION:.2f} at "
            f"which the confusable pair reaches even {MIN_ASSIGNMENT_FIDELITY:g} — so "
            "this point does not resolve three states at all and nothing downstream can "
            "recover them. Most often the sweep never prepared |2>: check the 1-2 pi "
            "pulse before the readout"
        )
    degraded = separation < MIN_THREE_STATE_SEPARATION
    if degraded:
        # Written rather than refused. This used to raise, on the grounds that a point
        # below the bar would only be refused again by `three_state_discrimination` — but
        # that reasoning cost more than it saved: it also took `ramsey_12`, `drag_12` and
        # `fine_amplitude_12` down, none of which had been given a chance to judge their
        # own data. Each of them carries its own guard and can refuse on its own evidence.
        log.warning(
            "three-state operating point %.6g Hz at %.4g resolves its closest pair to "
            "only %.2f scatters, under the %.1f a good three-state readout shows — about "
            "%.0f%% on that pair. Written and marked degraded so the 1-2 chain can run, "
            "but treat anything it feeds as provisional",
            frequency,
            amplitude,
            separation,
            MIN_THREE_STATE_SEPARATION,
            100.0 * NormalDist().cdf(separation / 2.0),
        )
    return {
        "readout_frequency": frequency,
        "readout_amplitude": amplitude,
        "separation": closest,
        "snr": separation,
        # Out in the report because every number downstream of this point inherits it.
        "degraded": float(degraded),
    }


def _weighted_midpoint(low: np.ndarray, high: np.ndarray) -> float:
    """Where two Gaussians of unequal width cross, along one axis.

    ``(m0*s1 + m1*s0) / (s0 + s1)`` — the point each cloud is the same number of its
    *own* standard deviations from, which is the maximum-likelihood boundary. The
    plain midpoint is the special case of equal widths, so this only ever helps.
    """
    mean_low, mean_high = float(np.mean(low)), float(np.mean(high))
    spread_low, spread_high = float(np.std(low)), float(np.std(high))
    total = spread_low + spread_high
    if total <= 0:
        return 0.5 * (mean_low + mean_high)
    return (mean_low * spread_high + mean_high * spread_low) / total
