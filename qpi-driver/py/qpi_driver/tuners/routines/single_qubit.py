"""Single-qubit gate calibration: Rabi through fine amplitude (RFC 0004 §3).

Each routine sweeps one axis and fits the response, and each writes exactly one
device parameter — except AllXY, which is a diagnostic and writes none.
"""

from typing import Any

import numpy as np
import xarray as xr

from qpi_driver.tuners.base.backend import SchedulerBackend
from qpi_driver.tuners.base.config import RoutineConfig
from qpi_driver.tuners.base.device import read_path, write_path
from qpi_driver.tuners.base.routines import (
    CalibrationRoutine,
    RoutineError,
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

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        self._amplitudes = setpoints_of(
            config, "amplitudes", linear_setpoints(0.0, 0.5, 41)
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


class Ramsey(CalibrationRoutine):
    """Ramsey interferometry: refine f01 and measure T2* (Ramsey, Phys. Rev. 78, 695)."""

    name = "ramsey"
    depends_on = ("rabi",)
    updates = ("clock_freqs.f01",)

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        self._delays = setpoints_of(config, "delays", linear_setpoints(4e-9, 10e-6, 41))
        self._detuning = float(config.get("artificial_detuning", 1e6))
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
        current = float(read_path(device.get_element(target), "clock_freqs.f01"))
        fitted["clock_freq_01"] = current - fitted["detuning"]
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
    updates = ("rxy.motzoi",)

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        # The DRAG parameter is in *seconds*: it scales a time derivative of the
        # pulse envelope, so its size is set by the pulse, not by the amplitude.
        # A calibrated 20 ns gate wants something of order 1e-11 — the lab's own
        # device file carries -5.4e-11 and -1.5e-11 — and the sweep has to
        # bracket that. It used to run -1.0 to 1.0, eleven orders of magnitude
        # out, which puts the derivative term so far above the carrier that the
        # waveform exceeds full scale and the schedule will not compile at all.
        self._betas = setpoints_of(
            config, "motzois", linear_setpoints(-2e-10, 2e-10, 31)
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
        write_path(device.get_element(target), "rxy.motzoi", params["motzoi"])


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

        low, high = float(np.min(measured)), float(np.max(measured))
        if high - low < 1e-12:
            raise RoutineError("AllXY response is flat — the qubit is not responding")
        normalised = (measured - low) / (high - low)

        deviation = normalised - np.asarray(ALLXY_IDEAL)
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

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        self._repetitions = [
            int(n) for n in setpoints_of(config, "repetitions", list(range(1, 26)))
        ]
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

        current = float(read_path(device.get_element(target), "rxy.amp180"))
        return fit_fine_amplitude(
            np.asarray(self._repetitions, dtype=float),
            signal[:count],
            current,
            ground=float(signal[count]),
            excited=float(signal[count + 1]),
        )

    def apply(self, device: Any, target: str, params: dict[str, Any]) -> None:
        write_path(device.get_element(target), "rxy.amp180", params["amp180"])
