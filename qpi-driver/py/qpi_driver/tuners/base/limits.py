"""What the instrument can actually produce (RFC 0007 §5).

A sweep's range is not a matter of taste when the hardware bounds it. An RF module
reaches its local oscillator plus or minus a fixed intermediate frequency, and outside
that there is no experiment to run — only a compiler error. So a routine that needs to
search for a line can ask how wide the search may be, rather than being told.
"""

import logging
from typing import Any

from qpi_driver.tuners.base.device import _walk

log = logging.getLogger(__name__)

#: Held back from each end of a band, in Hz. Clamping to the limit exactly puts a
#: setpoint *on* it, and the compiler's own rounding — the NCO is programmed in steps of
#: a quarter hertz — then places it a fraction outside and rejects the schedule. A
#: kilohertz is far below any linewidth worth sweeping and removes the edge case.
_BAND_MARGIN_HZ = 1e3


def addressable_band(
    device: Any, port_clock: str, if_limit_hz: float
) -> tuple[float, float] | None:
    """The frequencies *port_clock* can be driven at, as ``(low, high)``.

    *port_clock* is the hardware config's own key, ``"q0:mw-q0.01"``. The local
    oscillator comes from the wiring rather than from the device, which is why this is
    not on the element: two clocks on one port share an LO, and neither knows it.

    ``None`` when the wiring cannot be read or names no LO for this port. That is the
    honest answer for a config this driver does not recognise, and callers treat it as
    "no bound known" rather than as an empty band — a search that refused to run because
    it could not find an LO would be worse than one that guesses a span.
    """
    try:
        options = device.hardware_config().hardware_options
        lo = options.modulation_frequencies[port_clock].lo_freq
    except Exception:  # noqa: BLE001 - an unreadable config is not a bound of zero
        log.debug("no addressable band for %s: wiring unreadable", port_clock)
        return None

    if lo is None:
        # A port driven at baseband, or one whose LO the config leaves to the cluster.
        return None
    reach = float(if_limit_hz) - _BAND_MARGIN_HZ
    return (float(lo) - reach, float(lo) + reach)


def clamp_to_band(
    low: float, high: float, band: tuple[float, float] | None
) -> tuple[float, float]:
    """*low* to *high*, trimmed to what *band* can address.

    A search centred on a configured frequency is not centred on the LO, so half of a
    symmetric span can fall outside the module's reach while the other half is fine.
    Trimming keeps the reachable half instead of failing the whole sweep, which is the
    difference between finding a qubit 300 MHz off and reporting that the NCO complained.
    """
    if band is None:
        return (low, high)
    return (max(low, band[0]), min(high, band[1]))


#: What a waveform may reach before it clips, in the schedulers' own amplitude units.
#: A hardware fact rather than a device one: the DAC has a full-scale output and a pulse
#: asking past it is not a louder pulse, it is a distorted one.
FULL_SCALE = 1.0


def full_scale(element: Any, dotted: str) -> float:
    """The largest amplitude *dotted* may be swept to on *element*.

    :data:`FULL_SCALE` unless the element says something tighter. Both bounds are real
    and neither implies the other, so the smaller wins:

    - the hardware's, because a waveform past full scale clips;
    - the element's own validator, where it has one worth having.

    Worth knowing which is which. `spec.amplitude` and `r12.ef_amp180` on a
    `CalibratedTransmon` validate ``[0, 1]``, so for those the two agree. But quantify's
    `BasicTransmonElement` validates ``rxy.amp180`` in ``[-10, 10]`` — a sanity range, not
    a drive bound — so *nothing on the element stops a pi pulse being set to 5*, and the
    only reason a sweep stops at full scale is this function. An earlier RFC draft claimed
    the element bounded it at one; it does not.
    """
    try:
        owner, name = _walk(element, dotted)
        validator = getattr(getattr(owner, "parameters", {}).get(name), "vals", None)
        declared = getattr(validator, "_max_value", None)
    except Exception:  # noqa: BLE001 - an element that will not say is not a bound of zero
        declared = None
    if declared is None:
        return FULL_SCALE
    return min(float(declared), FULL_SCALE)
