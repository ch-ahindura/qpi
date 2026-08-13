"""Turning a rotation angle into a drive amplitude (RFC 0007 §11.5).

Both schedulers derive every ``Rxy`` angle from ``amp180`` alone, by linear
interpolation — quantify's ``rxy_drag_pulse`` says so in its own docstring, and
qblox's is the same function under a different parameter name. That assumes the
rotation angle is proportional to the amplitude, which stops being true as the
amplitude approaches full scale and the output chain compresses.

It is measurable, and it was measured. On the August 2026 B chip ``amp180`` was
0.5757 — past half of full scale — and the AllXY equator block carried an
antisymmetric error of +0.1795 that survived the residual detuning being driven
to -384 Hz, three orders of magnitude below what the Ramsey window resolves. A
pi/2 that is not half of a pi is the remaining explanation, and there was nowhere
to write a separately measured one.

This module is the interpolation those factories should have used, and it is
deliberately the *only* place the arithmetic lives: the two schedulers' element
classes are kept parallel by hand, and a rotation that differs between them would
be a chip that calibrates under one and not the other.
"""

import math

#: The angle ``amp90`` is the amplitude of. Not a free parameter — the whole point
#: is that this one angle is measured rather than interpolated.
QUARTER_TURN_DEGREES = 90.0

#: The angle ``amp180`` is the amplitude of.
HALF_TURN_DEGREES = 180.0


def amplitude_for_angle(theta: float, amp180: float, amp90: float = 0.0) -> float:
    """Drive amplitude that turns *theta* degrees, given what has been measured.

    With *amp90* unmeasured this is ``amp180 * theta / 180`` exactly — the same
    straight line both schedulers already draw, to the last bit. That equality is
    what makes the field safe to add to an element that has never been calibrated
    for it, and it is asserted rather than assumed.

    With *amp90* measured the line becomes two segments meeting at 90 degrees, so
    both measurements are honoured exactly and everything between them is
    interpolated. Piecewise-linear rather than a curve fitted through the two:
    two points do not determine a compression curve, and a quadratic through them
    is free to turn back on itself, which would hand a larger angle a smaller
    amplitude. Monotonic is worth more here than smooth.

    Past 180 degrees the upper segment continues, which keeps a ``Rxy(270)`` — a
    gate no routine here emits, but a scheduler is free to — from folding back
    onto an amplitude it already used.

    Args:
        theta: rotation angle in degrees, signed.
        amp180: amplitude of a pi pulse.
        amp90: amplitude of a pi/2 pulse, or zero if it was never measured.
    """
    if not amp90 or math.isnan(amp90):
        return amp180 * theta / HALF_TURN_DEGREES

    magnitude = abs(theta)
    if magnitude <= QUARTER_TURN_DEGREES:
        scaled = amp90 * magnitude / QUARTER_TURN_DEGREES
    else:
        upper = magnitude - QUARTER_TURN_DEGREES
        scaled = amp90 + (amp180 - amp90) * upper / QUARTER_TURN_DEGREES
    return math.copysign(scaled, theta)
