"""The readout chain's timing, read off a single raw trace.

Every other fit in this package consumes one point per setpoint. This one consumes
a `Trace` — a time series the digitiser recorded at 1 GSa/s — and nothing is swept:
the shape of one acquisition *is* the measurement.

Two numbers come out of it, and they come out **together** because they cannot be
separated by measuring them separately. A level crossing looks like the obvious way
to find when the signal arrives, and it is biased late by the resonator's own
ring-up: at a quarter of the settled level the trace is already ``0.29 * tau`` past
onset, which for a 2 MHz resonator is 23 ns on a delay of 148. Fitting the ring-up
to find ``tau`` needs the onset, and finding the onset needs ``tau``.

So both are recovered from one straight line. On the rising edge

    ``|S| = F + (A - F) * (1 - exp(-(t - t0) / tau))``

rearranges to ``ln(1 - r) = -(t - t0) / tau`` for the normalised rise ``r``, which
is linear in ``t`` with slope ``-1/tau`` and root ``t0``. No optimiser, no seed, and
the two answers are consistent by construction.
"""

import logging

import numpy as np

from .core import FitError

log = logging.getLogger(__name__)

#: The part of the rising edge the fit uses, as a fraction of the full swing. Below
#: the lower bound the trace is in its noise floor and ``ln(1 - r)`` is dominated by
#: that noise; above the upper bound ``1 - r`` approaches zero and its logarithm
#: diverges, so a sample there carries almost no information and unbounded error.
RISE_LOW = 0.10
RISE_HIGH = 0.90


#: Turns a fill time constant into the resonator linewidth it implies, in Hz.
#: ``tau = 1 / (2*pi*kappa)``, so this is the inverse — reported because the
#: linewidth is the number other routines reason with, and nothing else measures it.
def _linewidth_from(ring_up: float) -> float:
    return 1.0 / (2.0 * np.pi * ring_up) if ring_up > 0 else 0.0


def fit_readout_timing(trace: np.ndarray, sampling_rate: float) -> dict[str, float]:
    """When the readout signal arrives, and how fast the resonator fills.

    Returns ``{'time_of_flight', 'ring_up_time', 'linewidth', 'settled_amplitude',
    'noise_floor'}`` — seconds, except the linewidth in Hz.

    ``time_of_flight`` goes to ``measure.acq_delay`` so a window opens when the
    signal is there rather than while it is still in the cables. Nothing here sets
    ``measure.integration_time``: the ring-up is a *floor* on it, not an optimum,
    and choosing the optimum trades signal-to-noise against relaxation during the
    window — which needs the discrimination fidelity of RFC 0005 phase 4 to
    measure. Writing three time constants would have shortened the reference
    config's 1 us window to 240 ns on the strength of a criterion that does not
    mention noise.

    Raises:
        FitError: if the trace never rises out of its own noise — which means the
            readout returned no signal at all, a chain fault rather than a
            mismeasured delay — or if too little of the rising edge was captured to
            fit.
    """
    magnitude = np.abs(np.asarray(trace, dtype=complex).reshape(-1))
    if magnitude.size < 16:
        raise FitError(
            f"readout timing needs a trace, got {magnitude.size} samples — ask for a "
            "raw acquisition (meas_level 0) rather than an integrated one"
        )
    if sampling_rate <= 0:
        raise FitError(f"sampling rate must be positive, got {sampling_rate}")

    tail = max(magnitude.size // 5, 1)
    settled = float(np.mean(magnitude[-tail:]))
    # The baseline from the *front* of the trace, not from a low percentile over all
    # of it. A percentile works only when there is dead time to find: with the delay
    # already calibrated the trace starts rising immediately, so the 5th percentile
    # lands part-way up the ring-up and reports a floor about half the swing too
    # high — which moved the recovered onset by 50 ns on a true zero.
    #
    # The front is the baseline in both cases, because the resonator is empty when
    # the window opens whether or not it waited first.
    # Short, because with the delay already calibrated the rise begins at sample
    # zero and a long lead averages part of it into the baseline. Eight samples of
    # a 79 ns ring-up is 5% of the swing, which costs about 3 ns on the onset.
    lead = min(8, max(magnitude.size // 100, 4))
    floor = float(np.mean(magnitude[:lead]))
    swing = settled - floor
    scatter = float(np.std(magnitude[-tail:]))
    if swing <= 3.0 * scatter:
        raise FitError(
            f"the trace never rises out of its own noise (swing {swing:.4g} against "
            f"scatter {scatter:.4g}), so there is no arrival to time — the readout "
            "returned no signal, which is a chain fault rather than a wrong delay"
        )

    rise = (magnitude - floor) / swing
    usable = np.nonzero((rise >= RISE_LOW) & (rise <= RISE_HIGH))[0]
    if usable.size < 4:
        raise FitError(
            f"only {usable.size} samples lie on the rising edge, which is too few to "
            "fit — either the window opened after the resonator had filled, or the "
            "ring-up is faster than this sampling rate can see"
        )

    # Contiguous from the first crossing, so a later sample that dips back through
    # the band on noise does not extend the fit into the settled region.
    start = int(usable[0])
    end = start
    while end + 1 < rise.size and rise[end + 1] <= RISE_HIGH:
        end += 1
    span = np.arange(start, end + 1)
    remaining = 1.0 - rise[span]
    keep = remaining > 1.0 - RISE_HIGH
    if keep.sum() < 4:
        raise FitError(
            "too few usable points on the rising edge to fit a time constant"
        )

    times = span[keep] / sampling_rate
    slope, intercept = np.polyfit(times, np.log(remaining[keep]), 1)
    if slope >= 0:
        raise FitError(
            f"the trace does not rise towards its settled level (slope {slope:.4g}), "
            "so neither an arrival time nor a fill time constant can be read from it"
        )

    ring_up = float(-1.0 / slope)
    arrival = float(intercept * ring_up)
    return {
        # Clamped at zero: a negative onset means the window opened after the signal
        # had already started, and the delay to configure is then none at all.
        "time_of_flight": max(arrival, 0.0),
        "ring_up_time": ring_up,
        "linewidth": _linewidth_from(ring_up),
        "settled_amplitude": settled,
        "noise_floor": floor,
    }
