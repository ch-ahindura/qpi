"""Spectroscopy: finding the resonator, its readout power, and the qubit (RFC 0004 §3).

These are the roots of the DAG — everything downstream needs a frequency to
drive at. Each sweeps a clock frequency across the scan and fits a Lorentzian to
the response.
"""

from typing import Any

import xarray as xr

from qpi_driver.tuners.base.backend import SchedulerBackend
from qpi_driver.tuners.base.config import RoutineConfig
from qpi_driver.tuners.base.device import read_path, write_path
from qpi_driver.tuners.base.routines import (
    CalibrationRoutine,
    CheckOutcome,
    RoutineError,
    grid_duration,
    linear_setpoints,
    setpoints_of,
)
from qpi_driver.tuners.fitting import (
    fit_punchout,
    fit_qubit_spectroscopy,
    fit_readout_timing,
    fit_resonator_spectroscopy,
    signal_of,
)


def _frequency_sweep(
    config: RoutineConfig, device: Any, target: str, clock: str, default_span: float
) -> list[float]:
    """The frequencies to scan, either given outright or as a span about the current one.

    A span is the useful form for a recalibration — the frequency has drifted a
    little, so scan around where it was — while an explicit range is what a
    first bring-up needs, when there is no trustworthy current value.
    """
    if "frequencies" in config:
        return setpoints_of(config, "frequencies", [])

    centre = config.get("centre_frequency")
    if centre is None:
        centre = _current_clock(device, target, clock)
    span = float(config.get("span", default_span))
    points = int(config.get("points", 51))
    return linear_setpoints(centre - span / 2, centre + span / 2, points)


def _require_resolved_line(linewidth: float, frequencies: list[float]) -> None:
    """Refuse a line the sweep was too coarse to have seen.

    A Lorentzian narrower than the spacing between setpoints did not appear in
    the data: whatever the fit converged on came from noise between the points,
    and it comes with a small linewidth and a confident centre. That is the
    worst shape a wrong answer can take here, because the centre is written
    straight to the device as f01 and every gate afterwards is driven at it.

    Seen in practice: narrowing the line to 63 kHz while the sweep still stepped
    5 MHz made the routine report a frequency 377 MHz from the qubit, with a
    tidy fit and no complaint.

    Raises:
        RoutineError: naming both numbers, since the fix is a finer sweep.
    """
    if len(frequencies) < 2:
        return
    step = abs(frequencies[1] - frequencies[0])
    if linewidth < step:
        raise RoutineError(
            f"fitted linewidth {linewidth:.4g} Hz is narrower than the "
            f"{step:.4g} Hz spacing of the sweep, so the line was never "
            "measured — the fit is of the noise between setpoints. Scan the "
            "same span with more points, or narrow the span."
        )


def _current_clock(device: Any, target: str, clock: str) -> float:
    """The frequency currently configured for *clock* on *target*."""
    value = read_path(device.get_element(target), f"clock_freqs.{clock}")
    if value is None or value != value:  # None or NaN
        raise RoutineError(
            f"{target}.{clock} has no current value to scan around; set an explicit "
            f"'centre_frequency' or 'frequencies' for this routine"
        )
    return float(value)


class _ReadoutTraceRoutine(CalibrationRoutine):
    """Shared base for the two routines that read one raw acquisition (RFC 0005 §7).

    Both capture a `Trace` with the acquisition window opened at the readout pulse
    and both call `fit_readout_timing`, because the arrival time and the fill time
    constant cannot be measured independently — see that function. They are separate
    nodes because they write different parameters and drift for different reasons:
    ``acq_delay`` is a property of the cabling, ``integration_time`` of the
    resonator.

    ``acq_delay`` is set to zero for the measurement itself, which is the whole
    trick: opening the window with the pulse is what puts the dead time inside the
    trace where it can be seen. Leaving the configured delay in place would hide
    exactly the quantity being measured.
    """

    #: Samples per second the digitiser records a trace at. Qblox's rate; a chip on
    #: other hardware overrides it through ``sampling_rate``.
    SAMPLING_RATE = 1e9

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        element = device.get_element(target)
        self._restore = {
            "measure.acq_delay": read_path(element, "measure.acq_delay"),
            "measure.integration_time": read_path(element, "measure.integration_time"),
        }
        window = float(config.get("window", 2e-6))
        write_path(element, "measure.acq_delay", 0.0)
        write_path(element, "measure.integration_time", window)

        schedule = backend.new_schedule(
            self.name, repetitions=int(config.get("shots", 1024))
        )
        schedule.add(backend.Reset(target))
        schedule.add(
            backend.Measure(
                target,
                acq_index=0,
                acq_protocol="Trace",
                bin_mode=backend.BinMode.AVERAGE,
            )
        )
        return schedule

    def analyse(
        self, dataset: xr.Dataset, target: str, device: Any, config: RoutineConfig
    ) -> dict[str, Any]:
        try:
            trace = _trace_of(dataset)
            rate = float(config.get("sampling_rate", self.SAMPLING_RATE))
            return fit_readout_timing(trace, rate)
        finally:
            # Whatever the fit did, the device must not be left with the zero delay
            # and the long window this routine set to make its own measurement
            # possible. `apply` writes the calibrated value over the restored one.
            element = device.get_element(target)
            for path, value in self._restore.items():
                write_path(element, path, value)


class TimeOfFlight(_ReadoutTraceRoutine):
    """How long a readout signal takes to come back, so the window opens when it does.

    A root of the graph: it needs no calibrated parameter, only a readout pulse and a
    raw trace, and everything that acquires afterwards wants the delay it writes.
    Hand-set until now — 200 ns in the reference config, with nothing measuring it.
    """

    name = "time_of_flight"
    # The reference pipelines put `tof` at the very root, before any resonator is
    # known. That works for a reflection measurement, where a mismatched resonator
    # sends back plenty off resonance. What this simulator models — and what a
    # transmission geometry does — returns signal only *near* resonance, so a trace
    # taken at a wrong readout frequency is noise and the fit rightly refuses it.
    # Hence the dependency: find the resonator, then time the flight to it.
    depends_on = ("resonator_spectroscopy",)
    updates = ("measure.acq_delay",)

    def apply(self, device: Any, target: str, params: dict[str, Any]) -> None:
        # On the grid, because the fit reports an arrival to a fraction of a sample
        # and `acq_delay` shifts the acquisition. An unrounded value compiles here and
        # makes every *later* schedule fail with a complaint about a time value —
        # measured: 149.327 ns took out punchout, qubit spectroscopy, Rabi, Ramsey,
        # T1 and flux spectroscopy, none of which is the routine at fault.
        write_path(
            device.get_element(target),
            "measure.acq_delay",
            grid_duration(params["time_of_flight"]),
        )


class ResonatorRelaxation(_ReadoutTraceRoutine):
    """How fast the resonator fills — its linewidth, in the time domain.

    A characterisation rather than a calibration, and deliberately so. The obvious
    parameter to write is ``measure.integration_time``, and the ring-up is only a
    *floor* on it: the optimum trades signal-to-noise against relaxation during the
    window, which needs the discrimination fidelity of RFC 0005 phase 4 to measure.
    Three time constants would have shortened the reference config's 1 µs window to
    240 ns on the strength of a criterion that never mentions noise.

    What it reports is the linewidth, and nothing else measures that.
    `resonator_spectroscopy` fits one from its Lorentzian and discards it, and that
    routine's own check currently scales its tolerance by a constant for want of
    somewhere to keep it — see RFC 0005 §13.

    Depends on `resonator_spectroscopy` because a resonator interrogated off its own
    resonance rings up towards a smaller settled level, and a fit of that reports how
    the drive overlaps the line rather than the resonator's own width.
    """

    name = "resonator_relaxation"
    depends_on = ("resonator_spectroscopy",)
    updates = ()


def _trace_of(dataset: Any) -> Any:
    """The one raw trace in *dataset*, as a complex 1-D array.

    `signal_of` is the wrong reducer here: it takes the magnitude and flattens, and
    a trace fit needs the complex samples in time order rather than a scalar per
    acquisition.
    """
    import numpy as np

    values: Any = dataset
    data_vars = getattr(dataset, "data_vars", None)
    if data_vars is not None:
        names = list(data_vars)
        if not names:
            raise RoutineError("the trace acquisition returned no data variables")
        values = dataset[names[0]]
    array = np.asarray(getattr(values, "values", values)).reshape(-1)
    if array.size < 16:
        raise RoutineError(
            f"expected a raw trace, got {array.size} sample(s) — the acquisition "
            "protocol was not Trace, so there is no timing to read"
        )
    return array


class ResonatorSpectroscopy(CalibrationRoutine):
    """Sweep the readout clock and find the resonator (Koch et al., PRA 76, 042319)."""

    name = "resonator_spectroscopy"
    depends_on = ()
    updates = ("clock_freqs.readout",)

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        self._frequencies = _frequency_sweep(
            config, device, target, "readout", default_span=20e6
        )
        clock = f"{target}.ro"
        schedule = backend.new_schedule(
            self.name, repetitions=int(config.get("shots", 1024))
        )
        for index, frequency in enumerate(self._frequencies):
            schedule.add(backend.Reset(target))
            schedule.add(
                backend.SetClockFrequency(clock=clock, clock_freq_new=frequency)
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
        return fit_resonator_spectroscopy(self._frequencies, signal_of(dataset))

    def apply(self, device: Any, target: str, params: dict[str, Any]) -> None:
        write_path(
            device.get_element(target),
            "clock_freqs.readout",
            params["readout_frequency"],
        )

    #: The readout linewidth the check judges an offset against, in Hz, and how
    #: much of it the configured frequency may sit away from the peak.
    #:
    #: A constant with a config override rather than a value read from the device,
    #: because there *is* no device field for it: `fit_resonator_spectroscopy`
    #: measures a linewidth and nothing stores it, since neither scheduler's
    #: transmon has somewhere to put it. Reading a path no element has would raise
    #: `ParameterError`, the check would be unevaluable, and — because an
    #: unevaluable check is deliberately not evidence of drift — it would report
    #: nothing, forever, in silence. Set ``check_linewidth`` per chip; giving the
    #: resonator a linewidth field of its own is RFC 0005 §13.
    CHECK_LINEWIDTH_HZ = 2e6
    CHECK_MAX_OFFSET_LINEWIDTHS = 0.35

    def build_check_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        """Three points across the line: is the configured frequency still its peak?

        This is the check that makes a readout drift *visible*. RFC 0004 excluded
        the readout chain from any partial recalibration on the grounds that
        re-running it is expensive — which is true, and left a drift there with no
        symptom other than every downstream fit quietly getting worse.

        Three acquisitions rather than the sweep's fifty-one, and no fit: the
        stored frequency should read higher than a probe either side of it. A
        parabola through the three gives where the peak actually is, which is
        enough to say *how far* off without locating it precisely.
        """
        # One linewidth either side. Wider and the parabola stops describing the
        # top of the line; narrower and readout noise dominates the difference
        # between the three points.
        span = float(config.get("check_span", 0.0)) or 2.0 * self._linewidth(config)
        centre = _current_clock(device, target, "readout")
        self._check_frequencies = [centre - span / 2, centre, centre + span / 2]
        self._check_span = span

        clock = f"{target}.ro"
        schedule = backend.new_schedule(
            f"{self.name}_check", repetitions=int(config.get("check_shots", 1024))
        )
        for index, frequency in enumerate(self._check_frequencies):
            schedule.add(backend.Reset(target))
            schedule.add(
                backend.SetClockFrequency(clock=clock, clock_freq_new=frequency)
            )
            schedule.add(
                backend.Measure(
                    target, acq_index=index, bin_mode=backend.BinMode.AVERAGE
                )
            )
        return schedule

    def analyse_check(
        self, dataset: xr.Dataset, target: str, device: Any, config: RoutineConfig
    ) -> CheckOutcome:
        signal = signal_of(dataset)
        if signal.size < 3:
            raise RoutineError(
                f"readout check expected 3 acquisitions, got {signal.size}"
            )
        low, centre, high = (float(signal[i]) for i in range(3))
        step = self._check_span / 2.0

        # Vertex of the parabola through the three points, in units of *step*.
        denominator = low - 2.0 * centre + high
        if abs(denominator) < 1e-12:
            raise RoutineError(
                "readout check saw a flat response across the line, so it cannot "
                "say where the peak is"
            )
        shift = 0.5 * (low - high) / denominator
        offset = abs(float(shift) * step)

        linewidth = self._linewidth(config)
        fraction = float(
            config.get("check_max_offset_linewidths", self.CHECK_MAX_OFFSET_LINEWIDTHS)
        )
        tolerance = fraction * linewidth
        return CheckOutcome(
            passed=offset <= tolerance,
            margin=offset / max(tolerance, 1e-9),
            detail=(
                f"readout sits {offset / 1e3:.0f} kHz from the peak "
                f"({offset / max(linewidth, 1e-9):.2f} linewidths)"
            ),
        )

    def _linewidth(self, config: RoutineConfig) -> float:
        """The linewidth this check's probe spacing and tolerance scale with, in Hz."""
        return float(config.get("check_linewidth", self.CHECK_LINEWIDTH_HZ))


class ResonatorPunchout(CalibrationRoutine):
    """Sweep readout power to find the edge of the dressed regime.

    Writes the readout *frequency* as well as the power, and has to: the resonance
    moves with power — that movement is the experiment — so a new power leaves the
    frequency `resonator_spectroscopy` measured at the old one pointing at where
    the resonator used to be. Half a linewidth off on the simulated chip, costing
    a fifth of the readout contrast for every routine downstream, silently.

    It costs no extra measurement. This sweep fits a resonator spectrum at every
    power in its range, the selected one included; the fix is to stop discarding
    that row.
    """

    name = "resonator_punchout"
    depends_on = ("resonator_spectroscopy",)
    updates = ("measure.pulse_amp", "clock_freqs.readout")

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        self._powers = setpoints_of(
            config, "amplitudes", linear_setpoints(0.01, 0.5, 11)
        )
        self._frequencies = _frequency_sweep(
            config, device, target, "readout", default_span=20e6
        )
        clock = f"{target}.ro"
        schedule = backend.new_schedule(
            self.name, repetitions=int(config.get("shots", 512))
        )
        index = 0
        for power in self._powers:
            for frequency in self._frequencies:
                schedule.add(backend.Reset(target))
                schedule.add(
                    backend.SetClockFrequency(clock=clock, clock_freq_new=frequency)
                )
                schedule.add(
                    backend.Measure(
                        target,
                        acq_index=index,
                        bin_mode=backend.BinMode.AVERAGE,
                        pulse_amp=power,
                    )
                )
                index += 1
        return schedule

    def analyse(
        self, dataset: xr.Dataset, target: str, device: Any, config: RoutineConfig
    ) -> dict[str, Any]:
        signal = signal_of(dataset)
        rows = len(self._powers)
        columns = len(self._frequencies)
        if signal.size < rows * columns:
            raise RoutineError(
                f"punchout expected {rows * columns} acquisitions, got {signal.size}"
            )

        # One resonator fit per power, then the power at which it stops moving.
        frequencies = []
        for row in range(rows):
            chunk = signal[row * columns : (row + 1) * columns]
            fitted = fit_resonator_spectroscopy(self._frequencies, chunk)
            frequencies.append(fitted["readout_frequency"])
        return fit_punchout(self._powers, frequencies)

    def apply(self, device: Any, target: str, params: dict[str, Any]) -> None:
        element = device.get_element(target)
        write_path(element, "measure.pulse_amp", params["readout_power"])
        write_path(element, "clock_freqs.readout", params["readout_frequency"])


class QubitSpectroscopy(CalibrationRoutine):
    """Two-tone spectroscopy: find f01 (Schuster et al., Nature 445, 515)."""

    name = "qubit_spectroscopy"
    depends_on = ("resonator_spectroscopy", "resonator_punchout")
    updates = ("clock_freqs.f01",)

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        self._frequencies = _frequency_sweep(
            config, device, target, "f01", default_span=40e6
        )
        clock = f"{target}.01"
        # A weak drive at the calibrated pulse shape, deliberately.
        #
        # A long square saturation tone is what textbook two-tone spectroscopy
        # uses, and it does resolve the line far better — 63 kHz against 13.6
        # MHz, measured. It was tried here and backed out, because this routine's
        # job in the graph is to *locate* a qubit across hundreds of MHz, and a
        # square tone's response is a sinc: over a coarse sweep the Lorentzian
        # fit latches onto a side lobe and lands 91 MHz out. The DRAG envelope is
        # smooth and has no lobes to catch.
        #
        # Precision is not lost by that choice, it is delegated: `ramsey` runs
        # after `rabi` and refines f01 to hertz. Spectroscopy finds the qubit,
        # Ramsey measures it — which is what the dependency order already says.
        drive_amp = float(config.get("drive_amp", 0.01))
        schedule = backend.new_schedule(
            self.name, repetitions=int(config.get("shots", 1024))
        )
        for index, frequency in enumerate(self._frequencies):
            schedule.add(backend.Reset(target))
            schedule.add(
                backend.SetClockFrequency(clock=clock, clock_freq_new=frequency)
            )
            schedule.add(backend.Rxy(theta=180, phi=0, qubit=target, amp180=drive_amp))
            schedule.add(
                backend.Measure(
                    target, acq_index=index, bin_mode=backend.BinMode.AVERAGE
                )
            )
        return schedule

    def analyse(
        self, dataset: xr.Dataset, target: str, device: Any, config: RoutineConfig
    ) -> dict[str, Any]:
        fitted = fit_qubit_spectroscopy(self._frequencies, signal_of(dataset))
        _require_resolved_line(fitted["linewidth"], self._frequencies)
        return fitted

    def apply(self, device: Any, target: str, params: dict[str, Any]) -> None:
        write_path(
            device.get_element(target), "clock_freqs.f01", params["clock_freq_01"]
        )


class FluxSpectroscopy(CalibrationRoutine):
    """Map coupler frequency against flux bias — the input to a CZ."""

    name = "flux_spectroscopy"
    depends_on = ("qubit_spectroscopy",)
    updates = ()

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        self._offsets = setpoints_of(
            config, "flux_offsets", linear_setpoints(-0.2, 0.2, 11)
        )
        self._frequencies = _frequency_sweep(
            config, device, target, "f01", default_span=100e6
        )
        clock = f"{target}.01"
        duration = float(config.get("flux_duration", 200e-9))
        port = f"{target}:fl"
        schedule = backend.new_schedule(
            self.name, repetitions=int(config.get("shots", 512))
        )
        index = 0
        for offset in self._offsets:
            for frequency in self._frequencies:
                schedule.add(backend.Reset(target))
                schedule.add(
                    backend.SquarePulse(
                        amp=offset, duration=duration, port=port, clock="cl0.baseband"
                    )
                )
                schedule.add(
                    backend.SetClockFrequency(clock=clock, clock_freq_new=frequency)
                )
                schedule.add(backend.Rxy(theta=180, phi=0, qubit=target))
                schedule.add(
                    backend.Measure(
                        target, acq_index=index, bin_mode=backend.BinMode.AVERAGE
                    )
                )
                index += 1
        return schedule

    def analyse(
        self, dataset: xr.Dataset, target: str, device: Any, config: RoutineConfig
    ) -> dict[str, Any]:
        signal = signal_of(dataset)
        columns = len(self._frequencies)
        arc = []
        for row in range(len(self._offsets)):
            chunk = signal[row * columns : (row + 1) * columns]
            if chunk.size < columns:
                break
            arc.append(
                fit_qubit_spectroscopy(self._frequencies, chunk)["clock_freq_01"]
            )
        if not arc:
            raise RoutineError("flux spectroscopy produced no usable frequency arc")

        sweet_spot = max(range(len(arc)), key=lambda i: arc[i])
        return {
            "flux_offsets": list(self._offsets[: len(arc)]),
            "frequencies": arc,
            "sweet_spot_offset": float(self._offsets[sweet_spot]),
            "sweet_spot_frequency": float(arc[sweet_spot]),
        }
