"""Spectroscopy: finding the resonator, its readout power, and the qubit (RFC 0004 §3).

These are the roots of the DAG — everything downstream needs a frequency to
drive at. Each sweeps a clock frequency across the scan and fits a Lorentzian to
the response.
"""

import logging
import math
from typing import Any

import numpy as np
import xarray as xr

from qpi_driver.tuners.base.backend import SchedulerBackend
from qpi_driver.tuners.base.config import RoutineConfig
from qpi_driver.tuners.base.limits import addressable_band, clamp_to_band, full_scale
from qpi_driver.tuners.base.device import (
    has_flux_port,
    measured_linewidth,
    resonator_linewidth_path,
    read_path,
    spectroscopy_amplitude_path,
    write_path,
)
from qpi_driver.tuners.base.routines import (
    DEFAULT_SWEEP_POINTS,
    MAX_SWEEP_POINTS,
    DEFAULT_ROUTINE_TIMEOUT_S,
    CalibrationRoutine,
    CheckOutcome,
    RoutineError,
    grid_duration,
    require_resolved_line,
    linear_setpoints,
    setpoints_of,
)
from qpi_driver.tuners.fitting import (
    FitError,
    fit_punchout,
    fit_qubit_spectroscopy,
    fit_readout_timing,
    fit_resonator_spectroscopy,
    fit_spectroscopy_power,
    signal_of,
)

log = logging.getLogger(__name__)

#: Full scale. The elements validate the same bound on `spec.amplitude`; past it a
#: waveform clips and the schedule will not compile.
MAX_SPECTROSCOPY_AMPLITUDE = 1.0

#: Smallest dispersive shift worth calling one, as a fraction of the resonator
#: linewidth. Below this the ground and excited Lorentzians overlap to within a
#: twentieth of their own width, so no rotation or threshold separates the two
#: clouds and readout is capped near chance whatever the discriminator does. The
#: simulated chip measures 0.62 here; a chip whose X gate was off resonance
#: measured 0.0005 to 0.005 across six runs, so the floor sits an order of
#: magnitude clear of both.
MIN_SHIFT_TO_LINEWIDTH = 0.05

#: How far the tallest bin of a wide search must stand over the scatter to count as a
#: line. The tallest of n normal draws sits near ``sqrt(2 ln n)`` — 3.4 for the 301-point
#: default, 5.3 even at a million — so 6 refuses noise at any grid size worth sweeping.
#: Measured on the simulated chip: 115 to 126 on the line, 2.5 to 2.9 off it.
MIN_SEARCH_PEAK = 6.0

#: Scales a median absolute deviation to the standard deviation of a normal.
MAD_TO_SIGMA = 1.4826

#: What a transmon's anharmonicity may be, in Hz, and it is negative — the 1-2 transition
#: sits *below* the 0-1 one. The range is wide on purpose: fabricated transmons run from
#: about 150 to 400 MHz, and the point is not to pin a chip down but to refuse a number
#: that is not an anharmonicity at all.
#:
#: The failure it exists for: a device file carried `f12 = 4.8e9` as a placeholder against
#: an f01 of about 4.7 GHz, so the implied anharmonicity was *positive* on four of five
#: qubits. Nothing objected, `f12_spectroscopy` searched around a frequency no transmon
#: has, and the EF chain spent several runs measuring nothing.
ANHARMONICITY_RANGE_HZ = (-400e6, -150e6)

#: How wide an excited-state resonator sweep is, as a multiple of the measured linewidth.
#: Wider than a refinement because these have to *find* a resonance that has moved: the
#: dispersive shift puts it up to a couple of linewidths away, and the sweep needs baseline
#: either side of wherever it landed. The 20 MHz constant this replaces is 6.0 linewidths
#: on the simulated chip and the 4 MHz an operator hand-set on a 370 kHz resonator is 10.8,
#: so eight sits between the two chips that have been measured.
EXCITED_SPAN_IN_LINEWIDTHS = 8.0


#: The hardware-config key each device clock is driven through, for `addressable_band`.
_PORT_CLOCKS = {
    "readout": "{target}:res-{target}.ro",
    "f01": "{target}:mw-{target}.01",
    "f12": "{target}:mw-{target}.12",
}


def _frequency_sweep(
    config: RoutineConfig,
    device: Any,
    target: str,
    clock: str,
    default_span: float,
    backend: SchedulerBackend | None = None,
) -> list[float]:
    """The frequencies to scan, either given outright or as a span about the current one.

    A span is the useful form for a recalibration — the frequency has drifted a
    little, so scan around where it was — while an explicit range is what a
    first bring-up needs, when there is no trustworthy current value.

    Trimmed to what the port can be driven at, when *backend* says how far that reaches.
    A span is symmetric about a frequency and the LO is not at its centre, so a wide one
    runs off the end of the module's range — and asking for the part outside fails
    compilation with `Attempting to set NCO frequency` naming neither the routine nor the
    setpoint. Explicit ``frequencies`` are left alone: an operator listing setpoints
    outright has said what they want, and silently dropping some would be worse than the
    compiler's complaint.
    """
    if "frequencies" in config:
        return setpoints_of(config, "frequencies", [])

    centre = config.get("centre_frequency")
    if centre is None:
        centre = _current_clock(device, target, clock)
    span = float(config.get("span", default_span))
    points = int(config.get("points", DEFAULT_SWEEP_POINTS))

    low, high = centre - span / 2, centre + span / 2
    if backend is not None and clock in _PORT_CLOCKS:
        port_clock = _PORT_CLOCKS[clock].format(target=target)
        band = addressable_band(device, port_clock, backend.if_limit_hz)
        trimmed = clamp_to_band(low, high, band)
        if trimmed != (low, high):
            log.info(
                "%s.%s sweep trimmed from %.0f-%.0f Hz to the %.0f-%.0f Hz the port "
                "can reach",
                target,
                clock,
                low,
                high,
                *trimmed,
            )
        low, high = trimmed
        if high <= low:
            raise RoutineError(
                f"{target}.{clock} is configured at {centre:.0f} Hz, outside everything "
                f"its port can reach — no sweep around it is addressable, and the LO has "
                f"to move, which is a hardware-config change"
            )
    return linear_setpoints(low, high, points)


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
    Hand-set until now — 200 ns in a working chip's config, with nothing measuring it.
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
    reads = ("measure.acq_delay", "measure.integration_time")

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
    Three time constants would have shortened the fixture's 1 µs window to
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
    reads = ("measure.acq_delay", "measure.integration_time")


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
    updates = ("clock_freqs.readout", "resonator.linewidth")
    reads = ("clock_freqs.readout",)

    #: How wide to look when the operator names no span, in Hz.
    #:
    #: A named constant rather than a literal because escalation multiplies it: a refusal
    #: carries the axis ``span``, and `_scalar_axis` reads the value back off ``_span``.
    SPAN = 20e6

    def measure(
        self,
        target: str,
        device: Any,
        config: RoutineConfig,
        backend: SchedulerBackend,
        bias: Any = None,
        timeout_s: float = DEFAULT_ROUTINE_TIMEOUT_S,
    ) -> dict[str, Any]:
        """Widen and look again when the fitted line lands outside the window.

        The root of the graph could not do this, which is the whole of RFC 0007 §11.2. A
        resonator a few MHz outside its window is the commonest bring-up state there is —
        fabrication scatter alone moves one by tens of MHz — and every node downstream
        reads the frequency this one writes, so a refusal here stops the chip rather than
        one routine.
        """
        return self.escalating(target, device, config, backend, timeout_s)

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        # Recorded for escalation to read back, under the `_<axis>` convention `_widened`
        # already uses for setpoint lists.
        self._span = float(config.get("span", self.SPAN))
        self._frequencies = _frequency_sweep(
            config, device, target, "readout", default_span=self.SPAN, backend=backend
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
        fitted = fit_resonator_spectroscopy(self._frequencies, signal_of(dataset))
        # The root of the graph, and the one frequency every other node reads at.
        # It had no guard: a 72% dip confined to one 400 kHz bin was fitted as a
        # 2379 Hz linewidth at Q = 2.9 million, and the centre it wrote was 47 kHz
        # off the deepest sample it had actually measured.
        # span so a flat window widens and a line thinner than the grid gets a
        # finer one, instead of both ending the run (RFC 0007 §11.2).
        require_resolved_line(fitted, self._frequencies, axis="span")
        return fitted

    def apply(self, device: Any, target: str, params: dict[str, Any]) -> None:
        element = device.get_element(target)
        write_path(element, "clock_freqs.readout", params["readout_frequency"])
        # The linewidth too, when the element has somewhere for it. Three nodes size
        # their own sweeps from it and used to guess (RFC 0005 §13); this is the node
        # that measures it, and it was throwing it away. Opt-in like every other
        # `CalibratedTransmon` field: a plain `BasicTransmonElement` has no
        # ``resonator`` submodule, and those chips keep the old constant.
        if resonator_linewidth_path(element):
            write_path(element, "resonator.linewidth", params["linewidth"])

    #: The readout linewidth a check falls back to when the element cannot store one,
    #: in Hz, and how much of it the configured frequency may sit away from the peak.
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
        span = float(config.get("check_span", 0.0)) or 2.0 * self._linewidth(
            config, device, target
        )
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

        linewidth = self._linewidth(config, device, target)
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

    def _linewidth(self, config: RoutineConfig, device: Any, target: str) -> float:
        """The linewidth this check's probe spacing and tolerance scale with, in Hz.

        Measured, where this node has had somewhere to record it; an operator's
        ``check_linewidth`` still wins, and the constant is only reached on an element
        with no ``resonator`` submodule.
        """
        if "check_linewidth" in config:
            return float(config["check_linewidth"])
        return measured_linewidth(device.get_element(target), self.CHECK_LINEWIDTH_HZ)


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
    reads = ("clock_freqs.readout",)

    def acquire(
        self,
        target: str,
        device: Any,
        config: RoutineConfig,
        backend: SchedulerBackend,
        timeout_s: float,
    ) -> Any:
        """A row per readout amplitude, chunked when the grid outgrows one schedule."""
        return self.acquire_in_row_chunks(
            target, device, config, backend, timeout_s, rows_axis="amplitudes"
        )

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        # To full scale, not to half. Punch-through is by definition the *high*-power end
        # of the sweep, so a grid stopping at 0.5 finds it only on a line lossless enough
        # to punch through at half drive — and on a chip carrying `output_att: 20` it is
        # some 26 dB short of what the module can emit, which is to say it cannot find it
        # at all. This is the §5 hardware bound that got the node switched off on the
        # August 2026 chips, and that §12 recorded as fixed in phase 3 when it was not.
        self._amplitudes = setpoints_of(
            config,
            "amplitudes",
            linear_setpoints(
                0.01, full_scale(device.get_element(target), "measure.pulse_amp"), 11
            ),
        )
        self._frequencies = _frequency_sweep(
            config, device, target, "readout", default_span=20e6, backend=backend
        )
        clock = f"{target}.ro"
        schedule = backend.new_schedule(
            self.name, repetitions=int(config.get("shots", 512))
        )
        index = 0
        for power in self._amplitudes:
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
        rows = len(self._amplitudes)
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
        return fit_punchout(self._amplitudes, frequencies)

    def apply(self, device: Any, target: str, params: dict[str, Any]) -> None:
        element = device.get_element(target)
        write_path(element, "measure.pulse_amp", params["readout_power"])
        write_path(element, "clock_freqs.readout", params["readout_frequency"])

    #: How far the resonance may walk between the configured power and half of it
    #: before the readout counts as punched through, as a fraction of a linewidth.
    #:
    #: Halving the power is the probe because the dressed regime is defined by the
    #: pull *not* moving: below the crossover the resonance is flat in power, above
    #: it the resonance walks. So the question "are we still dressed?" is answered by
    #: changing the power and seeing whether the line follows.
    CHECK_LINEWIDTH_HZ = 2e6
    CHECK_MAX_WALK_LINEWIDTHS = 0.5

    def build_check_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        """Two short resonator scans, at the configured power and at half of it.

        A cheaper version of the sweep would be the wrong check. Punchout's answer is
        not "where is the resonance" but "is the power still below the crossover",
        and that is a question about how the resonance *responds* to power — one scan
        cannot ask it however finely it is stepped.

        Eight points each against the sweep's eleven powers by fifty-one frequencies.
        Enough to fit a line, not enough to place it precisely, which is all a check
        needs: a readout that has drifted into punch-through has moved by a linewidth,
        not by a hair.
        """
        element = device.get_element(target)
        power = float(read_path(element, "measure.pulse_amp"))
        self._check_powers = [power, power / 2.0]
        centre = _current_clock(device, target, "readout")
        span = float(config.get("check_span", 0.0)) or 6.0 * self._check_linewidth(
            config, device, target
        )
        points = int(config.get("check_points", 8))
        self._check_frequencies = linear_setpoints(
            centre - span / 2, centre + span / 2, points
        )

        clock = f"{target}.ro"
        schedule = backend.new_schedule(
            f"{self.name}_check", repetitions=int(config.get("check_shots", 512))
        )
        index = 0
        for probe in self._check_powers:
            for frequency in self._check_frequencies:
                schedule.add(backend.Reset(target))
                schedule.add(
                    backend.SetClockFrequency(clock=clock, clock_freq_new=frequency)
                )
                schedule.add(
                    backend.Measure(
                        target,
                        acq_index=index,
                        bin_mode=backend.BinMode.AVERAGE,
                        pulse_amp=probe,
                    )
                )
                index += 1
        return schedule

    def _check_linewidth(
        self, config: RoutineConfig, device: Any, target: str
    ) -> float:
        """As `ResonatorSpectroscopy._linewidth`: measured if recorded, else the constant."""
        if "check_linewidth" in config:
            return float(config["check_linewidth"])
        return measured_linewidth(device.get_element(target), self.CHECK_LINEWIDTH_HZ)

    def analyse_check(
        self, dataset: xr.Dataset, target: str, device: Any, config: RoutineConfig
    ) -> CheckOutcome:
        signal = signal_of(dataset)
        columns = len(self._check_frequencies)
        if signal.size < 2 * columns:
            raise RoutineError(
                f"punchout check expected {2 * columns} acquisitions, got {signal.size}"
            )
        resonances = [
            fit_resonator_spectroscopy(
                self._check_frequencies, signal[row * columns : (row + 1) * columns]
            )["readout_frequency"]
            for row in range(2)
        ]
        walk = abs(resonances[0] - resonances[1])
        allowed = self._check_linewidth(config, device, target) * float(
            config.get("check_max_walk_linewidths", self.CHECK_MAX_WALK_LINEWIDTHS)
        )
        return CheckOutcome(
            passed=walk <= allowed,
            margin=walk / max(allowed, 1e-9),
            detail=(
                f"resonance walks {walk / 1e6:.3f} MHz when the readout power is "
                f"halved, against {allowed / 1e6:.3f} MHz allowed"
            ),
        )


class ResonatorSpectroscopyExcited(CalibrationRoutine):
    """The resonator again with the qubit in ``|1>`` — which is where chi comes from.

    A characterisation, like `resonator_relaxation`: it writes nothing. The two
    resonances it and `resonator_spectroscopy` measure differ by twice the dispersive
    shift, and that number is the whole basis of the readout — it is what makes the
    states distinguishable at all, and its collapse is what punchout detects. Nothing
    else in the graph measures it.

    Deliberately not the producer of the optimal readout frequency. The frequency
    where the two responses differ most is not derivable from the two resonances
    alone — it depends on the linewidth and on how the two Lorentzians overlap — so
    `readout_frequency_two_state` measures the separation directly rather than
    computing it from here.
    """

    name = "resonator_spectroscopy_excited"
    depends_on = ("rabi",)
    updates = ()
    reads = (
        "clock_freqs.readout",
        "resonator.linewidth",
        "clock_freqs.f01",
        "rxy.amp180",
    )

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        element = device.get_element(target)
        self._frequencies = _frequency_sweep(
            config,
            device,
            target,
            "readout",
            default_span=EXCITED_SPAN_IN_LINEWIDTHS
            * measured_linewidth(element, 2.5e6),
            backend=backend,
        )
        # The reference `analyse` differences against, read here rather than there: a
        # prerequisite has to be readable before the acquisition to be one at all.
        self._ground = _current_clock(device, target, "readout")
        clock = f"{target}.ro"
        schedule = backend.new_schedule(
            self.name, repetitions=int(config.get("shots", 1024))
        )
        for index, frequency in enumerate(self._frequencies):
            schedule.add(backend.Reset(target))
            schedule.add(backend.X(target))
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
        fitted = fit_resonator_spectroscopy(self._frequencies, signal_of(dataset))
        require_resolved_line(fitted, self._frequencies)
        excited = fitted["readout_frequency"]
        ground = self._ground
        shift = 0.5 * (excited - ground)
        linewidth = float(fitted["linewidth"])
        # The one place the X gate is checked against a resonance instead of against
        # its own fit. A pulse driving nothing leaves the resonator where the ground
        # state had it, and every node downstream — discrimination, allxy, drag, rb —
        # then measures an idle qubit and fits its noise. Six runs of that read as six
        # unrelated failures until this node was made to refuse.
        if abs(shift) < MIN_SHIFT_TO_LINEWIDTH * linewidth:
            raise RoutineError(
                f"exciting {target} moved its resonator by {shift:.4g} Hz against a "
                f"{linewidth:.4g} Hz linewidth — {abs(shift) / linewidth:.1%} of it, "
                f"under the {MIN_SHIFT_TO_LINEWIDTH:.0%} two resolvable states clear. "
                "The X gate is not exciting this qubit: check that clock_freqs.f01 is "
                "the transition the drive line actually reaches, then that rxy.amp180 "
                "is a pi pulse at it"
            )
        return {
            "readout_frequency_excited": excited,
            # What the shift was measured against, reported because it is not measured
            # here: it is whatever the device currently holds, which `resonator_spectroscopy`
            # writes and anything pinning the config can override. Differencing against a
            # stale value reports a dispersive shift that is really the distance to the
            # stale value — seen at 186 kHz on a chip whose true shift was under 1 kHz.
            "readout_frequency_ground": ground,
            # Half the gap, signed: chi is negative for a transmon below its
            # resonator. The sign is worth keeping — it says which side of the bare
            # resonance the dressed one sits, which is how a mis-assigned resonator
            # shows up.
            "dispersive_shift": shift,
            "linewidth": linewidth,
            # The spectrum itself, so the shift can be read off two overlaid curves
            # rather than inferred from two fitted centres. When chi is a fraction of a
            # linewidth the centres are the least reliable way to see it.
            "fit": fitted["fit"],
        }

    def apply(self, device: Any, target: str, params: dict[str, Any]) -> None:
        """Nothing to write — see the class docstring."""


class QubitSpectroscopy(CalibrationRoutine):
    """Two-tone spectroscopy: find f01 (Schuster et al., Nature 445, 515).

    Sweeps drive power alongside frequency, and reports both. The power cannot be
    calibrated by a separate node before this one — choosing a spectroscopy power
    means comparing how clearly each power shows the line, and there is no line to
    look at until this routine has found it. Nor can it be calibrated after, since
    this routine's own answer depends on it. So it is one measurement of two
    quantities, the way `resonator_punchout` is.

    ``spec.amplitude`` only exists on a `CalibratedTransmon`. Against a config that
    keeps `BasicTransmonElement` the sweep still runs and still picks its best row —
    the power just is not remembered between calibrations.

    The configured ``clock_freqs.f01`` is a *prior*, not an answer: see :meth:`measure`.
    """

    name = "qubit_spectroscopy"
    depends_on = ("resonator_spectroscopy", "resonator_punchout")
    updates = ("clock_freqs.f01", "spec.amplitude")
    reads = ("clock_freqs.f01", "spec.amplitude")

    #: Drive powers to compare, as a fraction of full scale. Wide, because on a first
    #: bring-up nothing yet says which end of it the chip wants.
    #:
    #: Geometric, and that is the whole design: the response is power-law with an unknown
    #: scale, and `fit_spectroscopy_power` keeps the *narrowest* credible line of the set, so
    #: a wide bracket costs acquisitions rather than accuracy. A linear ladder would spend
    #: its rungs in one decade and miss whichever one the chip is in.
    #:
    #: **Raising the ceiling was tried in August 2026 and reverted.** Two chips disagree, and
    #: both are evidence. A B-chip transition reached only 3.33x over its own scatter at 0.08
    #: against the 5x `require_resolved_line` clears, so this ladder cannot see every real
    #: line. But a ladder reaching 0.3 put the simulated chip's calibrated f01 1.76 MHz out,
    #: against a 1 MHz tolerance — measured against the same rung *count* at these powers,
    #: which passes, so it is the power and not the changed noise draw.
    #:
    #: So the right ceiling is a property of the chip and its drive chain, which is what
    #: ``drive_amps`` is for. A chip that needs more than this says so by refusing, and the
    #: refusal names the axis; guessing higher here trades a chip that cannot be seen for
    #: every chip being measured slightly worse.
    DEFAULT_AMPLITUDES = (0.005, 0.01, 0.02, 0.04, 0.08)

    #: Multiples of a remembered power to bracket on a recalibration. Three rather
    #: than five, because this sweep costs one acquisition per power *per frequency*:
    #: a bring-up pays that to find the right end of a wide range, and a
    #: recalibration should not pay it again to confirm what it already knows.
    RECALIBRATION_FACTORS = (0.5, 1.0, 2.0)

    #: How far the widening pass looks, and how finely.
    #:
    #: Not as wide as it could usefully be, and the ceiling is hardware. An RF module
    #: reaches +/-500 MHz either side of its LO — quantify's ``NCO_FREQ_LIMIT_STEPS`` over
    #: ``NCO_FREQ_STEPS_PER_HZ`` — so a 1 GHz search is addressable only when the LO sits
    #: at the search centre, and asking past that does not compile. 600 MHz leaves 200 MHz
    #: of slack for an LO placed off-centre, which is the usual case.
    #:
    #: So the operator still has to widen this on a chip further out than 300 MHz, and the
    #: refusal below says so. What the default buys is that being a few hundred MHz wrong
    #: — a design value, a different flux bias — no longer needs anyone to notice.
    #:
    #: 2 MHz steps sit inside the width a saturating drive broadens the line to, so a real
    #: line lands in some bin, and 301 acquisitions is well clear of what a sequencer
    #: assembles.
    SEARCH_SPAN = 600e6
    SEARCH_POINTS = 301

    #: The widening pass drives at one power, and locating a line does not need powers
    #: compared — five of them across 301 frequencies is 1505 acquisitions, past what a
    #: sequencer assembles. Which power is *derived*: the strongest this routine would try
    #: anyway, so `_drive_amplitudes` answers it.
    #:
    #: It was a constant equal to the old `DEFAULT_AMPLITUDES` ceiling, which made the
    #: "strongest anyway" claim true only until either changed. An operator raising
    #: ``drive_amps`` to 0.3 left the search probing 3.75x weaker than the pass it exists to
    #: feed — backwards, since the search is the one that has to *see* a line at all, and
    #: the confirm pass is where a gentle power belongs.

    #: How wide the sweep that *confirms* a searched-out line should be, as a multiple of
    #: the width the search measured, and over how many points.
    #:
    #: The operator's ``span`` cannot be reused for it. A span says *where the line might
    #: be*, and once the search has located it that statement is spent — worse, a config
    #: whose span was wide precisely because it did not know where to look then confirms
    #: on a grid far too coarse to resolve anything. The loop fixture is the case: it
    #: sweeps 600 MHz over 61 points to find a qubit 214 MHz from its config, and
    #: re-centring that same grid steps 10 MHz across a line about as wide.
    #:
    #: Nor is a fixed window right, because the line's width is set by the drive
    #: amplitude the confirming sweep is itself choosing. Measured on the simulated chip
    #: against a line about 140 MHz wide at the strongest drive, by window:
    #:
    #:     20 MHz   refused, nothing clears the floor
    #:     40 MHz   reach 5.0, exactly at the floor, f01 1.62 MHz out
    #:    100 MHz   reach 60.7, f01 0.88 MHz out
    #:    200 MHz   reach 83.5, f01 1.17 MHz out
    #:    400 MHz   reach 31.5, f01 0.97 MHz out
    #:
    #: So a little over one width is the sweet spot: enough baseline either side to
    #: measure the line against, without diluting it across a window it does not fill.
    #: 1.5 is that, and it is derived rather than guessed — an earlier four-search-steps
    #: constant landed on 40 MHz here, which is the row that passes by nothing at all.
    CONFIRM_SPAN_IN_WIDTHS = 1.5
    CONFIRM_POINTS = 41

    #: Floors and ceilings the derived span, for the two ends the search cannot resolve.
    #: A line narrower than one search step reads as one step wide, and 1.5 steps is too
    #: tight to fit anything; a line as wide as the search itself leaves no baseline. Four
    #: steps is the same floor the earlier constant used, kept for the narrow end where it
    #: was never the problem.
    CONFIRM_MIN_SPAN_IN_STEPS = 4.0

    #: How wide the ordinary sweep about the configured f01 is, when the operator names no
    #: span. Named because `_confirm_points` reads it too, to hold the same step.
    NARROW_SPAN = 40e6

    def acquire(
        self,
        target: str,
        device: Any,
        config: RoutineConfig,
        backend: SchedulerBackend,
        timeout_s: float,
    ) -> Any:
        """A row per drive amplitude, chunked when the grid outgrows one schedule."""
        return self.acquire_in_row_chunks(
            target, device, config, backend, timeout_s, rows_axis="drive_amps"
        )

    def measure(
        self,
        target: str,
        device: Any,
        config: RoutineConfig,
        backend: SchedulerBackend,
        bias: Any = None,
        timeout_s: float = DEFAULT_ROUTINE_TIMEOUT_S,
    ) -> dict[str, Any]:
        """Sweep where the config says f01 is; if the line is not there, go and find it.

        This node's job is to measure f01, so a configured value has to be treated as a
        guess about the chip rather than as the answer. Design values, and values
        measured at some other flux bias, are both routinely a few hundred MHz out — and
        before this, the sweep only ever looked +/-20 MHz around whatever it was handed
        and refused what it fitted. On a chip whose f01 sat 302 MHz below its design
        value that read as six runs of "no drive power resolved a line", with nothing
        saying the window was the problem, and it left the operator to supply by hand the
        one number the node exists to produce.

        Two passes, and the split matters. The wide pass only chooses *where to look*:
        what gets written still comes from the narrow sweep and still has to clear
        `require_resolved_line`. So a coarse grid — on which every real line is narrower
        than one step, and a Lorentzian fit is therefore drawing through noise between
        points — can never be the thing that sets f01.

        Costs nothing on a chip that is where it says it is: the wide pass runs only
        after the narrow one has already failed.
        """
        try:
            return self._sweep(target, device, config, backend, timeout_s)
        except (RoutineError, FitError) as narrow:
            near = str(narrow)
            log.info(
                "%s: no line within the configured window for %s (%s) — widening to "
                "%.0f MHz",
                self.name,
                target,
                near,
                float(config.get("search_span", self.SEARCH_SPAN)) / 1e6,
            )

        found, width = self._search(target, device, config, backend, timeout_s)
        confirming = RoutineConfig(
            enabled=config.enabled,
            params={
                **config.params,
                "centre_frequency": found,
                "span": float(
                    config.get("confirm_span", self._confirm_span(config, width))
                ),
                "points": int(
                    config.get(
                        "confirm_points",
                        self._confirm_points(config, device, target, width),
                    )
                ),
            },
        )
        try:
            return self._sweep(target, device, confirming, backend, timeout_s)
        except (RoutineError, FitError) as exc:
            raise RoutineError(
                f"the widened search put {target}'s strongest line at {found:.0f} Hz, "
                f"but sweeping finely there did not confirm it: {exc}. Around the "
                f"configured f01 it said: {near}"
            ) from exc

    def _sweep(
        self,
        target: str,
        device: Any,
        config: RoutineConfig,
        backend: SchedulerBackend,
        timeout_s: float,
    ) -> dict[str, Any]:
        """The ordinary pass: build, run, fit, and refuse anything unresolved.

        Through `escalating`, which is `acquire` then `analyse` with a retry: a power
        sweep of many rows is still chunked rather than compiled into a program no
        sequencer takes, and a refusal that names ``drive_amps`` now climbs the ladder
        instead of ending the node.

        That retry is what makes :data:`DEFAULT_AMPLITUDES` a starting bracket rather
        than a ceiling. Its own note assumed this — "a chip that needs more than this
        says so by refusing, and the refusal names the axis" — but no refusal in
        `fit_spectroscopy_power` named one, so the ladder was a hard limit and a chip
        whose drive chain is more attenuated than the one it was tuned on died here.
        The August 2026 B chip did, and its operator hand-wrote ``drive_amps`` up to 0.3;
        one escalation from the default now reaches 0.305 on its own.

        An operator who sets ``drive_amps`` keeps it: `escalating` leaves an axis the
        config names alone, so this only ever moves a default.
        """
        return self.escalating(target, device, config, backend, timeout_s)

    def _search(
        self,
        target: str,
        device: Any,
        config: RoutineConfig,
        backend: SchedulerBackend,
        timeout_s: float,
    ) -> tuple[float, float]:
        """Where the strongest line in a wide window is, and roughly how wide.

        The width is what sizes the sweep that confirms it — see
        `CONFIRM_SPAN_IN_WIDTHS`. Returned rather than stored because it is only ever
        used by the caller that asked for the search.
        """
        span = float(config.get("search_span", self.SEARCH_SPAN))
        amplitude = float(
            config.get(
                "search_amp", max(self._drive_amplitudes(config, device, target))
            )
        )
        centre = _current_clock(device, target, "f01")

        # Trimmed to what the port can actually be driven at. A span centred on the
        # configured f01 is not centred on the LO, so half of it can fall outside the
        # module's reach while the other half is fine — and asking for the outside half
        # does not fail the sweep politely, it fails compilation with `Attempting to set
        # NCO frequency` and no mention of which routine or which setpoint. Trimming
        # keeps the reachable part, which is where the qubit has to be anyway: outside
        # the band there is no experiment to run.
        band = addressable_band(device, f"{target}:mw-{target}.01", backend.if_limit_hz)
        low, high = clamp_to_band(centre - span / 2, centre + span / 2, band)
        if high <= low:
            raise RoutineError(
                f"{target}'s configured f01 of {centre:.0f} Hz is outside everything its "
                f"drive port can reach ({band[0]:.0f} to {band[1]:.0f} Hz), so no search "
                "can find it — the LO has to move, which is a hardware-config change"
            )

        # The grid keeps its step rather than its point count, so trimming the span makes
        # the search cheaper instead of finer: a step chosen to sit inside a broadened
        # line has to stay that way whatever the band leaves.
        step = span / max(int(config.get("search_points", self.SEARCH_POINTS)) - 1, 1)
        points = max(int(round((high - low) / step)) + 1, 2)
        frequencies = linear_setpoints(low, high, points)

        # Fewer shots than the narrow pass, because this only has to see a peak rather
        # than measure its centre — and because a wide grid is already many acquisitions
        # against a `routine_timeout_s` that bounds the whole two-pass loop.
        schedule = self._probe_schedule(
            target,
            frequencies,
            [amplitude],
            backend,
            int(config.get("search_shots", 256)),
        )
        signal = signal_of(backend.run(schedule, timeout_s=timeout_s))

        # The tallest bin, not a fitted centre — no Lorentzian anywhere in this pass.
        #
        # Fitting one here was tried and is wrong twice over. A line on a 2 MHz grid is
        # narrower than a step, so there is nothing for a lineshape to be fitted *to*;
        # and an optimiser handed 301 points of noise returns a confident centre with a
        # signal-to-noise of 3, which cleared the floor this simulator then had and
        # would have sent the narrow pass to an arbitrary frequency. Measured: a real
        # line reaches 115-126 by the ratio below, and pure noise 2.5-2.9.
        #
        # A bin index cannot be pulled off the grid by a fit, and half a step of
        # precision is all this pass owes — the narrow sweep is what measures f01.
        baseline = float(np.median(signal))
        deviation = np.abs(np.asarray(signal, dtype=float) - baseline)
        # Median absolute deviation, scaled to a standard deviation. Robust by
        # construction: a peak occupying a few bins of hundreds cannot inflate the
        # scatter it is being judged against, the way an RMS residual would.
        scatter = MAD_TO_SIGMA * float(np.median(deviation))
        peak = float(np.max(deviation))
        reach = peak / scatter if scatter > 0 else float("inf")

        if reach < MIN_SEARCH_PEAK:
            raise RoutineError(
                f"nothing above the noise between {frequencies[0]:.0f} and "
                f"{frequencies[-1]:.0f} Hz — the tallest bin in a {span / 1e6:.0f} MHz "
                f"search around {target}'s configured f01 stands {reach:.1f}x over the "
                f"scatter, below the {MIN_SEARCH_PEAK:g}x a line clears. Either the qubit "
                f"is outside that window, or the drive is not reaching it: check the "
                f"port's wiring and attenuation, then widen with `search_span` — bearing "
                f"in mind a module reaches only +/-500 MHz either side of its LO, so past "
                f"that the LO has to move too"
            )

        found = float(frequencies[int(np.argmax(deviation))])
        # How wide the line is, counted rather than fitted: the bins standing at least
        # half the peak above the baseline are its full width at half maximum, to the
        # resolution of the grid. Crude on purpose — a fit here is what the comment above
        # rules out — and it only has to size the sweep that follows, not measure anything.
        step = frequencies[1] - frequencies[0] if len(frequencies) > 1 else 0.0
        width = float(np.count_nonzero(deviation >= peak / 2.0)) * abs(step)
        log.info(
            "%s: %s's strongest line is at %.0f Hz, %.0f MHz from the configured f01, "
            "%.1fx over the scatter, about %.0f MHz wide",
            self.name,
            target,
            found,
            (found - centre) / 1e6,
            reach,
            width / 1e6,
        )
        return found, width

    def _confirm_span(self, config: RoutineConfig, width: float) -> float:
        """How wide to sweep to confirm a line the search measured as *width* across."""
        span = float(config.get("search_span", self.SEARCH_SPAN))
        step = span / max(int(config.get("search_points", self.SEARCH_POINTS)) - 1, 1)
        return min(
            max(
                self.CONFIRM_SPAN_IN_WIDTHS * width,
                self.CONFIRM_MIN_SPAN_IN_STEPS * step,
            ),
            span,
        )

    def _confirm_points(
        self, config: RoutineConfig, device: Any, target: str, width: float
    ) -> int:
        """Points for the confirming sweep: enough to hold the narrow pass's own step.

        Fixed at `CONFIRM_POINTS` before, which made the *step* a consequence of the span
        rather than a choice — and the span comes from the width the search measured at
        **search** power. A power-broadened line reads as tens of MHz across: on the
        August 2026 B chip a 32 MHz width gave a 48 MHz span, 41 points, a 1.2 MHz step,
        and all three drive powers refused for a linewidth below the sweep step. The line
        was real, found within 2 MHz of the chip's VNA value, and 0.8 MHz wide.

        The operator's own ``span``/``points`` is their statement about the resolution
        their chip needs, so that is the step this holds. Bounded by the share of
        `MAX_SWEEP_POINTS` each drive power can afford, because the confirming sweep is one
        schedule across all of them, and floored at `CONFIRM_POINTS` so a chip whose narrow
        pass is coarser than the confirm span never sweeps fewer points than before.
        """
        narrow_span = float(config.get("span", self.NARROW_SPAN))
        narrow_points = max(int(config.get("points", DEFAULT_SWEEP_POINTS)), 2)
        step = narrow_span / (narrow_points - 1)
        span = float(config.get("confirm_span", self._confirm_span(config, width)))
        amplitudes = max(len(self._drive_amplitudes(config, device, target)), 1)
        affordable = MAX_SWEEP_POINTS // amplitudes
        wanted = int(span / step) + 1 if step > 0 else self.CONFIRM_POINTS
        return max(self.CONFIRM_POINTS, min(wanted, affordable))

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        return self._probe_schedule(
            target,
            _frequency_sweep(
                config,
                device,
                target,
                "f01",
                default_span=self.NARROW_SPAN,
                backend=backend,
            ),
            self._drive_amplitudes(config, device, target),
            backend,
            int(config.get("shots", 1024)),
        )

    def _probe_schedule(
        self,
        target: str,
        frequencies: list[float],
        amplitudes: list[float],
        backend: SchedulerBackend,
        shots: int,
    ) -> Any:
        # Recorded here rather than by each caller, so `analyse` cannot read a grid other
        # than the one the schedule it is handed actually swept — which two passes over
        # different windows makes a live possibility rather than a theoretical one.
        self._frequencies = frequencies
        self._drive_amps = amplitudes
        # Recorded for `_widened` to clamp against, under the `_<axis>` convention it
        # reads setpoints by. Without it escalation walks straight past full scale and
        # the compiler refuses the waveform — `Rabi` carries the same line for the same
        # reason. A drive amplitude is a fraction of full scale, so that is the bound.
        self._drive_amps_ceiling = MAX_SPECTROSCOPY_AMPLITUDE

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
        schedule = backend.new_schedule(self.name, repetitions=shots)
        index = 0
        for drive_amp in amplitudes:
            for frequency in frequencies:
                schedule.add(backend.Reset(target))
                schedule.add(
                    backend.SetClockFrequency(clock=clock, clock_freq_new=frequency)
                )
                schedule.add(
                    backend.Rxy(theta=180, phi=0, qubit=target, amp180=drive_amp)
                )
                schedule.add(
                    backend.Measure(
                        target, acq_index=index, bin_mode=backend.BinMode.AVERAGE
                    )
                )
                index += 1
        return schedule

    def _drive_amplitudes(
        self, config: RoutineConfig, device: Any, target: str
    ) -> list[float]:
        if "drive_amps" in config:
            return setpoints_of(config, "drive_amps", [])
        if "drive_amp" in config:
            return [float(config["drive_amp"])]

        path = spectroscopy_amplitude_path(device.get_element(target))
        remembered = read_path(device.get_element(target), path) if path else 0.0
        if not remembered:
            return list(self.DEFAULT_AMPLITUDES)
        return [
            min(factor * float(remembered), MAX_SPECTROSCOPY_AMPLITUDE)
            for factor in self.RECALIBRATION_FACTORS
        ]

    def analyse(
        self, dataset: xr.Dataset, target: str, device: Any, config: RoutineConfig
    ) -> dict[str, Any]:
        signal = signal_of(dataset)
        columns = len(self._frequencies)
        expected = len(self._drive_amps) * columns
        if signal.size < expected:
            raise RoutineError(
                f"qubit spectroscopy expected {expected} acquisitions, got {signal.size}"
            )
        rows = signal[:expected].reshape(len(self._drive_amps), columns)
        fitted = fit_spectroscopy_power(self._drive_amps, self._frequencies, rows)
        # The centre is still written — `ramsey` refines it either way — but a line wider
        # than the window it was fitted in has a linewidth nobody measured.
        return {
            **fitted,
            "unresolved": require_resolved_line(fitted, self._frequencies),
        }

    def apply(self, device: Any, target: str, params: dict[str, Any]) -> None:
        element = device.get_element(target)
        write_path(element, "clock_freqs.f01", params["clock_freq_01"])
        path = spectroscopy_amplitude_path(element)
        if path:
            write_path(element, path, params["drive_amplitude"])


class F12Spectroscopy(CalibrationRoutine):
    """Find the ``|1>``-``|2>`` transition, by driving it from ``|1>``.
    Depends on `rabi` because the transition starts from ``|1>``: without a calibrated
    pi pulse there is no population to drive out of, and the sweep comes back flat.
    That is the same straddle `readout_discrimination` sits in — part of the chip's
    characterisation cannot be done before the qubit chain has begun.

    ``clock_freqs.f12`` has been on the element all along and nothing measured it. The
    fixture pins it 134 MHz from where the simulated transmon's actually is,
    and nothing notices, because until now nothing read it either. It is the input to
    three-state readout and it grounds the ``|02>`` leg a CZ works through.
    """

    name = "f12_spectroscopy"
    depends_on = ("rabi",)
    updates = ("clock_freqs.f12",)
    reads = ("clock_freqs.f01", "rxy.amp180")

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        # Centred on f01 plus the anharmonicity rather than on the configured f12,
        # unless the config says otherwise: a chip whose f12 has never been measured
        # carries whatever was typed in, and scanning around that finds nothing. A
        # transmon's anharmonicity is a few hundred MHz and negative, so f01 - 300 MHz
        # is a far better prior than an unmeasured field.
        # Read here rather than in `analyse`, where the anharmonicity was differenced
        # against it: a prerequisite has to be readable before the acquisition to be one
        # at all, and this sweep is already centred on it.
        self._f01 = _current_clock(device, target, "f01")
        centre = config.get("centre_frequency")
        if centre is None:
            offset = float(config.get("anharmonicity_prior", -300e6))
            # Overridable, because the range is a prior over the transmon *family* and a
            # device deliberately built outside it is a chip fact, which belongs in a
            # config. The guard is against a placeholder, not against an unusual design.
            low, high = (
                float(v)
                for v in config.get("anharmonicity_range", ANHARMONICITY_RANGE_HZ)
            )
            if not low <= offset <= high:
                raise RoutineError(
                    f"`anharmonicity_prior` is {offset / 1e6:.0f} MHz, outside the "
                    f"{low / 1e6:.0f} to {high / 1e6:.0f} MHz a transmon's anharmonicity "
                    f"runs to — negative, the 1-2 transition sitting below the 0-1 one. "
                    f"Searching around f01 plus this would look where no transition is. "
                    f"Set `anharmonicity_range` for a device built outside it"
                )
            centre = self._f01 + offset
        span = float(config.get("span", 400e6))
        points = int(config.get("points", 81))
        self._frequencies = setpoints_of(
            config,
            "frequencies",
            linear_setpoints(centre - span / 2, centre + span / 2, points),
        )

        clock = f"{target}.12"
        # A tenth, not the 3% this asked for before the simulator learned where |2>
        # lands. That default was tuned against an artefact: with |2> reported on
        # |0>'s cloud, a 5% population transfer swung the signal across the whole
        # readout axis and the line looked strong. Read correctly, |1> and |2> sit
        # close together at a 0-1 readout point and the same transfer is a 5% wiggle —
        # measured, and enough to put the fitted centre 7 MHz out.
        #
        # A tenth gives 26% contrast and lands within a megahertz. Not more: half a pi
        # pulse is already the point where `_drive_ef`'s neglected off-resonant 0-1
        # term starts to matter, and this routine only has to find the line for
        # `rabi_12` to refine.
        self._drive_amps = self._drive_amplitudes(config, device, target)
        # Recorded for `_widened` to clamp against, under the `_<axis>` convention it
        # reads setpoints by. Without it escalation walks straight past full scale and
        # the compiler refuses the waveform — `Rabi` carries the same line for the same
        # reason. A drive amplitude is a fraction of full scale, so that is the bound.
        self._drive_amps_ceiling = MAX_SPECTROSCOPY_AMPLITUDE
        duration = float(config.get("duration", 20e-9))
        schedule = backend.new_schedule(
            self.name, repetitions=int(config.get("shots", 1024))
        )
        index = 0
        for amplitude in self._drive_amps:
            for frequency in self._frequencies:
                schedule.add(backend.Reset(target))
                # Into |1> first, which is what makes this the *ef* transition rather
                # than a second look at 0-1.
                schedule.add(backend.X(target))
                schedule.add(
                    backend.SetClockFrequency(clock=clock, clock_freq_new=frequency)
                )
                schedule.add(
                    backend.SquarePulse(
                        amp=amplitude,
                        duration=duration,
                        port=f"{target}:mw",
                        clock=clock,
                    )
                )
                schedule.add(
                    backend.Measure(
                        target, acq_index=index, bin_mode=backend.BinMode.AVERAGE
                    )
                )
                index += 1
        return schedule

    #: Amplitudes to try, as fractions of the ceiling below.
    #:
    #: Downward only, and that is deliberate rather than timid. The failure this node has
    #: is saturation — a drive past the line's own width broadens it and drags the fitted
    #: centre — so the rows worth adding are *weaker* ones, and `fit_spectroscopy_power`
    #: keeps the narrowest credible of the set. A ladder that can only reduce power cannot
    #: do worse than the single amplitude it replaces; one that could raise it did, badly.
    #:
    #: **Anchoring this to `spec.amplitude` was tried in August 2026 and reverted.** The
    #: reasoning was tergite's: it sweeps 1e-3 to 8e-3 for 0-1 and 6e-3 to 3e-2 for 1-2, a
    #: ratio of 4.7 between the two optima, and a ratio travels between chips where an
    #: amplitude does not. But that ratio holds between two *unsaturated* optima. On a chip
    #: whose 0-1 line is only visible at 0.3 — saturated itself — 4.7x lands at full scale,
    #: and there f12 fitted a 55 MHz line at an anharmonicity of -329 MHz where every run
    #: before it agreed on -250. Multiplying a saturated anchor compounds the saturation.
    DRIVE_FACTORS = (0.2, 1.0 / math.sqrt(5.0), 1.0)

    def _drive_amplitudes(
        self, config: RoutineConfig, device: Any, target: str
    ) -> list[float]:
        """A ladder to sweep, rather than the one amplitude this used to fix.

        Swept and chosen for the same reason `qubit_spectroscopy` sweeps its own: the power
        that shows a line best is a property of the chip, and driving past it broadens the
        line and moves its centre. `fit_spectroscopy_power` drops the rows that broadened
        and ranks what is left — it was simply never given more than one row here.

        The top of the ladder is the amplitude this node used to fix, so the strongest row
        is exactly what it drove before and the two added rows are weaker. Any chip this
        already worked on keeps a row that worked; a chip it saturated gains two chances not
        to be.
        """
        if "drive_amps" in config:
            return setpoints_of(config, "drive_amps", [])
        ceiling = float(config.get("drive_amp", 0.10))
        return [
            min(factor * ceiling, MAX_SPECTROSCOPY_AMPLITUDE)
            for factor in self.DRIVE_FACTORS
        ]

    def analyse(
        self, dataset: xr.Dataset, target: str, device: Any, config: RoutineConfig
    ) -> dict[str, Any]:
        signal = signal_of(dataset)
        columns = len(self._frequencies)
        expected = len(self._drive_amps) * columns
        if signal.size < expected:
            raise RoutineError(
                f"f12 spectroscopy expected {expected} acquisitions for "
                f"{len(self._drive_amps)} amplitudes x {columns} frequencies, got "
                f"{signal.size}"
            )
        fitted = fit_spectroscopy_power(
            np.asarray(self._drive_amps),
            np.asarray(self._frequencies),
            signal[:expected].reshape(len(self._drive_amps), columns),
        )
        try:
            require_resolved_line(fitted, self._frequencies)
        except (FitError, RoutineError) as unresolved:
            # The prior stands. Nothing here can invent an f12, but the last one measured
            # is still the best available, and every ef node reads `clock_freqs.f12` — so
            # refusing publishes nothing *and* leaves them reading the same stale value
            # they would have read anyway, minus the report saying so.
            #
            # Only where there is a prior. On a chip that has never resolved this line
            # there is nothing to fall back on and the refusal is the whole answer.
            prior = float(
                read_path(device.get_element(target), "clock_freqs.f12") or 0.0
            )
            if not prior:
                raise
            log.warning(
                "%s: %s — keeping the f12 of %.6g Hz already measured on this qubit",
                target,
                unresolved,
                prior,
            )
            return {
                "clock_freq_12": prior,
                "anharmonicity": prior - self._f01,
                "unresolved": 1.0,
                "fit": fitted.get("fit"),
            }
        return {
            "clock_freq_12": fitted["clock_freq_01"],
            "drive_amplitude": fitted["drive_amplitude"],
            "linewidth": fitted["linewidth"],
            "quality_factor": fitted["quality_factor"],
            # Reported because it is the number a reader wants and nothing else
            # measures it: the anharmonicity is f12 - f01, and it sets both the DRAG
            # optimum and where |02> sits for a CZ.
            "anharmonicity": self._require_transmon_anharmonicity(
                fitted["clock_freq_01"] - self._f01, target
            ),
            "unresolved": 0.0,
        }

    @staticmethod
    def _require_transmon_anharmonicity(anharmonicity: float, target: str) -> float:
        """Refuse a fitted f12 whose distance from f01 is not a transmon's.

        The line may be real and still be the wrong line: a two-photon transition, a
        neighbour's, a spurious mode. What says which is the spacing, and this node is the
        only one that knows both frequencies — see :data:`ANHARMONICITY_RANGE_HZ` for the
        placeholder that made this necessary.
        """
        low, high = ANHARMONICITY_RANGE_HZ
        if not low <= anharmonicity <= high:
            raise RoutineError(
                f"{target}'s fitted f12 sits {anharmonicity / 1e6:.1f} MHz from its f01, "
                f"which is not a transmon's anharmonicity — they run {low / 1e6:.0f} to "
                f"{high / 1e6:.0f} MHz and are negative. The line found is real but it is "
                f"not the 1-2 transition: check that clock_freqs.f01 is right before "
                f"trusting anything above it"
            )
        return float(anharmonicity)

    def apply(self, device: Any, target: str, params: dict[str, Any]) -> None:
        write_path(
            device.get_element(target), "clock_freqs.f12", params["clock_freq_12"]
        )


class FluxSpectroscopy(CalibrationRoutine):
    """Map a qubit's frequency against its own flux bias — the input to a DC-flux CZ."""

    name = "flux_spectroscopy"
    depends_on = ("qubit_spectroscopy",)
    updates = ()
    reads = ("clock_freqs.f01", "rxy.amp180")

    def applies_to(self, device: Any, target: str) -> bool:
        """Only to a qubit the wiring carries a flux line to.

        This sweeps *this qubit's* flux and watches its own frequency move, which a
        chip whose flux reaches only the couplers cannot do — and has no need to,
        since its CZ is found in frequency by `cz_spectroscopy` rather than in
        amplitude by `cz_chevron`, the node this one feeds.
        """
        return has_flux_port(device, target)

    def acquire(
        self,
        target: str,
        device: Any,
        config: RoutineConfig,
        backend: SchedulerBackend,
        timeout_s: float,
    ) -> Any:
        """A row per flux offset, chunked when the grid outgrows one schedule."""
        return self.acquire_in_row_chunks(
            target, device, config, backend, timeout_s, rows_axis="flux_offsets"
        )

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        self._flux_offsets = setpoints_of(
            config, "flux_offsets", linear_setpoints(-0.2, 0.2, 11)
        )
        self._frequencies = _frequency_sweep(
            config, device, target, "f01", default_span=100e6, backend=backend
        )
        clock = f"{target}.01"
        duration = float(config.get("flux_duration", 200e-9))
        port = f"{target}:fl"
        schedule = backend.new_schedule(
            self.name, repetitions=int(config.get("shots", 512))
        )
        index = 0
        for offset in self._flux_offsets:
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
        for row in range(len(self._flux_offsets)):
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
            "flux_offsets": list(self._flux_offsets[: len(arc)]),
            "frequencies": arc,
            "sweet_spot_offset": float(self._flux_offsets[sweet_spot]),
            "sweet_spot_frequency": float(arc[sweet_spot]),
        }
