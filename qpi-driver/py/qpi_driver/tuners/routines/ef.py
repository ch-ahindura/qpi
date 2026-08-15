"""The 1-2 transition: driving the transmon's third level (RFC 0005 §7).

A transmon is not a qubit, it is an anharmonic ladder that is *used* as one, and the
third rung is the difference. Every gate leaks a little population into ``|2>``,
where two-state readout reports it as one of the other two — so a leaked shot is not
merely lost, it is counted as an answer. Measuring that is what the EF chain is for,
and it is the only route to it.

`f12_spectroscopy` found where the transition is. These drive it.

Every routine here plays a raw pulse rather than a gate, because neither scheduler's
transmon has an EF gate: the device config's operations are built for ``rxy`` on the
``.01`` clock, and the ``.12`` clock has no gate that references it. The pulse is
therefore assembled here, and its parameters live in `EFDrive` on a
`CalibratedTransmon` — an element without one gets no EF calibration, which is the
same opt-in every other addition in this RFC makes.
"""

from typing import Any

import logging
import math

import numpy as np
import xarray as xr

from qpi_driver.tuners.base.backend import SchedulerBackend
from qpi_driver.tuners.base.config import RoutineConfig
from qpi_driver.tuners.base.device import (
    measured_contrast,
    measured_linewidth,
    read_path,
    write_path,
)
from qpi_driver.tuners.base.limits import full_scale
from qpi_driver.tuners.base.config import DEFAULT_ROUTINE_TIMEOUT_S
from qpi_driver.tuners.base.routines import (
    CalibrationRoutine,
    RoutineError,
    grid_duration,
    require_resolved_line,
    linear_setpoints,
    setpoints_of,
)
from qpi_driver.tuners.fitting import (
    fit_drag,
    fit_fine_amplitude,
    fit_rabi,
    fit_ramsey,
    fit_resonator_spectroscopy,
    fit_three_state_discrimination,
    fit_three_state_operating_point,
    signal_of,
)

#: Shared with `resonator_spectroscopy_excited`: the same experiment one rung up wants
#: the same window, and two constants that must agree are one written twice.
from qpi_driver.tuners.routines.spectroscopy import (  # noqa: E402
    EXCITED_SPAN_IN_LINEWIDTHS,
)

log = logging.getLogger(__name__)

#: How far the fitted 1-2 pi amplitude may sit from the ladder the 0-1 one implies.
#:
#: A transmon's 1-2 matrix element is sqrt(2) times its 0-1 one, so at the same duration the
#: same rotation needs ``amp180 / sqrt(2)``. That is a statement about the ladder rather than
#: about a chip, which is what makes it usable as a bound: it holds to about 10% where it has
#: been measured — 0.1577 fitted against 0.1429 predicted — and a factor of two either way
#: leaves room for the duration differing and for the approximation itself.
#:
#: The failure it exists for, on the August 2026 B chip: `rabi_12` fitted ``ef_amp180`` of
#: 0.0677 against an ``amp180`` of 0.5757, which the ladder puts at 0.4071 — six times out,
#: so the pulse it wrote turned a fraction of a rotation rather than half of one. Nothing
#: objected, and the whole EF chain then measured a qubit still in |1>: the second-excited
#: sweep reported |2> *closer* to |0> than |1> is, which no transmon does, and
#: `three_state_discrimination` was left as the only node that refused.
MAX_EF_LADDER_ERROR = 2.0

#: How much of an oscillation the sweep must show before the ladder stops being evidence.
#:
#: The bound above exists for one failure and only one: `fit_rabi` fitting a *partial*
#: rotation, where a drive too weak to turn a pi leaves the cosine's half period longer
#: than the sweep and the fit extrapolates an arc into a smaller amplitude. That failure
#: has a signature, and it is the opposite of what an off-ladder amplitude looks like when
#: the drive is strong: a partial rotation shows *less* than one period, never more.
#:
#: The August 2026 B chip is why this is here. Its `rabi_12` sweep runs 0 to 0.5 and holds
#: three and a half full periods — five maxima and five minima, evenly spaced, a flat
#: envelope, a residual of 7.8% of contrast, and a peak-to-peak 1.5x `rabi`'s own, which is
#: what |0>-|2> should give against |0>-|1>. Nothing about that is a partial rotation, and
#: the ladder refused it three runs running on an amplitude 3.7x off. Both drives share a
#: LO, mixer corrections and attenuation in that chip's hardware config, so the factor is
#: real and unexplained — but a resolved measurement is not the place to litigate it.
MIN_RESOLVED_PERIODS = 1.0

#: The fraction of `rabi`'s contrast a 1-2 sweep must swing to count as turning a pi.
#:
#: `rabi_12` maps ``|2>`` back through a 0-1 pi before reading, so its two extremes are
#: ``|0>`` and ``|2>`` — a wider dispersive separation than the ``|0>``-``|1>`` one `rabi`
#: measures, which is why chips come in *above* 1.0 rather than at it. A drive too weak to
#: turn a pi cannot reach here at all: it moves a fraction of the population by definition,
#: and the fraction is what the fitted amplitude is short by.
#:
#: So this separates the two failures the ladder ratio alone cannot, and it does so without
#: a number taken from any chip: it is a ratio of two contrasts measured on the same qubit
#: through the same readout, minutes apart. 0.7 leaves room for the ``|2>`` shift being
#: sub-linear and for readout drift between the two nodes, and still sits far above the
#: fraction a partial rotation can produce.
MIN_LADDER_SWING = 0.7

#: The ratio a transmon's two lowest transitions must show, at the same pulse.
#:
#: The 1-2 matrix element is ``sqrt(2)`` times the 0-1 one, so the *same* pulse — same
#: shape, same length, same port — turns the same angle at ``1/sqrt(2)`` of the amplitude
#: one rung up. This is the only form of the ladder with nothing else in it: the envelope
#: ratio and the duration ratio both cancel when the two pulses are identical, which is
#: what `ef_ladder` exists to arrange. See :data:`EF_ENVELOPE_AREA` for the correction
#: `rabi_12`'s own guard needs, and which this measurement does not.
LADDER_RATIO = math.sqrt(2.0)

#: Linewidths of resonator to sweep when looking for the three-state readout point.
#:
#: Wider than the 0.6 `readout_operating_point` uses, because the ladder it has to resolve
#: is wider: two states sit ``2*chi`` apart and the point that tells them apart is a
#: fraction of a linewidth off resonance, while three sit across ``4*chi`` and the point
#: that separates all three can be a whole flank out. Borrowing the two-state coefficient
#: broke this node outright — see :meth:`ThreeStateOperatingPoint._grid`.
#:
#: 1.8, which is where the 6 MHz constant this replaces came from: it reproduces it on the
#: simulated chip's 3.31 MHz resonator to 0.7%. The constant only looked right because that
#: was the chip it was measured on.
SPAN_IN_LINEWIDTHS = 1.8


#: How many sigma quantify's DRAG envelope spans *each side* of centre.
#:
#: The whole point of naming it. ``nr_sigma`` is quantify's own parameter and its docstring
#: says "after how many sigma the Gaussian is cut off" — which is per side, so a pulse of
#: length ``T`` has ``sigma = T / (2 * nr_sigma)``, not ``T / nr_sigma``. Reading it the
#: other way is what put :data:`EF_ENVELOPE_AREA` out by exactly two, and a factor of two
#: is the one error this whole module is least able to see, because it is also the spacing
#: of the cosine roots `fit_rabi` chooses between.
RXY_NR_SIGMA = 4.0

#: Area of `rxy`'s envelope against the ef pulse's, at equal amplitude.
#:
#: They are not the same shape, which the first version of the ladder bound missed. `rxy`
#: compiles through quantify's ``rxy_drag_pulse`` to a Gaussian cut at
#: :data:`RXY_NR_SIGMA`; `add_ef_pulse` emits a `SquarePulse` of area ``A*T``. Rotation
#: follows area, so the same *nominal* amplitude turns a larger angle on the ef transition,
#: and comparing the two amplitudes without the correction centres the bound too high.
#:
#: Derived rather than integrated, so it is worth saying what pins it: `test_ef_envelope_
#: area_matches_the_real_waveform` integrates quantify's actual ``drag`` output and asserts
#: this number. It exists because the hand-derived version was wrong by two for four days,
#: refusing a chip whose `ef_ladder` then measured the ratio at 1.49 against sqrt(2) —
#: 5.6%, which is the anharmonic correction and not a factor of two.
EF_ENVELOPE_AREA = math.sqrt(2.0 * math.pi) / (2.0 * RXY_NR_SIGMA)

#: Where a `CalibratedTransmon` keeps its EF pulse.
EF = "r12"

#: And the readout point that resolves all three levels.
THREE_STATE = "measure_3state"

#: Last-resort length of an EF pulse, for an element whose ``rxy.duration`` cannot be
#: read. The amplitude a sweep reports only means anything alongside a duration — "the
#: amplitude that turns pi" is a statement about a particular pulse length — which is
#: why this is a fallback rather than the default: see :func:`ef_duration`.
DEFAULT_EF_DURATION = 20e-9


def has_ef_drive(device: Any, target: str) -> bool:
    """Whether *target* can hold an EF pulse at all.

    `CalibratedTransmon` carries `r12`; a `BasicTransmonElement` does not, and a chip
    configured with one is not broken — it simply has no EF chain. Declining is what
    lets the graph complete on such a chip instead of reporting six failures for
    parameters the element was never going to have.
    """
    return ef_path(device.get_element(target), "ef_amp180") is not None


def has_three_state_readout(device: Any, target: str) -> bool:
    """Whether *target* can hold a three-state readout point."""
    element = device.get_element(target)
    return (
        has_ef_drive(device, target)
        and three_state_path(element, "frequency") is not None
    )


def ef_path(element: Any, name: str) -> str | None:
    """``r12.<name>`` if this element has one, else ``None``."""
    submodule = getattr(element, EF, None)
    if submodule is None or not hasattr(submodule, name):
        return None
    return f"{EF}.{name}"


def ef_duration(element: Any, config: RoutineConfig) -> float:
    """How long an EF pulse plays: the config, the element, or ``rxy.duration``.

    Falling back to the 0-1 pulse's length rather than to a constant. Both defaulted to
    20 ns, so a constant looked equivalent — but ``rxy.duration`` is configuration and
    moves with the chip, and the amplitude a sweep reports only means anything alongside
    a duration. Left at 20 ns against an ``rxy`` of 56, the 1-2 pi would need 2.8 times
    the 0-1 amplitude rather than ``1/sqrt(2)`` of it — 0.71 against a sweep that stops
    at 0.5, which no sweep of that range could ever have found. The August 2026 B chip
    sets both to 56 ns by hand and so never hit this; the trap is that it did not have
    to, and nothing would have said so.

    Nothing measures this, so an element carrying an explicit value is stating intent
    and keeps it. Zero means unset, which is why the element defaults to zero rather
    than to a length that silently disagrees with ``rxy``.
    """
    if "duration" in config:
        return float(config["duration"])
    path = ef_path(element, "ef_duration")
    if path:
        stored = read_path(element, path)
        if stored:
            return float(stored)
    return _rxy_duration(element) or DEFAULT_EF_DURATION


def add_ef_pulse(
    schedule: Any,
    backend: SchedulerBackend,
    target: str,
    amplitude: float,
    duration: float,
    phase_deg: float = 0.0,
    drag: float = 0.0,
    transition: str = "12",
) -> None:
    """One pulse on the ``.<transition>`` clock, into the port ``rxy`` uses.

    *transition* is ``"12"`` for every caller that calibrates the EF chain. `ef_ladder`
    passes ``"01"`` to play this exact pulse on the lower transition instead, which is the
    only way to measure the ladder without the pulse shape and duration in the way.

    Square by default, and shaped as soon as a phase or a DRAG coefficient is asked
    for — `SquarePulse` carries neither. The pulse *area* is what sets the rotation
    and the simulator normalises a shaped envelope to unit mean, so a Gaussian of the
    same amplitude and duration turns the same angle: `ef_amp180` keeps its meaning
    across the switch.

    *drag* is in the backend's own units, which differ between the two schedulers —
    see `SchedulerBackend.drag_pulse`.
    """
    clock = f"{target}.{transition}"
    port = f"{target}:mw"
    if drag or phase_deg % 360.0:
        schedule.add(
            backend.drag_pulse(
                amp=amplitude,
                drag=drag,
                duration=duration,
                port=port,
                clock=clock,
                phase_deg=phase_deg,
            )
        )
        return
    schedule.add(
        backend.SquarePulse(amp=amplitude, duration=duration, port=port, clock=clock)
    )


class Rabi12(CalibrationRoutine):
    """Sweep the EF drive amplitude to find the pi pulse between ``|1>`` and ``|2>``.

    The same experiment as `rabi` one rung up, with one thing that is not the same:
    it starts from ``|1>``, which it has to prepare and which relaxes while the EF
    pulse plays. So the oscillation sits on a decaying background rather than a flat
    one, and its contrast is smaller — the fit's offset term absorbs the first, and
    the second is why this asks for more shots than `rabi` does.

    Read out on the two-state chain, deliberately. ``|2>`` has its own place in the IQ
    plane — the dispersive pull is ``chi(1-2n)``, so the three levels form a ladder —
    and a magnitude readout sees the population move even without a three-state
    discriminator. That is enough to find the pi amplitude, and it is what lets this
    node come *before* three-state readout rather than after: the discriminator needs
    a calibrated EF pulse to prepare ``|2>`` in the first place.
    """

    name = "rabi_12"
    depends_on = ("f12_spectroscopy",)
    updates = (f"{EF}.ef_amp180",)
    reads = (
        "r12.ef_duration",
        "rxy.duration",
        "clock_freqs.f01",
        "rxy.amp180",
        "resonator.contrast",
    )

    def applies_to(self, device: Any, target: str) -> bool:
        """Only to an element with somewhere to keep an EF pulse."""
        return has_ef_drive(device, target)

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        element = device.get_element(target)
        # Half scale, and deliberately *not* full scale the way `rabi` now is. The bound
        # here is the model rather than the hardware: `_drive_ef` neglects the
        # off-resonant 0-1 term, and `f12_spectroscopy` records that half a pi pulse is
        # already where that starts to matter. Sweeping to 1.0 samples a regime the
        # cosine this fit assumes does not describe, and it showed: the fitted ef pi
        # moved to 0.1577 against 0.1429 for a sqrt(2) ladder, and `ramsey_12`'s fringe
        # fell to 2.8x its scatter against the 3x its guard allows.
        #
        # So the ef ceiling is physics-bounded (RFC 0007 §5) and lower than full scale.
        # `full_scale` is still the ceiling on the ceiling, for an element that declares
        # something tighter still.
        self._amplitudes = setpoints_of(
            config,
            "amplitudes",
            linear_setpoints(0.0, min(0.5, full_scale(element, f"{EF}.ef_amp180")), 41),
        )
        self._duration = ef_duration(element, config)
        schedule = backend.new_schedule(
            self.name, repetitions=int(config.get("shots", 2048))
        )
        for index, amplitude in enumerate(self._amplitudes):
            schedule.add(backend.Reset(target))
            # Into |1> first, which is what makes this the 1-2 transition rather than
            # a second look at 0-1.
            schedule.add(backend.X(target))
            add_ef_pulse(schedule, backend, target, amplitude, self._duration)
            # Back to |0> if the ef drive did nothing, and left in |2> if it turned a pi.
            #
            # Without this the readout has to tell |1> from |2> *directly*, and it is sitting
            # at an operating point chosen to separate |0> from |1> — where the two upper
            # levels project close together, because a 0-1 discriminator is tuned to put its
            # threshold between the first two and not the second two. The trace then barely
            # oscillates, and `fit_rabi` halves the period of whatever cosine it can find: on
            # the August 2026 B chip that returned an ef pi of 0.0677 against the 0.4071 the
            # sqrt(2) ladder predicts, six times out and near the *bottom* of a sweep that
            # reached 0.5, so no range guard could see it either.
            #
            # A second 0-1 pi maps |1> back to |0> and leaves |2> where it is, off-resonant
            # by the 250 MHz anharmonicity against a pulse whose bandwidth is some 18 MHz. So
            # the ef oscillation appears in the |0> population, which is the one quantity this
            # readout is already good at, and the contrast is the full readout contrast rather
            # than the difference between two dispersive shifts.
            #
            # This is also what unblocks the chain's bootstrap: every other EF node reads at
            # `measure_3state`, which cannot be calibrated until something has populated |2>,
            # and this is the node that has to do it first.
            schedule.add(backend.X(target))
            schedule.add(
                backend.Measure(
                    target, acq_index=index, bin_mode=backend.BinMode.AVERAGE
                )
            )
        return schedule

    def analyse(
        self, dataset: xr.Dataset, target: str, device: Any, config: RoutineConfig
    ) -> dict[str, Any]:
        # Seeded with what the ladder predicts, not checked against it afterwards. The two
        # cosines that fit a thin 1-2 sweep differ by a factor of two in period and the
        # data often cannot separate them; `_best_rabi_fit` keeps the physical one only
        # while that stays true, so this steers the fit without deciding it.
        fitted = fit_rabi(
            np.asarray(self._amplitudes),
            signal_of(dataset),
            expected_amp180=ladder_amplitude(device, target, self._duration) or None,
        )
        _require_ef_ladder(
            device,
            target,
            fitted["amp180"],
            self._duration,
            contrast=float(fitted.get("contrast", 0.0)),
            reference_contrast=measured_contrast(device.get_element(target)),
            fit=fitted.get("fit"),
            span=float(max(self._amplitudes)) - float(min(self._amplitudes)),
        )
        # The trace on success too, which only a refusal carried before. The shape is the
        # one thing separating the two ways this node comes back wrong, and they are
        # indistinguishable in `ef_amp180` alone: a 1-2 drive twice as strong as the ladder
        # expects, or a sweep whose period the cosine halved. `build_schedule` records that
        # the second has happened on this chip before.
        return {
            "ef_amp180": fitted["amp180"],
            "ef_duration": self._duration,
            "fit": fitted.get("fit"),
        }

    def apply(self, device: Any, target: str, params: dict[str, Any]) -> None:
        element = device.get_element(target)
        path = ef_path(element, "ef_amp180")
        if path:
            write_path(element, path, params["ef_amp180"])


def three_state_path(element: Any, name: str) -> str | None:
    """``measure_3state.<name>`` if this element has one, else ``None``."""
    submodule = getattr(element, THREE_STATE, None)
    if submodule is None or not hasattr(submodule, name):
        return None
    return f"{THREE_STATE}.{name}"


def three_state_point(element: Any) -> dict[str, float] | None:
    """The readout point three-state shots are taken at, if calibrated."""
    paths = {
        name: three_state_path(element, name) for name in ("frequency", "pulse_amp")
    }
    if not all(paths.values()):
        return None
    point = {name: float(read_path(element, path)) for name, path in paths.items()}
    if point["frequency"] <= 0.0 or point["pulse_amp"] <= 0.0:
        return None
    return point


def _required_ef_amplitude(element: Any, target: str) -> float:
    path = ef_path(element, "ef_amp180")
    amplitude = float(read_path(element, path)) if path else 0.0
    if amplitude <= 0:
        raise RoutineError(
            f"{target} has no calibrated EF pi pulse, so |2> cannot be prepared — "
            f"run rabi_12 first"
        )
    return amplitude


class ThreeStateOperatingPoint(CalibrationRoutine):
    """Where to read so all three levels are distinguishable, not merely two.

    The two-state point cannot do this, and it is not close: tuned for ``|0>`` against
    ``|1>``, the readout sits where ``|1>`` and ``|2>`` both return almost nothing and
    their clouds collapse together — 2.95 sigma apart on the simulated chip, against
    39 here.

    Ranked on the *closest* pair of the three, since a classifier is only as good as
    the two states it confuses most.

    On this chip the amplitude runs to the top of its range. The separation does peak
    and fall — punch-through eventually collapses all three together — but it peaks at
    about twice full scale, so within what the instrument can play more is better.
    That is a property of this resonator rather than of the criterion, and it is why
    the bound is full scale rather than a chosen number.
    """

    name = "three_state_operating_point"
    # On the *refined* ef pi, not the coarse one. Populating |2> is this node's whole
    # premise — its own refusal says so, "most often the sweep never prepared |2>" — and
    # `rabi_12` lands within a few per cent at best and on the wrong oscillation at worst.
    # `fine_amplitude_12` amplifies the residual until it is unambiguous, and now runs on
    # the 0-1 readout so it can sit here rather than behind this node.
    depends_on = ("fine_amplitude_12", "readout_operating_point")
    updates = (f"{THREE_STATE}.frequency", f"{THREE_STATE}.pulse_amp")
    reads = (
        "clock_freqs.readout",
        "measure.pulse_amp",
        "resonator.linewidth",
        "r12.ef_amp180",
        "r12.ef_duration",
        "rxy.duration",
        "clock_freqs.f01",
        "rxy.amp180",
    )

    #: Two, not three. The register budget buys ten settings and they are better
    #: spent on frequency: the amplitude runs to the top of whatever range it is
    #: given — the separation peaks near twice full scale — while the frequency is
    #: where the choice actually is.
    AMPLITUDE_FACTORS = (1.0, 1.5)

    #: Three prepared states per setting against the register budget
    #: `ReadoutOperatingPoint` explains, so this grid is smaller than that node's.
    MAX_SINGLE_SHOT_ACQUISITIONS = 32

    def applies_to(self, device: Any, target: str) -> bool:
        return has_three_state_readout(device, target)

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        element = device.get_element(target)
        self._ef_amplitude = _required_ef_amplitude(element, target)
        self._duration = ef_duration(element, config)
        self._settings = self._grid(element, config)

        schedule = backend.new_schedule(
            self.name, repetitions=int(config.get("shots", 300))
        )
        index = 0
        for frequency, amplitude in self._settings:
            schedule.add(
                backend.SetClockFrequency(
                    clock=f"{target}.ro", clock_freq_new=frequency
                )
            )
            for level in (0, 1, 2):
                schedule.add(backend.Reset(target))
                if level >= 1:
                    schedule.add(backend.X(target))
                if level >= 2:
                    add_ef_pulse(
                        schedule, backend, target, self._ef_amplitude, self._duration
                    )
                schedule.add(
                    backend.Measure(
                        target,
                        acq_index=index,
                        bin_mode=backend.BinMode.APPEND,
                        pulse_amp=amplitude,
                    )
                )
                index += 1
        return schedule

    def _grid(self, element: Any, config: RoutineConfig) -> list[tuple[float, float]]:
        centre = float(read_path(element, "clock_freqs.readout"))
        # Five points, and a span from the linewidth `resonator_spectroscopy` measured
        # rather than a constant six megahertz. Three points stepped 3 MHz against a
        # 2 MHz linewidth found a point good enough to classify three states and not
        # good enough for anything measured *at* it: `ramsey_12` reads |1> against |2>,
        # and on the coarse point its f12 came back a megahertz out where a properly
        # placed one gives kilohertz.
        #
        # Six megahertz, and a constant — this is the one span in the graph that resisted
        # being derived, so the reason is recorded rather than the number quietly kept.
        #
        # Two attempts. Sharing `readout_operating_point`'s 0.6 of a linewidth broke it
        # outright: `ramsey_12`'s T2* came back at 250 us against a 0-1 coherence of 20 us,
        # a fit extrapolating through a fringe with no decay left in it. The coefficient
        # does not transfer because the ladder is wider — two states sit 2chi apart and the
        # point that tells them apart is within a fraction of a linewidth of resonance,
        # while three sit across 4chi and the point that separates all three can be
        # further out.
        #
        # Then 1.8 linewidths, which reproduces this constant on the simulated chip almost
        # exactly (1.8 x 3.31 MHz = 5.96 MHz). That left `ramsey_12`'s fringe at 3.0x its
        # scatter against the 3x its guard allows — passing by nothing, on a quantity that
        # now varies with a measurement.
        #
        # Derived after all, from the coefficient the note above had already found. 1.8
        # linewidths reproduces the old 6 MHz constant on the simulated chip to 0.7%
        # (1.8 x 3.31 MHz = 5.96 MHz), and a constant is what broke this on the second
        # chip the graph met — the same way it broke `readout_operating_point`, whose
        # 2 MHz was 5.4 linewidths there and put the outer setpoints off resonance.
        #
        # This resonator is 327 kHz wide, so 6 MHz is *eighteen* linewidths: four of the
        # five points would sit where nothing comes back. Hand-narrowing it to 200 kHz
        # goes wrong the other way — that is 0.61 linewidths, which is the two-state
        # coefficient this node's docstring records as breaking it outright, and it never
        # reaches the flank where |1> and |2> separate. Those two sit only 11 kHz apart in
        # resonator shift (-102 against -91 kHz) against a 327 kHz linewidth, so the point
        # that tells them apart is a flank away, not a tenth of a linewidth away.
        #
        # Placement still wants more *points* than the sequencer's single-shot registers
        # allow — five, since two amplitudes x five frequencies x three states is 30
        # against a limit of 32. That bound is unchanged; only the width now follows the
        # chip. RFC 0007 §5.
        span = float(
            config.get(
                "span",
                SPAN_IN_LINEWIDTHS * measured_linewidth(element, 6e6 / SPAN_IN_LINEWIDTHS),
            )
        )
        points = int(config.get("points", 5))
        frequencies = (
            setpoints_of(config, "frequencies", [])
            if "frequencies" in config
            else linear_setpoints(centre - span / 2, centre + span / 2, points)
        )
        current = float(read_path(element, "measure.pulse_amp"))
        amplitudes = (
            setpoints_of(config, "amplitudes", [])
            if "amplitudes" in config
            else sorted({min(f * current, 1.0) for f in self.AMPLITUDE_FACTORS})
        )
        grid = [(f, a) for a in amplitudes for f in frequencies]
        if 3 * len(grid) > self.MAX_SINGLE_SHOT_ACQUISITIONS:
            raise RoutineError(
                f"{len(frequencies)} frequencies x {len(amplitudes)} amplitudes needs "
                f"{3 * len(grid)} single-shot acquisitions, and a sequencer has "
                f"registers for {self.MAX_SINGLE_SHOT_ACQUISITIONS}"
            )
        return grid

    def analyse(
        self, dataset: xr.Dataset, target: str, device: Any, config: RoutineConfig
    ) -> dict[str, Any]:
        shots = _prepared_clouds(dataset, 3 * len(self._settings))
        clouds = [shots[i * 3 : i * 3 + 3] for i in range(len(self._settings))]
        return fit_three_state_operating_point(self._settings, clouds)

    def apply(self, device: Any, target: str, params: dict[str, Any]) -> None:
        element = device.get_element(target)
        for name, key in (
            ("frequency", "readout_frequency"),
            ("pulse_amp", "readout_amplitude"),
        ):
            path = three_state_path(element, name)
            if path:
                write_path(element, path, params[key])


def open_three_state_readout(
    schedule: Any, backend: SchedulerBackend, target: str, element: Any
) -> dict[str, Any]:
    """Point the readout where all three levels are distinguishable.

    Returns the kwargs the `Measure` needs, having already put the frequency on the
    schedule — the measure operation's clock is fixed at ``{qubit}.ro`` in the device
    config, so a frequency is applied by moving that clock rather than as an argument.

    An uncalibrated point means "leave the readout where it is", which is honest: the
    routines that call this still run, they just run with whatever contrast the
    two-state point happens to give — and between ``|1>`` and ``|2>`` that is close to
    none.
    """
    point = three_state_point(element)
    if not point:
        return {}
    schedule.add(
        backend.SetClockFrequency(
            clock=f"{target}.ro", clock_freq_new=point["frequency"]
        )
    )
    return {"pulse_amp": point["pulse_amp"]}


class ResonatorSpectroscopySecondExcited(CalibrationRoutine):
    """The resonator again with the qubit in ``|2>`` — the third rung of the ladder.

    `resonator_spectroscopy` and `resonator_spectroscopy_excited` give the first two
    resonances, and their difference is the dispersive shift. This gives the third,
    and what it adds is a *test of the model rather than a parameter*: the pull is
    supposed to go as ``chi(1 - 2n)``, evenly spaced in the excitation number, and
    nothing else in the graph checks that the spacing is even.

    It matters because three-state readout rests on it. A ladder that bunched up would
    put ``|1>`` and ``|2>`` closer than ``|0>`` and ``|1>``, and
    `three_state_operating_point` would be optimising against a chip whose levels
    cannot be separated however it is tuned. This is the node that would say so.

    A characterisation, like the other two resonator sweeps that write nothing.
    """

    name = "resonator_spectroscopy_second_excited"
    depends_on = ("rabi_12",)
    updates = ()
    reads = (
        "clock_freqs.readout",
        "resonator.linewidth",
        "r12.ef_amp180",
        "r12.ef_duration",
        "rxy.duration",
        "clock_freqs.f01",
        "rxy.amp180",
    )

    def applies_to(self, device: Any, target: str) -> bool:
        return has_ef_drive(device, target)

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        element = device.get_element(target)
        amplitude = _required_ef_amplitude(element, target)
        duration = ef_duration(element, config)
        # The reference `analyse` differences against, read here rather than there: a
        # prerequisite has to be readable before the acquisition to be one at all, and
        # this sweep is already centred on the same value.
        self._ground = centre = float(read_path(element, "clock_freqs.readout"))
        # From the measured linewidth, as `resonator_spectroscopy_excited` does and for the
        # same reason: this has to find a resonance the ladder has moved, so it wants
        # several linewidths rather than a refinement's fraction of one.
        span = float(
            config.get(
                "span",
                EXCITED_SPAN_IN_LINEWIDTHS * measured_linewidth(element, 2.5e6),
            )
        )
        points = int(config.get("points", 51))
        self._frequencies = setpoints_of(
            config,
            "frequencies",
            linear_setpoints(centre - span / 2, centre + span / 2, points),
        )

        schedule = backend.new_schedule(
            self.name, repetitions=int(config.get("shots", 1024))
        )
        for index, frequency in enumerate(self._frequencies):
            schedule.add(backend.Reset(target))
            schedule.add(backend.X(target))
            add_ef_pulse(schedule, backend, target, amplitude, duration)
            schedule.add(
                backend.SetClockFrequency(
                    clock=f"{target}.ro", clock_freq_new=frequency
                )
            )
            schedule.add(
                backend.Measure(
                    target, acq_index=index, bin_mode=backend.BinMode.AVERAGE
                )
            )
        return schedule

    def analyse(
        self, dataset: xr.Dataset, target: str, device: Any, config: RoutineConfig
    ) -> dict[str, Any]:
        fitted = fit_resonator_spectroscopy(self._frequencies, signal_of(dataset))
        require_resolved_line(fitted, self._frequencies)
        second = fitted["readout_frequency"]
        ground = self._ground
        return {
            "readout_frequency_second_excited": second,
            # See `resonator_spectroscopy_excited`: the reference is not measured here.
            "readout_frequency_ground": ground,
            # Quarter of the gap, because |0> sits at +chi and |2> at -3chi: four
            # dispersive shifts apart. Equal to the shift the excited-state sweep
            # reports if the ladder is linear, and that equality is the measurement.
            "dispersive_shift": 0.25 * (second - ground),
            "linewidth": fitted["linewidth"],
            "fit": fitted["fit"],
        }

    def apply(self, device: Any, target: str, params: dict[str, Any]) -> None:
        """Nothing to write — see the class docstring."""


class FineAmplitude12(CalibrationRoutine):
    """Amplify a small error in the EF pi pulse by repeating it.

    `rabi_12` fits a whole oscillation and lands within a few per cent; this measures
    the residual by playing the pulse n times, so a per-pulse error grows linearly
    while the readout noise does not. The same trick `fine_amplitude` plays one rung
    down, and for the same reason: a single pi pulse is second order in its own error.

    The pi/2 pre-rotation is not decoration. Without it the response goes as
    ``cos(n(pi+delta))``, identical at integer ``n`` for an over- and an
    under-rotation; the pre-rotation makes it a sine and the sign measurable.

    **Reads at the ordinary 0-1 point, by mapping back.** It used to read at the
    three-state one and depend on `three_state_operating_point`, on the grounds that its
    reference states are ``|1>`` and ``|2>`` and those sit almost on top of each other at
    a 0-1 readout. True, and `rabi_12` solves it: a second 0-1 pi after the ef pulses
    returns ``|1>`` to ``|0>`` and leaves ``|2>`` where it is, so the ef rotation appears
    in the ``|0>`` population — the one quantity a 0-1 readout is already good at. The
    same trick, one node along.

    That dependency was also a deadlock. `three_state_operating_point` needs a correct ef
    pi to populate ``|2>`` at all, and this is the node that makes the ef pi correct; on a
    chip whose three-state clouds never separated, this and the three nodes behind it never
    ran once in six attempts. tergite-autocalibration's equivalent, `n_rabi_12_oscillations`,
    reads at ``qubit_state = 1`` and needs no three-state readout either.

    Amplification is also what settles which oscillation `rabi_12` found. A per-pulse error
    grows linearly with repetitions while noise does not, so an ef amplitude that is out by
    the factor of two that chip alternates between is unmistakable by the seventh pulse,
    where a single-pulse sweep confuses the two.
    """

    name = "fine_amplitude_12"
    depends_on = ("rabi_12",)
    updates = (f"{EF}.ef_amp180",)
    reads = (
        "r12.ef_amp180",
        "r12.ef_duration",
        "rxy.duration",
        "clock_freqs.f01",
        "rxy.amp180",
    )

    def applies_to(self, device: Any, target: str) -> bool:
        return has_ef_drive(device, target)

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        element = device.get_element(target)
        self._amplitude = _required_ef_amplitude(element, target)
        self._duration = ef_duration(element, config)
        self._repetitions = [
            int(n) for n in setpoints_of(config, "repetitions", list(range(1, 26)))
        ]

        schedule = backend.new_schedule(
            self.name, repetitions=int(config.get("shots", 1024))
        )

        for index, count in enumerate(self._repetitions):
            schedule.add(backend.Reset(target))
            schedule.add(backend.X(target))
            # Half the amplitude is half the rotation: the drive is linear in it at
            # fixed duration, which is the same assumption `rabi_12` fits under.
            add_ef_pulse(
                schedule, backend, target, self._amplitude / 2.0, self._duration
            )
            for _ in range(count):
                add_ef_pulse(schedule, backend, target, self._amplitude, self._duration)
            # Back to |0> if the ef pulses left the qubit in |1>, and untouched in |2>.
            # See the class docstring: this is what puts the accumulated ef error into
            # the |0> population and lets a 0-1 readout resolve it.
            schedule.add(backend.X(target))
            schedule.add(
                backend.Measure(
                    target,
                    acq_index=index,
                    bin_mode=backend.BinMode.AVERAGE,
                )
            )

        # The EF subspace's two states, measured through the *same* map-back as the sweep
        # above — otherwise the contrast the fit divides by is not the contrast the sweep
        # traversed. Without them only the product of contrast and rotation error is
        # recoverable, and the error comes out scaled by whatever fraction of the contrast
        # this sweep happened to cover.
        #
        # So both references end with the mapping pi: no ef pulse leaves |1>, which maps to
        # |0>, and one ef pi leaves |2>, which does not. They are the two ends of the
        # population axis this sweep actually moves along.
        reference = len(self._repetitions)
        for offset, prepare_two in enumerate((False, True)):
            schedule.add(backend.Reset(target))
            schedule.add(backend.X(target))
            if prepare_two:
                add_ef_pulse(schedule, backend, target, self._amplitude, self._duration)
            schedule.add(backend.X(target))
            schedule.add(
                backend.Measure(
                    target,
                    acq_index=reference + offset,
                    bin_mode=backend.BinMode.AVERAGE,
                )
            )
        return schedule

    def analyse(
        self, dataset: xr.Dataset, target: str, device: Any, config: RoutineConfig
    ) -> dict[str, Any]:
        signal = signal_of(dataset)
        expected = len(self._repetitions) + 2
        if signal.size < expected:
            raise RoutineError(
                f"fine amplitude 12 expected {expected} acquisitions, got {signal.size}"
            )
        swept = signal[: len(self._repetitions)]
        in_one, in_two = (
            float(signal[len(self._repetitions)]),
            float(signal[len(self._repetitions) + 1]),
        )
        fitted = fit_fine_amplitude(
            np.asarray(self._repetitions, dtype=float),
            swept,
            self._amplitude,
            ground=in_one,
            excited=in_two,
        )
        return {"ef_amp180": fitted["amplitude"], **fitted}

    def apply(self, device: Any, target: str, params: dict[str, Any]) -> None:
        element = device.get_element(target)
        path = ef_path(element, "ef_amp180")
        if path:
            write_path(element, path, params["ef_amp180"])


class EfLadder(CalibrationRoutine):
    """Measure the sqrt(2) ladder directly, with the pulse shape and duration taken out.

    A characterisation, not a calibration: it writes nothing. It exists because
    `rabi_12`'s ladder guard has to *predict* the 1-2 pi amplitude from the 0-1 one, and
    that prediction carries two corrections which are properties of the pulses rather than
    of the chip — `rxy` compiles to a Gaussian of area ``0.627*A*T`` where `add_ef_pulse`
    emits a square of area ``A*T``, and the two may be configured to different lengths. On
    the August 2026 B chip the prediction came out 3.7x above the measurement and no
    correction accounted for it.

    So this removes the corrections rather than refining them. It sweeps the *same* pulse
    `rabi_12` sweeps — same envelope, same duration, same port, over the same amplitudes —
    on the ``.01`` clock instead of the ``.12``. Both factors cancel identically, and what
    is left is the ladder alone: the ratio of the two pi amplitudes must be
    :data:`LADDER_RATIO`, and it is a statement about the transmon that no pulse convention
    can move.

    Which makes the answer diagnostic either way. Near ``sqrt(2)`` and the ladder holds, so
    `rabi_12`'s amplitude is right and the 3.7x lives in the corrections. Far from it and
    the two clocks are not being driven alike — the same nominal amplitude reaching the port
    differently at 68 MHz from the LO than at 319 MHz — which is a property of the output
    chain and not of the chip, and nothing in this graph can calibrate it away.
    """

    name = "ef_ladder"
    depends_on = ("rabi_12",)
    updates = ()
    reads = (
        "r12.ef_amp180",
        "r12.ef_duration",
        "rxy.duration",
        "rxy.amp180",
        "clock_freqs.f01",
    )

    def applies_to(self, device: Any, target: str) -> bool:
        """Only where `rabi_12` had somewhere to write, since this compares against it."""
        return has_ef_drive(device, target)

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        element = device.get_element(target)
        self._duration = ef_duration(element, config)
        # `rabi_12`'s own sweep, so the two amplitudes are read off the same grid. Any
        # difference between them is then the transitions and not the sampling.
        self._amplitudes = setpoints_of(
            config,
            "amplitudes",
            linear_setpoints(0.0, min(0.5, full_scale(element, f"{EF}.ef_amp180")), 41),
        )
        schedule = backend.new_schedule(
            self.name, repetitions=int(config.get("shots", 2048))
        )
        for index, amplitude in enumerate(self._amplitudes):
            schedule.add(backend.Reset(target))
            # From the ground state and on the lower clock, so this is an ordinary Rabi —
            # the only thing borrowed from the EF chain is the pulse itself.
            add_ef_pulse(
                schedule,
                backend,
                target,
                float(amplitude),
                self._duration,
                transition="01",
            )
            schedule.add(
                backend.Measure(
                    target, acq_index=index, bin_mode=backend.BinMode.AVERAGE
                )
            )
        return schedule

    def analyse(
        self, dataset: xr.Dataset, target: str, device: Any, config: RoutineConfig
    ) -> dict[str, Any]:
        signal = signal_of(dataset)
        if signal.size < len(self._amplitudes):
            raise RoutineError(
                f"ef ladder expected {len(self._amplitudes)} acquisitions, "
                f"got {signal.size}"
            )
        fitted = fit_rabi(
            np.asarray(self._amplitudes, dtype=float), signal[: len(self._amplitudes)]
        )
        matched = float(fitted["amp180"])
        measured = _measured_ef_amplitude(device, target)
        ratio = matched / measured if measured else 0.0
        log.info(
            "%s on %s: the same %g ns pulse turns pi at %.4g on 0-1 and %.4g on 1-2 — "
            "a ladder of %.3f against the %.3f a transmon's sqrt(2) requires (%.2fx out)",
            self.name,
            target,
            self._duration * 1e9,
            matched,
            measured,
            ratio,
            LADDER_RATIO,
            ratio / LADDER_RATIO if LADDER_RATIO else 0.0,
        )
        return {
            "matched_amp180": matched,
            "ef_amp180": measured,
            "ladder_ratio": ratio,
            "expected_ladder_ratio": LADDER_RATIO,
            # The number to read: one means the ladder holds and the pulse conventions
            # explain everything; anything else is the output chain.
            "ladder_agreement": ratio / LADDER_RATIO if LADDER_RATIO else 0.0,
            "pulse_duration": self._duration,
            "contrast": float(fitted.get("contrast", 0.0)),
            "fit": fitted.get("fit"),
        }

    def apply(self, device: Any, target: str, params: dict[str, Any]) -> None:
        """Nothing. The ladder is a property of the chip, not a setting on it."""


def _measured_ef_amplitude(device: Any, target: str) -> float:
    """What `rabi_12` wrote, or zero if this element has nowhere to keep it."""
    element = device.get_element(target)
    path = ef_path(element, "ef_amp180")
    if not path:
        return 0.0
    try:
        return float(read_path(element, path))
    except Exception:  # noqa: BLE001 - an unreadable amplitude is not a ladder
        return 0.0


class Ramsey12(CalibrationRoutine):
    """Ramsey interferometry on the 1-2 transition: refine f12 and measure its T2*.

    `f12_spectroscopy` drives a 20 ns pulse, so its line is Fourier-limited to tens of
    megahertz and it lands within a few. That is enough to *find* the transition and
    not enough to drive it cleanly, which is the same split `qubit_spectroscopy` and
    `ramsey` already have one rung down — spectroscopy finds, Ramsey measures.

    The second pi/2 is phase-advanced rather than the clock detuned, so the fringe is
    deliberate and its direction known. Reads at the three-state point, because its
    two states are ``|1>`` and ``|2>``: at a 0-1 readout those are nearly on top of
    each other, and the fringe would sit inside the noise.

    This could not be written until the EF drive shared a frame with the idles between
    its pulses. It did not: the phase used to accumulate at the whole anharmonicity, a
    3.5 ns period aliased to noise on any usable delay grid, so a Ramsey here measured
    the gap between two rotating frames rather than the transition.
    """

    name = "ramsey_12"
    depends_on = ("three_state_operating_point",)
    updates = ("clock_freqs.f12",)
    reads = (
        "clock_freqs.f12",
        "measure_3state.frequency",
        "measure_3state.pulse_amp",
        "r12.ef_amp180",
        "r12.ef_duration",
        "rxy.duration",
        "clock_freqs.f01",
        "rxy.amp180",
    )

    def applies_to(self, device: Any, target: str) -> bool:
        return has_three_state_readout(device, target)

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        element = device.get_element(target)
        # Half the pi amplitude is half the rotation, at fixed duration.
        self._half = _required_ef_amplitude(element, target) / 2.0
        self._duration = ef_duration(element, config)
        # The clock this run corrects, read before the acquisition rather than after it.
        self._current_f12 = float(read_path(element, "clock_freqs.f12"))
        # On the instrument's 1 ns grid. A linear sweep between two round numbers
        # generally is not — 41 points from 4 ns to 2 us step 49.9 ns — and the
        # compiler rejects a schedule whose operations do not land on it, some way
        # from the sweep that asked for them.
        # Squeezed from both ends, like `ramsey`'s, and measured rather than guessed.
        #
        # Long enough to *contain* the decay. The fit refuses a T2* the window never
        # saw, and rightly: a 2 us sweep against this coherence returned 1.4 seconds.
        # A 12 us one then fitted 15.5 us — accepted by the fit, but extrapolated past
        # its own window, which is a number to distrust. The 1-2 coherence here runs
        # about 15 us, so 30 us holds two time constants of it.
        #
        # Fine enough for the fringe: 125 ns steps put Nyquist at 4 MHz, well clear of
        # the 1 MHz advance below.
        self._delays = [
            grid_duration(delay)
            for delay in setpoints_of(
                config, "delays", linear_setpoints(4e-9, 30e-6, 241)
            )
        ]
        self._detuning = float(config.get("artificial_detuning", 1e6))

        schedule = backend.new_schedule(
            self.name, repetitions=int(config.get("shots", 1024))
        )
        measure = open_three_state_readout(schedule, backend, target, element)
        # The clock is detuned rather than the second pulse phase-advanced, which is
        # the opposite of what `ramsey` does one rung down and is not a preference.
        # A phase advance is a `ShiftClockPhase`, and on the ``.12`` clock it produced
        # no fringe at all: the fitted detuning came back at minus the artificial one
        # whatever the device's f12 was set to, so the sweep was measuring nothing.
        # Detuning the clock does work — the fringe tracks the offset — and it is what
        # the routine's own frame offset is built from.
        current = float(read_path(element, "clock_freqs.f12"))
        schedule.add(
            backend.SetClockFrequency(
                clock=f"{target}.12", clock_freq_new=current + self._detuning
            )
        )
        for index, delay in enumerate(self._delays):
            schedule.add(backend.Reset(target))
            schedule.add(backend.X(target))
            add_ef_pulse(schedule, backend, target, self._half, self._duration)
            backend.idle(schedule, delay)
            add_ef_pulse(schedule, backend, target, self._half, self._duration)
            schedule.add(
                backend.Measure(
                    target,
                    acq_index=index,
                    bin_mode=backend.BinMode.AVERAGE,
                    **measure,
                )
            )
        return schedule

    def analyse(
        self, dataset: xr.Dataset, target: str, device: Any, config: RoutineConfig
    ) -> dict[str, Any]:
        fitted = fit_ramsey(
            np.asarray(self._delays), signal_of(dataset), self._detuning
        )
        fitted["clock_freq_12"] = self._current_f12 - fitted["detuning"]
        return fitted

    def apply(self, device: Any, target: str, params: dict[str, Any]) -> None:
        write_path(
            device.get_element(target), "clock_freqs.f12", params["clock_freq_12"]
        )


class Drag12(CalibrationRoutine):
    """Sweep the EF pulse's derivative quadrature to cancel what it leaks.

    The same idea as `drag` one rung down, against a different neighbour. A pulse on
    the 1-2 transition is only a few linewidths from 0-1, so it off-resonantly excites
    it — about 2.6% here — and the DRAG quadrature is what cancels that.

    Two sequences that are equal only at the right coefficient: a 90 then a 180 at
    right angles, and the same pair with the angles swapped. Their difference crosses
    zero at the optimum and is linear about it, so the fit is a line rather than a
    peak.

    None of this could be measured until the EF drive became a real drive on the
    ladder. Before, it had no envelope for a coefficient to shape *and* no neighbour
    to leak into — ``|0>`` was a spectator — so the sweep would have found zero by
    construction rather than by measurement.
    """

    name = "drag_12"
    depends_on = ("ramsey_12",)
    updates = (f"{EF}.ef_motzoi",)
    reads = (
        "measure_3state.frequency",
        "measure_3state.pulse_amp",
        "r12.ef_amp180",
        "r12.ef_duration",
        "rxy.duration",
        "clock_freqs.f01",
        "rxy.amp180",
    )

    def applies_to(self, device: Any, target: str) -> bool:
        return has_three_state_readout(device, target)

    def measure(
        self,
        target: str,
        device: Any,
        config: RoutineConfig,
        backend: SchedulerBackend,
        bias: Any = None,
        timeout_s: float = DEFAULT_ROUTINE_TIMEOUT_S,
    ) -> dict[str, Any]:
        """Widen the beta sweep when the optimum turns out to be outside it.

        The default is `SchedulerBackend.drag_span` either side of zero, which is a
        statement about the units rather than about a chip — and the B chip's 1-2 optimum
        came out at 0.298 against a range of +/-0.2, so the node refused a fit that had
        found its answer.
        """
        return self.escalating(target, device, config, backend, timeout_s)

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        element = device.get_element(target)
        amplitude = _required_ef_amplitude(element, target)
        duration = ef_duration(element, config)
        # In the backend's own units, for the reason `drag` gives: a span sized for
        # quantify's ratio is nine orders out for qblox's seconds.
        self._drags = setpoints_of(
            config, "drags", linear_setpoints(-backend.drag_span, backend.drag_span, 31)
        )

        schedule = backend.new_schedule(
            self.name, repetitions=int(config.get("shots", 1024))
        )
        measure = open_three_state_readout(schedule, backend, target, element)
        for index, drag in enumerate(self._drags):
            for offset, (first, second) in enumerate(((0.0, 90.0), (90.0, 0.0))):
                schedule.add(backend.Reset(target))
                schedule.add(backend.X(target))
                add_ef_pulse(
                    schedule,
                    backend,
                    target,
                    amplitude / 2.0,
                    duration,
                    phase_deg=first,
                    drag=drag,
                )
                add_ef_pulse(
                    schedule,
                    backend,
                    target,
                    amplitude,
                    duration,
                    phase_deg=second,
                    drag=drag,
                )
                schedule.add(
                    backend.Measure(
                        target,
                        acq_index=2 * index + offset,
                        bin_mode=backend.BinMode.AVERAGE,
                        **measure,
                    )
                )
        return schedule

    def analyse(
        self, dataset: xr.Dataset, target: str, device: Any, config: RoutineConfig
    ) -> dict[str, Any]:
        signal = signal_of(dataset)
        if signal.size < 2 * len(self._drags):
            raise RoutineError(
                f"drag_12 expected {2 * len(self._drags)} acquisitions, got {signal.size}"
            )
        paired = signal[: 2 * len(self._drags)].reshape(-1, 2)
        fitted = fit_drag(
            np.asarray(self._drags), paired[:, 0] - paired[:, 1], axis="drags"
        )
        return {"ef_motzoi": fitted["motzoi"], **fitted}

    def apply(self, device: Any, target: str, params: dict[str, Any]) -> None:
        element = device.get_element(target)
        path = ef_path(element, "ef_motzoi")
        if path:
            write_path(element, path, params["ef_motzoi"])


class ThreeStateDiscrimination(CalibrationRoutine):
    """Prepare ``|0>``, ``|1>`` and ``|2>``, and measure how often each is misread.

    This is what the EF chain is for. Two-state readout does not *lose* a leaked shot
    — it reports it as ``|0>`` or ``|1>``, so leakage arrives as an answer and every
    fidelity measured on top of it is quietly optimistic. The only way to see it is to
    prepare the third state deliberately and count.

    A characterisation: it writes nothing. Using three-state assignment in the job
    path would mean a measurement level returning three outcomes, which is an API
    question rather than a calibration one — and a two-state chip is not wrong to
    report two, only incomplete. What this produces is the number saying how
    incomplete.
    """

    name = "three_state_discrimination"
    depends_on = ("three_state_operating_point",)
    updates = ()
    reads = (
        "measure_3state.frequency",
        "measure_3state.pulse_amp",
        "r12.ef_amp180",
        "r12.ef_duration",
        "rxy.duration",
        "clock_freqs.f01",
        "rxy.amp180",
    )

    #: Prepared states, in the order the confusion matrix indexes them.
    STATES = (0, 1, 2)

    def applies_to(self, device: Any, target: str) -> bool:
        return has_three_state_readout(device, target)

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        element = device.get_element(target)
        amplitude = _required_ef_amplitude(element, target)
        duration = ef_duration(element, config)

        schedule = backend.new_schedule(
            self.name, repetitions=int(config.get("shots", 2000))
        )
        # At the three-state point, not the two-state one: there |1> and |2> collapse
        # together and there is nothing to classify.
        measure_kwargs = open_three_state_readout(schedule, backend, target, element)
        for index, level in enumerate(self.STATES):
            schedule.add(backend.Reset(target))
            if level >= 1:
                schedule.add(backend.X(target))
            if level >= 2:
                add_ef_pulse(schedule, backend, target, amplitude, duration)
            schedule.add(
                backend.Measure(
                    target,
                    acq_index=index,
                    bin_mode=backend.BinMode.APPEND,
                    **measure_kwargs,
                )
            )
        return schedule

    def analyse(
        self, dataset: xr.Dataset, target: str, device: Any, config: RoutineConfig
    ) -> dict[str, Any]:
        return fit_three_state_discrimination(
            _prepared_clouds(dataset, len(self.STATES))
        )

    def apply(self, device: Any, target: str, params: dict[str, Any]) -> None:
        """Nothing to write — see the class docstring."""


def _prepared_clouds(dataset: Any, states: int) -> list[np.ndarray]:
    """One complex array of single shots per prepared state."""
    data_vars = getattr(dataset, "data_vars", None)
    if data_vars is None or not list(data_vars):
        raise RoutineError("the discrimination acquisition returned no data variables")

    values = np.asarray(dataset[list(data_vars)[0]].values)
    if values.ndim < 2 or values.shape[-1] < states:
        raise RoutineError(
            f"expected single shots of {states} prepared states, got shape "
            f"{values.shape} — the acquisition was averaged rather than appended, so "
            "the width of each cloud is gone and nothing can be classified"
        )
    return [values[..., index].reshape(-1) for index in range(states)]


def ladder_amplitude(device: Any, target: str, ef_duration: float) -> float:
    """The 1-2 pi amplitude the sqrt(2) ladder predicts, or 0 when it cannot be formed.

    Three corrections, and all three are properties of the pulses rather than of the chip:
    the ``sqrt(2)`` is the transmon's 1-2 matrix element, the envelope ratio is that `rxy`
    is a Gaussian where the ef pulse is a square, and the durations are whatever the two
    are configured to be. Rotation follows area, so a pulse half as long needs twice the
    amplitude — not a detail on a chip whose `rxy` is 56 ns against an ef pulse of 20.

    Public because two callers need the same number for different reasons: `_require_ef_
    ladder` checks a fitted amplitude against it afterwards, and `rabi_12` seeds its fit
    with it beforehand, so a sweep whose cosine has two near-degenerate minima lands in the
    one physics expects rather than the one the frequency estimator happened to guess.
    """
    try:
        amp180 = float(read_path(device.get_element(target), "rxy.amp180"))
    except Exception:  # noqa: BLE001 - an unreadable amp180 is not evidence
        return 0.0
    rxy_duration = _rxy_duration(device.get_element(target))
    if not amp180 or not rxy_duration or not ef_duration:
        return 0.0
    stretch = rxy_duration / ef_duration
    return amp180 * stretch * EF_ENVELOPE_AREA / math.sqrt(2.0)


def _require_ef_ladder(
    device: Any,
    target: str,
    ef_amp180: float,
    ef_duration: float,
    contrast: float = 0.0,
    reference_contrast: float = 0.0,
    fit: dict | None = None,
    span: float = 0.0,
) -> None:
    """Refuse a 1-2 pi amplitude the 0-1 one says cannot be a pi pulse.

    `fit_rabi` fits a cosine and takes its half period, and a partial rotation is still a
    cosine: driven too weakly the fit finds a longer period and reports a *smaller* amplitude
    with no sign that anything is wrong. What catches it is that the 1-2 amplitude is not
    free — see :data:`MAX_EF_LADDER_ERROR`.

    Silent when the element has no ``rxy.amp180`` to compare against, or it is zero: this
    runs after `rabi` in the graph, so an absent value means that node was disabled or
    skipped, and inventing a comparison against nothing would refuse a chip for the wrong
    reason.
    """
    expected = ladder_amplitude(device, target, ef_duration)
    if not expected:
        return
    element = device.get_element(target)
    amp180 = float(read_path(element, "rxy.amp180"))
    rxy_duration = _rxy_duration(element)
    stretch = rxy_duration / ef_duration
    ratio = ef_amp180 / expected
    if 1.0 / MAX_EF_LADDER_ERROR <= ratio <= MAX_EF_LADDER_ERROR:
        return

    # A resolved oscillation is not the failure this guard exists for, whatever the ladder
    # says about it — see :data:`MIN_RESOLVED_PERIODS`. Said rather than raised, because
    # the number is measured and the discrepancy is still worth an operator's attention.
    #
    # A factor of two used to be excluded from that benefit, because the August 2026 B chip
    # resolved clean oscillations at 1.81x the ladder while `resonator_spectroscopy_second_
    # excited` put |2>'s dispersive shift at -35 kHz against |1>'s -100 — |2> was not being
    # populated, so the oscillation was real and was not the 1-2 transition. That inference
    # came from another node. Once `rabi_12` maps |2> back through a 0-1 pi it can be made
    # here, from this sweep, against `rabi`'s own contrast — which is what *swing* is.
    #
    # It is the sharper test. A drive too weak to turn a pi cannot move the full population
    # however the ladder reads, and a drive that moves it is turning one somewhere. The same
    # B chip then swung 1.42x `rabi`'s contrast at 0.47x the ladder, between the two levels
    # the |0> and |2> readout magnitudes predict, with the sweep's second minimum exactly
    # at twice the fitted amplitude — a full 2-pi, so the first maximum is a pi and not a
    # pi/2. On that evidence the ladder constant is what is wrong, and refusing costs four
    # nodes to protect a prediction.
    periods = span / (2.0 * ef_amp180) if ef_amp180 else 0.0
    swing = contrast / reference_contrast if reference_contrast and contrast else 0.0
    resolved = periods >= MIN_RESOLVED_PERIODS
    doubled = 1.6 <= ratio <= 2.5 or 0.4 <= ratio <= 0.625
    if resolved and (swing >= MIN_LADDER_SWING or not (doubled or swing)):
        log.warning(
            "%s: the 1-2 pi amplitude fitted to %.4g against the %.4g a sqrt(2) ladder "
            "implies from the 0-1 amplitude of %.4g — %.2fx. Accepted, because the sweep "
            "resolves %.1f full oscillations and swings %s of `rabi`'s contrast: a drive "
            "too weak to turn a pi shows less than one oscillation, never more, and cannot "
            "move the population that far however the ladder reads. This is a measurement "
            "the ladder does not describe rather than a fit of a partial rotation, so the "
            "ladder is the more likely thing to be wrong. `fine_amplitude_12` amplifies "
            "what is left",
            target,
            ef_amp180,
            expected,
            amp180,
            ratio,
            periods,
            f"{swing:.2f}x" if swing else "an unmeasured fraction",
        )
        return
    lengths = (
        ""
        if abs(stretch - 1.0) < 1e-9
        else (
            f" The two pulses are not the same length — {rxy_duration * 1e9:.0f} ns "
            f"against {ef_duration * 1e9:.0f} ns — so the 1-2 pulse needs "
            f"{stretch:.2g}x the amplitude for the same area; setting `rabi_12.duration` "
            f"to the 0-1 pulse's length would put the pi at {expected / stretch:.4g}."
        )
    )
    # With the sweep attached: whether 0.0675 is this oscillation's fundamental or a
    # harmonic of a non-sinusoidal readout is a question one look at the trace settles,
    # and three runs of this refusal in a row could not answer it.
    raise RoutineError(
        f"the 1-2 pi amplitude fitted to {ef_amp180:.4g} against the {expected:.4g} that "
        f"the 0-1 amplitude of {amp180:.4g} implies — {ratio:.2f}x, outside the "
        f"{1 / MAX_EF_LADDER_ERROR:.1f}-{MAX_EF_LADDER_ERROR:.0f}x a transmon's sqrt(2) "
        "ladder allows. A cosine fitted to a partial rotation reports a smaller amplitude "
        "than a pi pulse, so this is most likely a 1-2 drive too weak to turn one: check "
        f"clock_freqs.f12 is the transition, and widen the amplitude sweep. The sweep "
        f"resolves {periods:.2f} of an oscillation, under the "
        f"{MIN_RESOLVED_PERIODS:g} that would make this a measurement rather than an "
        f"extrapolated arc."
        f"{lengths}{_contrast_reading(contrast, reference_contrast)}",
        fit=fit,
    )


def _contrast_reading(contrast: float, reference_contrast: float) -> str:
    """The one reading that separates the two ways this guard can fire.

    This routine maps ``|2>`` back through a 0-1 pi before reading, so an oscillation
    genuinely turning a 1-2 pi swings the *full* readout contrast — the same one `rabi`
    measured. Much smaller, and the sweep found something too weak to be a pi, which is
    what the refusal above assumes. Comparable, and the population is moving as far as
    `rabi` moves it, which a weak drive cannot do at any ladder ratio.

    Said here rather than acted on, because reaching this function means the swing was
    already too small to accept — see :data:`MIN_LADDER_SWING`. What is left is telling
    an operator by how much, and against what.
    """
    if not contrast:
        return ""
    if not reference_contrast:
        return (
            f" This sweep's contrast is {contrast:.4g}, and `rabi` did not record its own "
            f"to compare against — so whether this is a weak drive or a pi the ladder "
            f"mispredicts cannot be settled from here. Re-run `rabi` first."
        )
    return (
        f" This sweep swings {contrast:.4g} against `rabi`'s {reference_contrast:.4g} — "
        f"{contrast / reference_contrast:.2f}x, under the {MIN_LADDER_SWING:.2f}x that "
        f"would make it a full population transfer. So the drive is not turning a pi "
        f"between any two levels, which is why the amplitude is being read as too weak "
        f"rather than as a chip the ladder does not describe."
    )


def _rxy_duration(element: Any) -> float:
    """The 0-1 pulse's length, or zero if this element will not say."""
    try:
        return float(read_path(element, "rxy.duration"))
    except Exception:  # noqa: BLE001 - an unreadable duration is not evidence
        return 0.0
