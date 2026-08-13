"""Single-qubit gate calibration: Rabi through fine amplitude (RFC 0004 §3).

Each routine sweeps one axis and fits the response, and each writes exactly one
device parameter — except AllXY, which is a diagnostic and writes none.
"""

from typing import Any

import numpy as np
import xarray as xr

from qpi_driver.tuners.base.backend import SchedulerBackend
from qpi_driver.tuners.base.config import RoutineConfig
from qpi_driver.tuners.base.device import drag_parameter_name, read_path, write_path
from qpi_driver.tuners.base.limits import full_scale
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


class Rabi(CalibrationRoutine):
    """Sweep drive amplitude to find the π pulse (Vion et al., Science 296, 886)."""

    name = "rabi"
    depends_on = ("qubit_spectroscopy",)
    updates = ("rxy.amp180",)

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
        self._amplitudes = setpoints_of(
            config,
            "amplitudes",
            linear_setpoints(
                0.0, 0.5 * full_scale(device.get_element(target), "rxy.amp180"), 41
            ),
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
        write_path(device.get_element(target), "rxy.amp180", params["amp180"])

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

    name = "ramsey"
    depends_on = ("rabi",)
    updates = ("clock_freqs.f01",)
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
        """Widen the delays and try again when the fit says the decay was never seen.

        A window too short for this chip is the commonest way this node fails, and the
        guard already knows it — see `CalibrationRoutine.escalating`.
        """
        return self.escalating(target, device, config, backend, timeout_s)

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
        return fitted

    def apply(self, device: Any, target: str, params: dict[str, Any]) -> None:
        write_path(
            device.get_element(target), "clock_freqs.f01", params["clock_freq_01"]
        )


class T1(CalibrationRoutine):
    """Relaxation: excite, wait, measure."""

    name = "t1"
    depends_on = ("rabi",)
    updates = ()

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
        self._delays = setpoints_of(config, "delays", linear_setpoints(0.0, 100e-6, 41))
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


class T2Echo(CalibrationRoutine):
    """Hahn echo: a refocusing π cancels static dephasing (Bylander et al., Nat. Phys. 7, 565)."""

    name = "t2_echo"
    depends_on = ("rabi",)
    updates = ()

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
        self._delays = setpoints_of(config, "delays", linear_setpoints(0.0, 100e-6, 41))
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
        return fit_t2(np.asarray(self._delays), signal_of(dataset))


class Drag(CalibrationRoutine):
    """DRAG: sweep the Motzoi parameter to cancel leakage phase (Motzoi et al., PRL 103, 110501)."""

    name = "drag"
    depends_on = ("ramsey",)
    # Spelled `rxy.beta` under qblox — see `drag_parameter_name`.
    updates = ("rxy.motzoi",)

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
        self._betas = setpoints_of(
            config,
            "motzois",
            linear_setpoints(-backend.drag_span, backend.drag_span, 31),
        )
        schedule = backend.new_schedule(
            self.name, repetitions=int(config.get("shots", 1024))
        )
        # X90-Y180 against Y90-X180: the two sequences are equal only at the
        # right beta, so their difference crosses zero there and is linear about it.
        for index, beta in enumerate(self._betas):
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
        if signal.size < 2 * len(self._betas):
            raise RoutineError(
                f"DRAG expected {2 * len(self._betas)} acquisitions, got {signal.size}"
            )
        paired = signal[: 2 * len(self._betas)].reshape(-1, 2)
        return fit_drag(np.asarray(self._betas), paired[:, 0] - paired[:, 1])

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

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        schedule = backend.new_schedule(
            self.name, repetitions=int(config.get("shots", 1024))
        )
        for index, (first, second) in enumerate(ALLXY_PAIRS):
            schedule.add(backend.Reset(target))
            for theta, phi in (first, second):
                if theta:
                    schedule.add(backend.Rxy(theta=theta, phi=phi, qubit=target))
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
        if signal.size < len(ALLXY_PAIRS):
            raise RoutineError(
                f"AllXY expected {len(ALLXY_PAIRS)} acquisitions, got {signal.size}"
            )
        measured = signal[: len(ALLXY_PAIRS)]
        ideal = np.asarray(ALLXY_IDEAL)

        # Against the sequence's *own* reference points, not its min and max.
        #
        # Two reasons, and the first is a correctness bug. Min-max normalisation
        # assumes the smallest reading is |0> and the largest is |1>, which is only
        # true when the readout happens to make |z| rise with excitation. Whether it
        # rises or falls depends on which side of the resonator's line the readout
        # sits, and `resonator_spectroscopy` puts it on the ground-state resonance —
        # where |1> reflects *less*. Inverted, this compared a descending response
        # against an ascending staircase and reported an rms deviation of 0.65 on a
        # well-calibrated qubit. The five |0> pairs and four |1> pairs are in the
        # sequence precisely so it can normalise itself, and dividing by their
        # difference carries the sign.
        #
        # Second, averaging nine reference points is steadier than trusting the two
        # most extreme readings in the set, which is what min-max does.
        ground = float(np.mean(measured[ideal == 0.0]))
        excited = float(np.mean(measured[ideal == 1.0]))
        contrast = excited - ground
        if abs(contrast) < 1e-12:
            raise RoutineError(
                "AllXY's |0> and |1> reference pairs read the same, so the qubit is "
                "not responding and there is nothing to normalise against"
            )
        # Deliberately unclipped: a point outside the reference range is a real error
        # signal, and min-max normalisation threw exactly that information away by
        # construction.
        normalised = (measured - ground) / contrast

        deviation = normalised - ideal
        return {
            "rms_deviation": float(np.sqrt(np.mean(deviation**2))),
            "max_deviation": float(np.max(np.abs(deviation))),
            "normalised_response": [float(v) for v in normalised],
        }


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
    reads = ("rxy.amp180",)

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

        return fit_fine_amplitude(
            np.asarray(self._repetitions, dtype=float),
            signal[:count],
            self._current_amp180,
            ground=float(signal[count]),
            excited=float(signal[count + 1]),
        )

    def apply(self, device: Any, target: str, params: dict[str, Any]) -> None:
        write_path(device.get_element(target), "rxy.amp180", params["amp180"])
