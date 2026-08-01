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

import numpy as np

from .core import FitError

log = logging.getLogger(__name__)


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
    return {
        "acq_rotation": rotation,
        "acq_threshold": threshold,
        "assignment_fidelity": 1.0 - 0.5 * (ground_error + excited_error),
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

    (frequency, amplitude), fitted = best
    log.debug(
        "readout operating point %.6g Hz at %.4g, snr %.2f",
        frequency,
        amplitude,
        fitted["snr"],
    )
    return {"readout_frequency": frequency, "readout_amplitude": amplitude, **fitted}


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
