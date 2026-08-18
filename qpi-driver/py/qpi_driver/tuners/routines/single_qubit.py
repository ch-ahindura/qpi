"""Single-qubit gate calibration: Rabi through fine amplitude (RFC 0004 §3).

Each routine sweeps one axis and fits the response, and each writes exactly one
device parameter — except AllXY, which is a diagnostic and writes none.
"""

from typing import Any

import logging
from collections.abc import Sequence
import math

import numpy as np
import xarray as xr

from qpi_driver.tuners.base.backend import SchedulerBackend
from qpi_driver.tuners.base.fusion import add_after, add_together
from qpi_driver.tuners.base.config import RoutineConfig
from qpi_driver.executors.base.rotations import (
    QUARTER_TURN_DEGREES,
    amplitude_for_angle,
)
from qpi_driver.tuners.base.device import (
    drag_parameter_name,
    measured_t1,
    read_path,
    readout_contrast_path,
    relaxation_time_path,
    write_path,
)
from qpi_driver.tuners.base.limits import full_scale
from qpi_driver.tuners.fitting.core import MIN_FIT_POINTS, OutOfRange
from qpi_driver.tuners.base.routines import (
    DEFAULT_ROUTINE_TIMEOUT_S,
    CalibrationRoutine,
    CheckOutcome,
    RoutineError,
    grid_duration,
    linear_setpoints,
    setpoints_of,
)
from qpi_driver.tuners.fitting import (
    fit_drag,
    fit_fine_amplitude,
    fit_rabi,
    fit_ramsey,
    fit_t1,
    fit_t2,
    signal_of,
)

log = logging.getLogger(__name__)

#: The 21 gate pairs of the AllXY sequence, in Reed's order (Yale thesis, 2013).
#: The ideal response is a staircase: five points at |0>, twelve at the
#: equator, four at |1>. Deviations from it name the miscalibration.
ALLXY_PAIRS: tuple[tuple[tuple[float, float], tuple[float, float]], ...] = (
    ((0, 0), (0, 0)),
    ((180, 0), (180, 0)),
    ((180, 90), (180, 90)),
    ((180, 0), (180, 90)),
    ((180, 90), (180, 0)),
    ((90, 0), (0, 0)),
    ((90, 90), (0, 0)),
    ((90, 0), (90, 90)),
    ((90, 90), (90, 0)),
    ((90, 0), (180, 90)),
    ((90, 90), (180, 0)),
    ((180, 0), (90, 90)),
    ((180, 90), (90, 0)),
    ((90, 0), (180, 0)),
    ((180, 0), (90, 0)),
    ((90, 90), (180, 90)),
    ((180, 90), (90, 90)),
    ((180, 0), (0, 0)),
    ((180, 90), (0, 0)),
    ((90, 0), (90, 0)),
    ((90, 90), (90, 90)),
)

#: The staircase AllXY should produce, normalised to [0, 1].
ALLXY_IDEAL: tuple[float, ...] = (0.0,) * 5 + (0.5,) * 12 + (1.0,) * 4

#: Last-resort coherence sweep, for a chip with no measured T1 to scale one from.
DEFAULT_COHERENCE_WINDOW_S = 100e-6

#: The longest echo sweep worth running, in multiples of T1 — the ceiling escalation may
#: widen the delays to.
#:
#: Six, which is three time constants of the *longest T2 physics allows*: a Hahn echo
#: cannot outlast ``2*T1``, so a window past ``3 * 2*T1`` cannot be resolving the echo
#: however unresolved the fit still looks. What lives out there is drift — a readout
#: wandering over a sweep that takes minutes looks exactly like a slow decay and has no
#: reason to respect T1.
#:
#: Without it, escalation walked the 2026-08-16 B chip's window from 148 us to 355 us
#: chasing a curve it could resolve, found one, and reported 148 us of T2 against a
#: 49.4 us T1 — three times a bound nothing can exceed.
MAX_ECHO_WINDOW_IN_T1 = 6.0

#: Multiples of T1 to sweep a Hahn echo over — see :meth:`T2Echo._window`.
#:
#: A Hahn echo refocuses static dephasing and nothing else, so ``T2 <= 2*T1`` bounds it and
#: a window has to clear that bound to constrain the fit rather than truncate it. Three
#: puts the ceiling at 2/3 of the sweep, leaving the decay visibly flattened before the
#: last point on any chip whose T2 is anywhere in its allowed range — which is the property
#: a fixed duration cannot have, since it is right only for the T1 it was chosen against.
T2_WINDOW_IN_T1 = 3.0

#: How far AllXY's ``|0>`` and ``|1>`` reference plateaus must stand apart, in units of the
#: scatter *within* the plateaus, before the sequence is measuring anything.
#:
#: The contrast between those two groups is AllXY's own normalisation denominator, so when
#: it is noise the normalisation divides by noise and manufactures a full-scale response
#: out of nothing. On the August 2026 B chip, whose qubit was never excited, that produced
#: a "normalised response" ranging -22 to +12.5 and an rms deviation of 9.65 — reported as
#: success. `allxy_check` turned the same data into a *fidelity* of 0.533 and offered it to
#: the drift check, which is the number a threshold would have believed.
#:
#: Three sigma because the plateaus average five and four points: two random groups of that
#: size differ by about 0.7 of their own scatter, so three separates noise from any real
#: contrast without demanding a good gate — a badly calibrated one still has full readout
#: contrast between ``|0>`` and ``|1>``, which is exactly what AllXY then measures the shape
#: of.
MIN_ALLXY_CONTRAST = 3.0


def normalised_allxy(measured: np.ndarray) -> np.ndarray:
    """*measured* on the ``[0, 1]`` scale `ALLXY_IDEAL` is written on.

    Against the sequence's *own* reference points, not its min and max.

    Two reasons, and the first is a correctness bug. Min-max normalisation assumes the
    smallest reading is ``|0>`` and the largest is ``|1>``, which is only true when the
    readout happens to make ``|z|`` rise with excitation. Whether it rises or falls depends
    on which side of the resonator's line the readout sits, and `resonator_spectroscopy`
    puts it on the ground-state resonance — where ``|1>`` reflects *less*. Inverted, this
    compared a descending response against an ascending staircase and reported an rms
    deviation of 0.65 on a well-calibrated qubit. The five ``|0>`` pairs and four ``|1>``
    pairs are in the sequence precisely so it can normalise itself, and dividing by their
    difference carries the sign.

    Second, averaging nine reference points is steadier than trusting the two most extreme
    readings in the set, which is what min-max does.

    Raises:
        RoutineError: if the two reference plateaus are not separated by more than the
            scatter within them. The contrast is the denominator, so without this the
            normalisation divides by noise — see :data:`MIN_ALLXY_CONTRAST`.
    """
    ideal = np.asarray(ALLXY_IDEAL)
    ground_points = measured[ideal == 0.0]
    excited_points = measured[ideal == 1.0]
    ground = float(np.mean(ground_points))
    excited = float(np.mean(excited_points))
    contrast = excited - ground

    # Pooled about each plateau's own mean, so a real staircase is not counted as scatter.
    residuals = np.concatenate(
        [
            ground_points - ground,
            measured[ideal == 0.5] - float(np.mean(measured[ideal == 0.5])),
            excited_points - excited,
        ]
    )
    scatter = float(np.std(residuals))
    if abs(contrast) < MIN_ALLXY_CONTRAST * scatter:
        raise RoutineError(
            f"AllXY's |0> and |1> reference pairs are {abs(contrast):.4g} apart against a "
            f"scatter of {scatter:.4g} within the plateaus — {abs(contrast) / scatter:.1f}x, "
            f"below the {MIN_ALLXY_CONTRAST:g}x a responding qubit clears. The sequence "
            "normalises against that contrast, so there is nothing to divide by and any "
            "deviation reported from it would be noise scaled to full range"
        )

    # Deliberately unclipped: a point outside the reference range is a real error signal,
    # and min-max normalisation threw exactly that information away by construction.
    return (measured - ground) / contrast


class Rabi(CalibrationRoutine):
    """Sweep drive amplitude to find the π pulse (Vion et al., Science 296, 886)."""

    name = "rabi"
    depends_on = ("qubit_spectroscopy",)
    updates = ("rxy.amp180", "resonator.contrast")
    reads = ("clock_freqs.f01",)

    def measure(
        self,
        target: str,
        device: Any,
        config: RoutineConfig,
        backend: SchedulerBackend,
        bias: Any = None,
        timeout_s: float = DEFAULT_ROUTINE_TIMEOUT_S,
    ) -> dict[str, Any]:
        """Reach further when the fit says the pi pulse was above the sweep."""
        return self.escalating(target, device, config, backend, timeout_s)

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        # Half scale by default, and *escalating* to full scale rather than starting
        # there. Both bounds are real and they pull against each other.
        #
        # Reach: a sweep stopping at 0.5 cannot find a pi pulse above it, and
        # `require_in_range` will not say so — it checks the fitted value lies inside the
        # swept range, which fires the same way for a value too small. A chip whose own
        # working calibration used 0.5683 returned a flat Rabi every run and wrote an
        # amplitude that left X rotating five degrees.
        #
        # Accuracy: the fit is a cosine, and a strongly driven transmon stops being one —
        # population leaks to |2> and the oscillation is no longer what is being fitted.
        # Sweeping straight to full scale put amp180 6.9% out on the simulated chip where
        # half scale lands within 1%, and `rabi_12` moved off its sqrt(2) ladder entirely.
        #
        # So: measure where the model holds, and reach further only when the fit says the
        # pi pulse is not in there. `full_scale` is the ceiling on that reaching, because
        # a waveform past it clips.
        # Recorded for `_widened` to clamp against, under the same `_<axis>` convention it
        # already reads setpoints by. Without it escalation walks straight past full scale.
        self._amplitudes_ceiling = full_scale(device.get_element(target), "rxy.amp180")
        self._amplitudes = setpoints_of(
            config,
            "amplitudes",
            linear_setpoints(0.0, 0.5 * self._amplitudes_ceiling, 41),
        )
        schedule = backend.new_schedule(
            self.name, repetitions=int(config.get("shots", 1024))
        )
        for index, amplitude in enumerate(self._amplitudes):
            schedule.add(backend.Reset(target))
            schedule.add(backend.Rxy(theta=180, phi=0, qubit=target, amp180=amplitude))
            schedule.add(
                backend.Measure(
                    target, acq_index=index, bin_mode=backend.BinMode.AVERAGE
                )
            )
        return schedule

    def analyse(
        self, dataset: xr.Dataset, target: str, device: Any, config: RoutineConfig
    ) -> dict[str, Any]:
        return fit_rabi(np.asarray(self._amplitudes), signal_of(dataset))

    def apply(self, device: Any, target: str, params: dict[str, Any]) -> None:
        element = device.get_element(target)
        write_path(element, "rxy.amp180", params["amp180"])
        # For `rabi_12`'s ladder guard, which cannot otherwise tell a 1-2 drive too weak
        # to turn a pi from one whose pi is simply not where the ladder predicts.
        path = readout_contrast_path(element)
        if path is not None and params.get("contrast"):
            write_path(element, path, float(params["contrast"]))

    #: Where a `CalibratedTransmon` keeps its separately measured pi/2 amplitude.

    #: Repetitions of the pi pulse the check amplifies the error over, and the
    #: rotation error it tolerates, in radians. Five pulses turn a 3-degree error
    #: into a 15-degree one, which is the point: a single pi pulse is *second*
    #: order in its own error, so playing one and reading the population cannot
    #: distinguish a pulse 5% short from a readout whose gain moved 5%.
    CHECK_REPETITIONS = 5
    CHECK_MAX_ROTATION_ERROR = 0.05

    def build_check_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        """Amplify any error in the stored `amp180` over a few repetitions.

        Three acquisitions against the sweep's forty-one, and a different
        experiment rather than a narrower one:

        - two references, ``|0>`` and ``X|0>``, so the verdict is a *fraction* of
          the measured contrast and survives a change in readout gain;
        - then ``X90`` followed by N pi pulses. The pre-rotation is what makes the
          error first order — without it the signal goes as ``cos(n*delta)`` and is
          flat at ``delta = 0`` — and N is what amplifies it. This is
          `fine_amplitude`'s trick, borrowed at one setpoint.
        """
        repetitions = int(config.get("check_repetitions", self.CHECK_REPETITIONS))
        schedule = backend.new_schedule(
            f"{self.name}_check", repetitions=int(config.get("check_shots", 512))
        )
        for index, prepare in enumerate((0, 1)):
            schedule.add(backend.Reset(target))
            if prepare:
                schedule.add(backend.X(target))
            schedule.add(
                backend.Measure(
                    target, acq_index=index, bin_mode=backend.BinMode.AVERAGE
                )
            )

        schedule.add(backend.Reset(target))
        schedule.add(backend.Rxy(theta=90, phi=0, qubit=target))
        for _ in range(repetitions):
            schedule.add(backend.X(target))
        schedule.add(
            backend.Measure(target, acq_index=2, bin_mode=backend.BinMode.AVERAGE)
        )
        self._check_repetitions = repetitions
        return schedule

    def analyse_check(
        self, dataset: xr.Dataset, target: str, device: Any, config: RoutineConfig
    ) -> CheckOutcome:
        """Recover the per-pulse rotation error from the amplified sequence.

        ``P(n) = 0.5 * (1 + (-1)^n * sin(n * delta))`` — the model
        `fit_fine_amplitude` uses — so the amplified point sits at exactly half the
        contrast when the pulse is right, and its distance from there gives
        ``delta`` directly.
        """
        signal = signal_of(dataset)
        if signal.size < 3:
            raise RoutineError(
                f"pi-pulse check expected 3 acquisitions, got {signal.size}"
            )
        ground, excited, amplified = (float(signal[i]) for i in range(3))
        contrast = excited - ground
        if abs(contrast) < 1e-9:
            raise RoutineError(
                "pi-pulse check saw no contrast between |0> and |1>, so it cannot "
                "say whether the pulse inverted — the readout is the suspect here, "
                "not the amplitude"
            )

        # On the ground-to-excited axis, so a change in readout gain cancels.
        fraction = (amplified - ground) / contrast
        repetitions = getattr(self, "_check_repetitions", self.CHECK_REPETITIONS)
        deviation = min(abs(2.0 * (fraction - 0.5)), 1.0)
        error = float(np.arcsin(deviation) / max(repetitions, 1))

        tolerance = float(
            config.get("check_max_rotation_error", self.CHECK_MAX_ROTATION_ERROR)
        )
        return CheckOutcome(
            passed=error <= tolerance,
            margin=error / max(tolerance, 1e-12),
            detail=(
                f"{np.rad2deg(error):.2f} deg per-pulse rotation error over "
                f"{repetitions} pulses"
            ),
        )


class Ramsey(CalibrationRoutine):
    """Ramsey interferometry: refine f01 and measure T2* (Ramsey, Phys. Rev. 78, 695)."""

    #: How many times to re-measure after correcting f01.
    #:
    #: Each pass multiplies the residual by roughly the fractional error of the last, so
    #: three is far more than convergence needs and is here as a bound rather than a
    #: target — the loop normally stops on `_detuning_floor` after one or two.
    MAX_REFINEMENTS = 3

    name = "ramsey"
    depends_on = ("rabi",)
    updates = ("clock_freqs.f01",)
    reads = ("clock_freqs.f01", "rxy.amp180")

    def measure(
        self,
        target: str,
        device: Any,
        config: RoutineConfig,
        backend: SchedulerBackend,
        bias: Any = None,
        timeout_s: float = DEFAULT_ROUTINE_TIMEOUT_S,
    ) -> dict[str, Any]:
        """Refine f01 until the residual detuning is under what this sweep can resolve.

        One pass is not enough, and the reason is in `analyse`: the correction is
        ``current_f01 - detuning``, and the detuning was measured *with the old f01* in the
        drive. Get it wrong by a megahertz and the fringe you fitted was a megahertz off
        resonance, so the correction lands near the answer rather than on it. Each pass
        starts from where the last left the device and measures what remains, so the
        residual falls geometrically.

        Iterating rather than accepting the first answer is what RFC 0007 §12 recorded as
        worth doing and §11.5 then needed: on the B chip a single pass moved f01 by
        1.032 MHz and left an AllXY whose whole error was in the equator block, which reads
        as either a residual detuning or a pi/2 amplitude error. A second pass measures the
        first of those directly, so the ambiguity is settled by the graph rather than by
        the operator.

        Bounded three ways. It stops when the detuning is below what the sweep can resolve
        — see `_detuning_floor`, which derives that from the window rather than guessing a
        constant. It stops after `MAX_REFINEMENTS` whatever happens. And each pass is a
        full `escalating` call, so a window too short for this chip is still widened by the
        guard that already knows how.
        """
        refined = self.escalating(target, device, config, backend, timeout_s)
        # Zero when nothing has built a schedule yet, and then there is no bias to compare
        # against and no sign to resolve — the same reading `_detuning_floor` gives an
        # unswept `_delays`.
        artificial = float(getattr(self, "_detuning", 0.0) or 0.0)
        if artificial and abs(float(refined.get("detuning", 0.0))) >= artificial:
            refined = self._resolved_root(
                target, device, config, backend, timeout_s, refined
            )
        floor = self._detuning_floor(config)
        for _attempt in range(self.MAX_REFINEMENTS):
            if abs(float(refined.get("detuning", 0.0))) <= floor:
                break
            # Applied here so the next pass drives at the corrected frequency, which is the
            # whole mechanism. The DAG applies again afterwards, and a write is idempotent.
            self.apply(device, target, refined)
            again = self.escalating(target, device, config, backend, timeout_s)
            if abs(float(again.get("detuning", 0.0))) >= abs(
                float(refined.get("detuning", 0.0))
            ):
                # Not converging: the residual is no smaller than what we started this pass
                # with, so another pass measures noise. Keep the better of the two.
                log.info(
                    "%s on %s: detuning stopped falling at %.0f Hz, keeping it",
                    self.name,
                    target,
                    abs(float(refined.get("detuning", 0.0))),
                )
                break
            refined = again
        return refined

    def _resolved_root(
        self,
        target: str,
        device: Any,
        config: RoutineConfig,
        backend: SchedulerBackend,
        timeout_s: float,
        fitted: dict[str, Any],
    ) -> dict[str, Any]:
        """Which of the fringe's two roots is this chip's, measured rather than assumed.

        The fringe oscillates at ``|residual + artificial|``, so both ``+fringe`` and
        ``-fringe`` solve it and only one is the chip's. The artificial detuning is what
        picks between them — but only while it is the *larger* of the two, which is the
        precondition nothing was checking. Once the residual exceeds it the sign is not
        recoverable from one sweep, and the wrong root moves f01 further off than leaving
        it alone: on the August 2026 B chip it wrote 5.311817 GHz where that chip's own
        working calibration says 5.317995, while the other root lands 60 kHz from it.

        So put f01 on each root, measure what remains, and keep the one that leaves less.
        Costs one extra sweep, and only when the residual says the sign is in doubt.
        """
        others = {**fitted, "clock_freq_01": fitted["clock_freq_01_alternative"]}
        measured = []
        for candidate in (fitted, others):
            self.apply(device, target, candidate)
            measured.append(self.escalating(target, device, config, backend, timeout_s))
        best = min(measured, key=lambda pass_: abs(float(pass_.get("detuning", 0.0))))
        log.info(
            "%s on %s: fringe %.0f Hz against a %.0f Hz artificial detuning leaves the "
            "sign ambiguous; the two roots left %s Hz, keeping %.0f",
            self.name,
            target,
            float(fitted.get("fringe_frequency", 0.0)),
            self._detuning,
            " and ".join(f"{abs(float(m.get('detuning', 0.0))):.0f}" for m in measured),
            abs(float(best.get("detuning", 0.0))),
        )
        return best

    def _detuning_floor(self, config: RoutineConfig) -> float:
        """The smallest detuning this sweep could tell from zero, in Hz.

        A fringe frequency fitted over a window ``T`` is resolved to about ``1/(2*pi*T)``,
        so a residual below that is not a measurement of anything and another pass would
        chase noise. Derived from the operator's own delays rather than set as a constant,
        which is the same reasoning `_confirm_points` uses: their sweep is their statement
        about the resolution their chip needs.
        """
        delays = [float(d) for d in getattr(self, "_delays", ()) or ()]
        if not delays:
            return 0.0
        window = max(delays) - min(delays)
        return 1.0 / (2.0 * math.pi * window) if window > 0 else 0.0

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        # 601 points from 4 ns to 24 us, and both ends are load-bearing — this sweep has
        # to satisfy two constraints at once, which is why it cannot be small.
        #
        # Fine enough. The fringe is the *residual* detuning plus the artificial one, and
        # spectroscopy leaves a residual of several MHz — its line is Fourier-limited by a
        # 20 ns pulse, so it lands the frequency to within about ten. At 40 ns steps
        # Nyquist is 12.5 MHz, which covers that; the 250 ns steps this used to default to
        # put it at 2 MHz, and a 9 MHz fringe folded down to something slow and plausible.
        # RFC 0007's acceptance test found exactly that, reporting T2* = 1361 seconds.
        #
        # Long enough. The fit refuses a T2* the window never saw, so a sweep shorter than
        # the coherence cannot measure it: 24 us against the simulated chip's 20 us.
        #
        # Escalation cannot substitute for either, and that is the point. It moves one axis
        # per attempt, and this sweep being wrong in *both* directions at once — too coarse
        # and too short — is a state it cannot walk out of: widening for the unfinished
        # decay coarsens the step that was already aliasing. 601 acquisitions is inside the
        # sequencer's ceiling of about 950, so one sweep can satisfy both, and it is what an
        # operator had to supply by hand until now.
        #
        # On the grid, as `ramsey_12` does: a delay that is not a whole number of
        # nanoseconds does not compile. Gridded here rather than on the way into the
        # schedule because `analyse` fits against these same numbers.
        self._delays = [
            grid_duration(delay)
            for delay in setpoints_of(
                config, "delays", linear_setpoints(4e-9, 24e-6, 601)
            )
        ]
        self._detuning = float(config.get("artificial_detuning", 1e6))
        # The clock this run corrects, read before the acquisition rather than after it.
        self._current_f01 = float(
            read_path(device.get_element(target), "clock_freqs.f01")
        )
        schedule = backend.new_schedule(
            self.name, repetitions=int(config.get("shots", 1024))
        )
        for index, delay in enumerate(self._delays):
            # The second π/2 is phase-advanced rather than the clock detuned, so
            # the fringe is deliberate and its direction known.
            phase = 360.0 * self._detuning * delay
            schedule.add(backend.Reset(target))
            schedule.add(backend.Rxy(theta=90, phi=0, qubit=target))
            backend.idle(schedule, delay)
            schedule.add(backend.Rxy(theta=90, phi=phase % 360.0, qubit=target))
            schedule.add(
                backend.Measure(
                    target, acq_index=index, bin_mode=backend.BinMode.AVERAGE
                )
            )
        return schedule

    def analyse(
        self, dataset: xr.Dataset, target: str, device: Any, config: RoutineConfig
    ) -> dict[str, Any]:
        fitted = fit_ramsey(
            np.asarray(self._delays), signal_of(dataset), self._detuning
        )
        fitted["clock_freq_01"] = self._current_f01 - fitted["detuning"]
        # A fringe frequency is a magnitude, so `-fringe - artificial` is the residual
        # just as consistently as `+fringe - artificial`. Carried alongside rather than
        # chosen here: which root is the chip's takes another sweep to find out, and
        # `_resolved_root` is where that happens.
        fitted["clock_freq_01_alternative"] = self._current_f01 + (
            fitted["fringe_frequency"] + self._detuning
        )
        return fitted

    def apply(self, device: Any, target: str, params: dict[str, Any]) -> None:
        write_path(
            device.get_element(target), "clock_freqs.f01", params["clock_freq_01"]
        )


class T1(CalibrationRoutine):
    """Relaxation: excite, wait, measure."""

    name = "t1"
    depends_on = ("rabi",)
    # Not a calibration — nothing plays differently because of it — but `t2_echo` needs it
    # as a ceiling, and it was being measured and thrown away. See `CoherenceTimes`.
    updates = ("coherence.t1",)
    reads = ("clock_freqs.f01", "rxy.amp180")

    def measure(
        self,
        target: str,
        device: Any,
        config: RoutineConfig,
        backend: SchedulerBackend,
        bias: Any = None,
        timeout_s: float = DEFAULT_ROUTINE_TIMEOUT_S,
    ) -> dict[str, Any]:
        """Widen the delays and try again when the fit says the decay was never seen.

        A window too short for this chip is the commonest way this node fails, and the
        guard already knows it — see `CalibrationRoutine.escalating`.
        """
        return self.escalating(target, device, config, backend, timeout_s)

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        self._delays = setpoints_of(
            config, "delays", linear_setpoints(0.0, DEFAULT_COHERENCE_WINDOW_S, 41)
        )
        schedule = backend.new_schedule(
            self.name, repetitions=int(config.get("shots", 1024))
        )
        for index, delay in enumerate(self._delays):
            schedule.add(backend.Reset(target))
            schedule.add(backend.X(target))
            backend.idle(schedule, delay)
            schedule.add(
                backend.Measure(
                    target, acq_index=index, bin_mode=backend.BinMode.AVERAGE
                )
            )
        return schedule

    def analyse(
        self, dataset: xr.Dataset, target: str, device: Any, config: RoutineConfig
    ) -> dict[str, Any]:
        return fit_t1(np.asarray(self._delays), signal_of(dataset))

    def apply(self, device: Any, target: str, params: dict[str, Any]) -> None:
        """Keep T1 where `t2_echo` can find it, when the element has somewhere for it.

        Opt-in like every other `CalibratedTransmon` field: a plain `BasicTransmonElement`
        has no ``coherence`` submodule, and `fit_t2` then does without the ceiling exactly
        as it did before.
        """
        element = device.get_element(target)
        if relaxation_time_path(element):
            write_path(element, "coherence.t1", params["t1"])


class T2Echo(CalibrationRoutine):
    """Hahn echo: a refocusing π cancels static dephasing (Bylander et al., Nat. Phys. 7, 565)."""

    name = "t2_echo"
    # On `t1` as well as `rabi`, for the ceiling rather than for a pulse: `fit_t2` cannot
    # tell an unconstrained decay from a long-lived one without it. See `CoherenceTimes`.
    depends_on = ("rabi", "t1")
    updates = ()
    reads = ("clock_freqs.f01", "rxy.amp180", "coherence.t1")

    def measure(
        self,
        target: str,
        device: Any,
        config: RoutineConfig,
        backend: SchedulerBackend,
        bias: Any = None,
        timeout_s: float = DEFAULT_ROUTINE_TIMEOUT_S,
    ) -> dict[str, Any]:
        """Widen the delays and try again when the fit says the decay was never seen.

        A window too short for this chip is the commonest way this node fails, and the
        guard already knows it — see `CalibrationRoutine.escalating`.
        """
        return self.escalating(target, device, config, backend, timeout_s)

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        # Snapped so that *half* a delay lands on the grid, because that is what `idle`
        # is given. A window scaled from a measured T1 divides into steps of no particular
        # length — 73.82 us of T1 gave 5536.857838 ns — and the schedule then compiles
        # right up until qblox refuses a time value, in a routine that looks fine.
        # Read by `_widened` under the `_<axis>_ceiling` convention, so escalation cannot
        # widen past what physics allows — see :data:`MAX_ECHO_WINDOW_IN_T1`.
        t1 = measured_t1(device.get_element(target))
        self._delays_ceiling = (
            MAX_ECHO_WINDOW_IN_T1 * t1
            if t1
            else MAX_ECHO_WINDOW_IN_T1 * DEFAULT_COHERENCE_WINDOW_S / T2_WINDOW_IN_T1
        )
        self._delays = [
            2.0 * grid_duration(delay / 2.0)
            for delay in setpoints_of(
                config,
                "delays",
                linear_setpoints(0.0, self._window(device, target), 41),
            )
        ]
        schedule = backend.new_schedule(
            self.name, repetitions=int(config.get("shots", 1024))
        )
        for index, delay in enumerate(self._delays):
            schedule.add(backend.Reset(target))
            schedule.add(backend.Rxy(theta=90, phi=0, qubit=target))
            backend.idle(schedule, delay / 2)
            schedule.add(backend.Rxy(theta=180, phi=0, qubit=target))
            backend.idle(schedule, delay / 2)
            schedule.add(backend.Rxy(theta=90, phi=0, qubit=target))
            schedule.add(
                backend.Measure(
                    target, acq_index=index, bin_mode=backend.BinMode.AVERAGE
                )
            )
        return schedule

    def analyse(
        self, dataset: xr.Dataset, target: str, device: Any, config: RoutineConfig
    ) -> dict[str, Any]:
        return fit_t2(
            np.asarray(self._delays),
            signal_of(dataset),
            t1=measured_t1(device.get_element(target)),
        )

    def _window(self, device: Any, target: str) -> float:
        """How long to sweep, in units of the relaxation this echo refocuses through.

        A fixed window measures whatever the chip in front of it happens to have. An echo
        can reach ``2*T1`` and a decay is only constrained once the sweep passes it, so a
        window has to be a multiple of T1 rather than a duration: on a 60 us T1 the 100 us
        constant here stops before ``2*T1``, and `fit_t2` then refuses an unbounded decay
        that more shots cannot bound. The same argument RFC 0005 §13 makes for sizing the
        readout sweep in linewidths.

        Falls back to the constant when `t1` has not run, which is also what makes this
        safe on an element with nowhere to keep one.
        """
        t1 = measured_t1(device.get_element(target))
        return T2_WINDOW_IN_T1 * t1 if t1 else DEFAULT_COHERENCE_WINDOW_S


class Drag(CalibrationRoutine):
    """DRAG: sweep the Motzoi parameter to cancel leakage phase (Motzoi et al., PRL 103, 110501)."""

    name = "drag"
    depends_on = ("ramsey",)
    # Spelled `rxy.beta` under qblox — see `drag_parameter_name`.
    updates = ("rxy.motzoi",)
    reads = ("clock_freqs.f01", "rxy.amp180")

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

        What `drag_12` already does, and for the same reason: the default is
        `SchedulerBackend.drag_span` either side of zero, which is a statement about the
        units rather than about a chip. The August 2026 B chip's 0-1 optimum came out at
        -0.4803 against a range of +/-0.2, so the node refused a fit that had found its
        answer — and everything downstream of `drag` then ran on an uncorrected pulse.
        """
        return self.escalating(target, device, config, backend, timeout_s)

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        # The DRAG parameter's *units differ between the two schedulers*, so the
        # default sweep cannot be a constant here — see `SchedulerBackend.drag_span`.
        # quantify's `motzoi` is a dimensionless ratio; qblox's `beta` is the same
        # quantity multiplied by the pulse sigma, so in seconds. A sweep sized for
        # one is nine orders of magnitude wrong for the other, and being wrong in
        # the large direction does not merely mis-fit: it pushes the derivative
        # term past full scale and the schedule stops compiling.
        self._motzois = setpoints_of(
            config,
            "motzois",
            linear_setpoints(-backend.drag_span, backend.drag_span, 31),
        )
        schedule = backend.new_schedule(
            self.name, repetitions=int(config.get("shots", 1024))
        )
        # X90-Y180 against Y90-X180: the two sequences are equal only at the
        # right beta, so their difference crosses zero there and is linear about it.
        for index, beta in enumerate(self._motzois):
            schedule.add(backend.Reset(target))
            override = {backend.drag_parameter: beta}
            schedule.add(backend.Rxy(theta=90, phi=0, qubit=target, **override))
            schedule.add(backend.Rxy(theta=180, phi=90, qubit=target, **override))
            schedule.add(
                backend.Measure(
                    target, acq_index=2 * index, bin_mode=backend.BinMode.AVERAGE
                )
            )
            schedule.add(backend.Reset(target))
            schedule.add(backend.Rxy(theta=90, phi=90, qubit=target, **override))
            schedule.add(backend.Rxy(theta=180, phi=0, qubit=target, **override))
            schedule.add(
                backend.Measure(
                    target, acq_index=2 * index + 1, bin_mode=backend.BinMode.AVERAGE
                )
            )
        return schedule

    def analyse(
        self, dataset: xr.Dataset, target: str, device: Any, config: RoutineConfig
    ) -> dict[str, Any]:
        signal = signal_of(dataset)
        if signal.size < 2 * len(self._motzois):
            raise RoutineError(
                f"DRAG expected {2 * len(self._motzois)} acquisitions, got {signal.size}"
            )
        paired = signal[: 2 * len(self._motzois)].reshape(-1, 2)
        # Named, so a refusal is escalatable rather than prose — see `measure`.
        element = device.get_element(target)
        # ``rxy.motzoi`` under quantify, ``rxy.beta`` under qblox — see `apply`.
        name = drag_parameter_name(element)
        return fit_drag(
            np.asarray(self._motzois),
            paired[:, 0] - paired[:, 1],
            axis="motzois",
            current=float(read_path(element, f"rxy.{name}")) if name else 0.0,
        )

    def apply(self, device: Any, target: str, params: dict[str, Any]) -> None:
        element = device.get_element(target)
        name = drag_parameter_name(element)
        if name is None:
            raise RoutineError(
                f"{target} has no DRAG parameter to write the fitted optimum to"
            )
        write_path(element, f"rxy.{name}", params["motzoi"])


class AllXY(CalibrationRoutine):
    """The 21-pair diagnostic — validates the 1Q gates without changing them."""

    name = "allxy"
    depends_on = ("drag",)
    updates = ()
    reads = ("clock_freqs.f01", "rxy.amp180")

    def build_group_schedule(
        self,
        targets: Sequence[str],
        device: Any,
        config: RoutineConfig,
        backend: SchedulerBackend,
    ) -> Any:
        """The 21 pairs on every target at once — the sequence is the same on each."""
        schedule = backend.new_schedule(
            self.name, repetitions=int(config.get("shots", 1024))
        )
        for index, (first, second) in enumerate(ALLXY_PAIRS):
            anchor = add_together(schedule, [backend.Reset(t) for t in targets])
            for theta, phi in (first, second):
                if theta:
                    anchor = add_after(
                        schedule,
                        [backend.Rxy(theta=theta, phi=phi, qubit=t) for t in targets],
                        anchor,
                    )
            add_after(
                schedule,
                [
                    backend.Measure(
                        target,
                        acq_channel=channel,
                        acq_index=index,
                        bin_mode=backend.BinMode.AVERAGE,
                    )
                    for channel, target in enumerate(targets)
                ],
                anchor,
            )
        return schedule

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        return self.build_group_schedule([target], device, config, backend)

    def analyse(
        self, dataset: xr.Dataset, target: str, device: Any, config: RoutineConfig
    ) -> dict[str, Any]:
        signal = signal_of(dataset)
        if signal.size < len(ALLXY_PAIRS):
            raise RoutineError(
                f"AllXY expected {len(ALLXY_PAIRS)} acquisitions, got {signal.size}"
            )
        measured = signal[: len(ALLXY_PAIRS)]
        normalised = normalised_allxy(measured)

        deviation = normalised - np.asarray(ALLXY_IDEAL)
        return {
            "rms_deviation": float(np.sqrt(np.mean(deviation**2))),
            "max_deviation": float(np.max(np.abs(deviation))),
            "normalised_response": [float(v) for v in normalised],
        }


#: How many times a fine-amplitude sweep may be shortened before giving up.
#:
#: Each pass cuts the accumulated rotation to roughly a radian, so two is already an
#: eightfold reduction from a sweep that overran by that much. A third would be measuring
#: a pulse so far out that `rabi` upstream is the thing to fix.
MAX_SHORTENINGS = 2


def amplified(
    routine: CalibrationRoutine,
    target: str,
    device: Any,
    config: RoutineConfig,
    backend: SchedulerBackend,
    timeout_s: float,
    step: int,
) -> dict[str, Any]:
    """Run *routine*, shortening its repetitions if the rotation outran its own model.

    The fit linearises ``sin(n*d)`` as ``n*d``, so how many repetitions it can take
    depends on how big ``d`` turns out to be — which is the thing being measured. There is
    no default that is right in advance: the August 2026 B chip needed 25 for its pi and
    could not take 13 for its pi/2, on the same run.

    So the refusal names the shortening it wants and this applies it, which is escalation
    running downward. `_widened` declines the direction on purpose; the ladder is *step*
    and rebuilding it is what a generic stretch cannot do.
    """
    for attempt in range(MAX_SHORTENINGS + 1):
        try:
            return routine.escalating(target, device, config, backend, timeout_s)
        except OutOfRange as refusal:
            counts = [
                int(n)
                for n in (
                    config.get("repetitions")
                    or getattr(routine, "_repetitions", ())
                    or ()
                )
            ]
            shorter = _shortened(counts, refusal.factor, step)
            if refusal.direction != "shorter":
                raise
            if (
                attempt == MAX_SHORTENINGS
                or len(shorter) < MIN_FIT_POINTS
                or shorter == counts
            ):
                # Out of ladder, not out of measurement. The rotation outran the linear
                # model at every length this can reach, which says the pulse under test is
                # far enough off that amplification saturates immediately — a real finding
                # about the chip. What it does *not* license is a correction, since the
                # slope it would come from is the one the model could not describe.
                #
                # So the prior stands and the node reports. Refusing instead published
                # nothing and took every node behind it down: `fine_amplitude_12` refused
                # on all six runs of the August 2026 B chip, and `r12`'s pi went unrefined
                # the whole time for want of a number this already had.
                log.warning(
                    "%s on %s: %s — keeping the existing amplitude rather than correcting "
                    "from a fit its own model does not describe",
                    routine.name,
                    target,
                    refusal,
                )
                return routine.uncorrected(device, target)
            log.info(
                "%s on %s: %s — repeating %d times instead of %d (%d of %d)",
                routine.name,
                target,
                refusal,
                max(shorter),
                max(counts),
                attempt + 1,
                MAX_SHORTENINGS,
            )
            config = RoutineConfig(
                enabled=config.enabled,
                params={**config.params, "repetitions": shorter},
            )
    raise RoutineError(  # pragma: no cover - the loop above always returns or raises
        f"{routine.name} exhausted its shortenings on {target}"
    )


def _shortened(counts: list[int], factor: float, step: int) -> list[int]:
    """*counts* rebuilt no longer than *factor* of their reach, on the same ladder.

    The ladder is why this is not `_widened`'s job. A generic stretch interpolates, and
    both of these sweeps have a shape interpolation breaks: the pi sweep needs whole
    repetitions, and the pi/2 sweep needs odd ones or the error it is amplifying does not
    lie along the axis being measured. Rebuilding from *step* keeps both.

    Floored at `MIN_FIT_POINTS` points rather than at two. Two is what a slope needs and
    four is what `align` takes, so the old floor let a shortening produce a ladder the fit
    then refused — and the routine had spent its retry to arrive at a refusal about its own
    sweep instead of about the chip.
    """
    top = max(int(max(counts) * factor), 1 + step * (MIN_FIT_POINTS - 1))
    return list(range(1, top + 1, step))


class FineAmplitude(CalibrationRoutine):
    """Amplify a small amplitude error by repeating the π pulse.

    The π/2 pre-rotation is not decoration: without it the response is
    ``cos(n(π+δ))``, which at integer ``n`` is identical for an over- and an
    under-rotation. The pre-rotation turns it into a sine, and the sign of the
    correction becomes measurable.
    """

    name = "fine_amplitude"
    depends_on = ("drag",)
    updates = ("rxy.amp180",)
    reads = ("rxy.amp180", "clock_freqs.f01")

    def measure(
        self,
        target: str,
        device: Any,
        config: RoutineConfig,
        backend: SchedulerBackend,
        bias: Any = None,
        timeout_s: float = DEFAULT_ROUTINE_TIMEOUT_S,
    ) -> dict[str, Any]:
        """Shorten the sweep if 25 repetitions turn further than the fit can linearise.

        On the August 2026 B chip they turned 2.3 radians — a full swing of the sine,
        fitted as a straight line, and written to the amplitude every X pulse plays at.
        """
        return amplified(self, target, device, config, backend, timeout_s, step=1)

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        self._repetitions = [
            int(n) for n in setpoints_of(config, "repetitions", list(range(1, 26)))
        ]
        # The amplitude this run refines, read before the acquisition rather than after
        # it — it is what every X below is played at, so reading it later described a
        # sweep that had already happened.
        self._current_amp180 = float(
            read_path(device.get_element(target), "rxy.amp180")
        )
        schedule = backend.new_schedule(
            self.name, repetitions=int(config.get("shots", 1024))
        )
        for index, count in enumerate(self._repetitions):
            schedule.add(backend.Reset(target))
            schedule.add(backend.Rxy(theta=90, phi=0, qubit=target))
            for _ in range(count):
                schedule.add(backend.X(target))
            schedule.add(
                backend.Measure(
                    target, acq_index=index, bin_mode=backend.BinMode.AVERAGE
                )
            )

        # Two reference points, |0> and |1>, so the fit knows the full contrast.
        # Without them only the product of contrast and rotation error is
        # recoverable, and the error comes out scaled by whatever fraction of
        # the contrast this sweep happened to cover.
        reference = len(self._repetitions)
        schedule.add(backend.Reset(target))
        schedule.add(
            backend.Measure(
                target, acq_index=reference, bin_mode=backend.BinMode.AVERAGE
            )
        )
        schedule.add(backend.Reset(target))
        schedule.add(backend.X(target))
        schedule.add(
            backend.Measure(
                target, acq_index=reference + 1, bin_mode=backend.BinMode.AVERAGE
            )
        )
        return schedule

    def analyse(
        self, dataset: xr.Dataset, target: str, device: Any, config: RoutineConfig
    ) -> dict[str, Any]:
        signal = signal_of(dataset)
        count = len(self._repetitions)
        if signal.size < count + 2:
            raise RoutineError(
                f"fine amplitude expected {count + 2} acquisitions "
                f"(sweep plus two calibration points), got {signal.size}"
            )

        fitted = fit_fine_amplitude(
            np.asarray(self._repetitions, dtype=float),
            signal[:count],
            self._current_amp180,
            ground=float(signal[count]),
            excited=float(signal[count + 1]),
        )
        return {"amp180": fitted["amplitude"], **fitted}

    def uncorrected(self, device: Any, target: str) -> dict[str, Any]:
        current = float(read_path(device.get_element(target), "rxy.amp180"))
        return {
            "amp180": current,
            "amplitude": current,
            "error_per_pulse": 0.0,
            "amplitude_error": 0.0,
            "unresolved": 1.0,
        }

    def apply(self, device: Any, target: str, params: dict[str, Any]) -> None:
        write_path(device.get_element(target), "rxy.amp180", params["amp180"])


AMP90_PATH = "fine.amp90"

#: Repetition counts that amplify a pi/2 error linearly.
#:
#: Every fourth, and it is the model rather than a preference: after ``4k+1`` quarter
#: turns the state is back on the equator with the accumulated error along the measured
#: axis, so the response is ``(1 + sin(n*d))/2``. At ``4k+3`` the quadratures have
#: swapped and at even counts the response is flat in the error to first order — see
#: `fit_fine_amplitude`, which refuses the wrong counts rather than fitting them.
#:
#: Short on purpose. The slope fit linearises ``sin(n*d)``, so it needs ``n*d`` inside
#: about a radian; the B chip's equator block implies ``d`` near 0.2, which 13 pulses
#: already stretch to 2.6. The refinement below is what makes that converge, and it is
#: cheaper to iterate four short sweeps than to fit one long one through a sine's turn.
DEFAULT_AMP90_REPETITIONS = (1, 5, 9, 13)


class FineAmplitude90(CalibrationRoutine):
    """Amplify a small error in the pi/2 pulse by repeating it (RFC 0007 §11.5).

    `fine_amplitude` refines the pi pulse and nothing refines the pi/2, because until
    `fine.amp90` existed there was nowhere to write one: both schedulers derive every
    angle from ``amp180`` by linear interpolation, so a pi/2 was *defined* as half a pi
    and could not be wrong. Past half of full scale the amplifier compresses and it is,
    and the error is invisible to everything else in the graph — randomised benchmarking
    averages a coherent over-rotation into its depolarising rate, and Rabi and
    `fine_amplitude` both measure the pi.

    AllXY sees it, which is how it was found: on the August 2026 B chip the two plateaus
    were flat to 0.009 while the equator block carried an antisymmetric +0.1795 that
    survived the residual detuning being driven to -384 Hz. But AllXY writes nothing.
    This measures the same error directly and writes it.

    No pre-rotation, unlike `fine_amplitude`. There the pulse under test is the pi and a
    pi/2 in front of it turns an even response into a signed one; here the pulse under
    test *is* the pi/2, and starting at ``|0>`` with ``4k+1`` of them puts the state on
    the equator by itself. A pre-rotation would have to be played by the very pulse being
    calibrated, so its error would enter twice and the fit could not tell the two apart.
    """

    #: How many times to re-measure after correcting amp90. Same bound and same reason as
    #: `Ramsey.MAX_REFINEMENTS`, and here it is load-bearing rather than belt-and-braces:
    #: the first pass of a badly-set pi/2 is biased low by the sine it linearises.
    MAX_REFINEMENTS = 3

    #: Stop when a pass moves the amplitude by less than this fraction of itself. Below a
    #: per mille the correction is smaller than the shot noise on a 1024-shot sweep, so
    #: another pass would be measuring the readout.
    CONVERGED_FRACTION = 1e-3

    name = "fine_amplitude_90"
    depends_on = ("fine_amplitude",)
    updates = (AMP90_PATH,)
    reads = ("rxy.amp180", AMP90_PATH, "clock_freqs.f01")

    def applies_to(self, device: Any, target: str) -> bool:
        """Only an element with somewhere to put the answer.

        A `BasicTransmonElement` keeps the interpolated pi/2 it always had. That is not a
        misconfiguration — it is every device config written before this element existed.
        """
        element = device.get_element(target)
        submodule = getattr(element, "fine", None)
        return submodule is not None and hasattr(submodule, "amp90")

    def measure(
        self,
        target: str,
        device: Any,
        config: RoutineConfig,
        backend: SchedulerBackend,
        bias: Any = None,
        timeout_s: float = DEFAULT_ROUTINE_TIMEOUT_S,
    ) -> dict[str, Any]:
        """Refine amp90 until a pass stops moving it.

        One pass is not enough here for a different reason than `ramsey`'s. The fit takes
        the slope of ``sin(n*d)`` as ``d``, which is exact only for small ``n*d``; an
        uncalibrated pi/2 starts far enough out that the first pass under-reports and
        corrects most of the way rather than all of it. Each pass then plays the corrected
        amplitude — the element's own factory sees to that — and measures what remains, so
        what is left shrinks into the range the linearisation is exact in.

        Bounded the same three ways as `ramsey`: by convergence, by the correction
        becoming smaller than the noise, and by `MAX_REFINEMENTS`.
        """
        refined = self._pass(target, device, config, backend, timeout_s)
        # Carry forward whatever the first pass settled on, so a sweep that had to be
        # shortened is not rediscovered — and paid for — on every pass after it.
        # `build_schedule` leaves the counts it used here.
        config = RoutineConfig(
            enabled=config.enabled,
            params={**config.params, "repetitions": list(self._repetitions)},
        )
        for _attempt in range(self.MAX_REFINEMENTS):
            previous = float(refined["amp90"])
            # Applied here so the next pass plays the corrected pi/2, which is the whole
            # mechanism. The DAG applies again afterwards, and a write is idempotent.
            self.apply(device, target, refined)
            again = self._pass(target, device, config, backend, timeout_s)
            moved = abs(float(again["amp90"]) - previous) / max(previous, 1e-12)
            refined = again
            if moved <= self.CONVERGED_FRACTION:
                break
            log.info(
                "%s on %s: pass moved amp90 by %.2f%%, refining again",
                self.name,
                target,
                100 * moved,
            )
        return refined

    def _pass(self, target, device, config, backend, timeout_s) -> dict[str, Any]:
        """One refinement pass, shortened if the rotation outran the linearisation.

        Every fourth count, because only after ``4k+1`` quarter turns does the accumulated
        error lie along the axis being measured — see `DEFAULT_AMP90_REPETITIONS`.
        """
        # Odd, not every fourth. The default ladder is 4k+1 because that keeps the
        # demodulation at +1 throughout, which is easier to read — but what the guard
        # actually requires is ``cos(n*pi/2) == 0``, and that holds for every odd n with
        # the demodulation alternating instead. Shortening on 4 has nowhere to go: [1, 5,
        # 9, 13] is already the shortest four-point ladder it allows, so a rotation that
        # overran could only be cut to something `align` refuses. On 2 the same four points
        # become [1, 3, 5, 7] — 0.81 rad where 13 pulses gave 1.51, which is the difference
        # between refining this pulse and refusing it.
        return amplified(self, target, device, config, backend, timeout_s, step=2)

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        self._repetitions = [
            int(n)
            for n in setpoints_of(
                config, "repetitions", list(DEFAULT_AMP90_REPETITIONS)
            )
        ]
        # What the compiler will actually play for a 90, which is not amp180/2 once this
        # has run once. Read before the acquisition rather than after it, for the reason
        # `fine_amplitude` states: it is the amplitude every pulse below is played at.
        element = device.get_element(target)
        self._current_amp90 = amplitude_for_angle(
            QUARTER_TURN_DEGREES,
            float(read_path(element, "rxy.amp180")),
            float(read_path(element, AMP90_PATH) or 0.0),
        )

        schedule = backend.new_schedule(
            self.name, repetitions=int(config.get("shots", 1024))
        )
        for index, count in enumerate(self._repetitions):
            schedule.add(backend.Reset(target))
            for _ in range(count):
                schedule.add(backend.Rxy(theta=90, phi=0, qubit=target))
            schedule.add(
                backend.Measure(
                    target, acq_index=index, bin_mode=backend.BinMode.AVERAGE
                )
            )

        # |0> and |1>, so the fit knows the full contrast rather than whatever fraction
        # of it this sweep reached. See `fit_fine_amplitude`.
        reference = len(self._repetitions)
        schedule.add(backend.Reset(target))
        schedule.add(
            backend.Measure(
                target, acq_index=reference, bin_mode=backend.BinMode.AVERAGE
            )
        )
        schedule.add(backend.Reset(target))
        schedule.add(backend.X(target))
        schedule.add(
            backend.Measure(
                target, acq_index=reference + 1, bin_mode=backend.BinMode.AVERAGE
            )
        )
        return schedule

    def analyse(
        self, dataset: xr.Dataset, target: str, device: Any, config: RoutineConfig
    ) -> dict[str, Any]:
        signal = signal_of(dataset)
        count = len(self._repetitions)
        if signal.size < count + 2:
            raise RoutineError(
                f"fine amplitude 90 expected {count + 2} acquisitions "
                f"(sweep plus two calibration points), got {signal.size}"
            )

        fitted = fit_fine_amplitude(
            np.asarray(self._repetitions, dtype=float),
            signal[:count],
            self._current_amp90,
            ground=float(signal[count]),
            excited=float(signal[count + 1]),
            turn=math.pi / 2,
            pre_rotation=0.0,
        )
        return {"amp90": fitted["amplitude"], **fitted}

    def uncorrected(self, device: Any, target: str) -> dict[str, Any]:
        current = float(read_path(device.get_element(target), AMP90_PATH))
        return {
            "amp90": current,
            "amplitude": current,
            "error_per_pulse": 0.0,
            "amplitude_error": 0.0,
            "unresolved": 1.0,
        }

    def apply(self, device: Any, target: str, params: dict[str, Any]) -> None:
        write_path(device.get_element(target), AMP90_PATH, params["amp90"])
