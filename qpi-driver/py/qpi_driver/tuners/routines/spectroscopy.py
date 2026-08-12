"""Spectroscopy: finding the resonator, its readout power, and the qubit (RFC 0004 §3).

These are the roots of the DAG — everything downstream needs a frequency to
drive at. Each sweeps a clock frequency across the scan and fits a Lorentzian to
the response.
"""

import logging
from typing import Any

import numpy as np
import xarray as xr

from qpi_driver.tuners.base.backend import SchedulerBackend
from qpi_driver.tuners.base.config import RoutineConfig
from qpi_driver.tuners.base.device import (
    has_flux_port,
    read_path,
    spectroscopy_amplitude_path,
    write_path,
)
from qpi_driver.tuners.base.routines import (
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
        fitted = fit_resonator_spectroscopy(self._frequencies, signal_of(dataset))
        # The root of the graph, and the one frequency every other node reads at.
        # It had no guard: a 72% dip confined to one 400 kHz bin was fitted as a
        # 2379 Hz linewidth at Q = 2.9 million, and the centre it wrote was 47 kHz
        # off the deepest sample it had actually measured.
        require_resolved_line(fitted, self._frequencies)
        return fitted

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
            config
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

    def _check_linewidth(self, config: RoutineConfig) -> float:
        return float(config.get("check_linewidth", self.CHECK_LINEWIDTH_HZ))

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
        allowed = self._check_linewidth(config) * float(
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

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        self._frequencies = _frequency_sweep(
            config, device, target, "readout", default_span=20e6
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

    #: Drive powers to compare, as a fraction of full scale. Wide, because on a first
    #: bring-up nothing yet says which end of it the chip wants.
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

    #: One power for the widening pass, the strongest this routine would try anyway.
    #: Locating a line does not need powers compared, and five of them across 301
    #: frequencies is 1505 acquisitions — past what a sequencer will assemble.
    SEARCH_AMPLITUDE = 0.08

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

        found = self._search(target, device, config, backend, timeout_s)
        widened = RoutineConfig(
            enabled=config.enabled,
            params={**config.params, "centre_frequency": found},
        )
        try:
            return self._sweep(target, device, widened, backend, timeout_s)
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
        """The ordinary pass: build, run, fit, and refuse anything unresolved."""
        schedule = self.build_schedule(target, device, config, backend)
        dataset = backend.run(schedule, timeout_s=timeout_s)
        return self.analyse(dataset, target, device, config)

    def _search(
        self,
        target: str,
        device: Any,
        config: RoutineConfig,
        backend: SchedulerBackend,
        timeout_s: float,
    ) -> float:
        """Where the strongest line in a wide window is, to point the narrow sweep at."""
        span = float(config.get("search_span", self.SEARCH_SPAN))
        points = int(config.get("search_points", self.SEARCH_POINTS))
        amplitude = float(config.get("search_amp", self.SEARCH_AMPLITUDE))
        centre = _current_clock(device, target, "f01")
        frequencies = linear_setpoints(centre - span / 2, centre + span / 2, points)

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
        # signal-to-noise of 3, which cleared `MIN_LINE_SNR` in this simulator and would
        # have sent the narrow pass to an arbitrary frequency. Measured: a real line
        # reaches 115-126 by the ratio below, and pure noise 2.5-2.9.
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
        log.info(
            "%s: %s's strongest line is at %.0f Hz, %.0f MHz from the configured f01, "
            "%.1fx over the scatter",
            self.name,
            target,
            found,
            (found - centre) / 1e6,
            reach,
        )
        return found

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        return self._probe_schedule(
            target,
            _frequency_sweep(config, device, target, "f01", default_span=40e6),
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
        self._amplitudes = amplitudes

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
        expected = len(self._amplitudes) * columns
        if signal.size < expected:
            raise RoutineError(
                f"qubit spectroscopy expected {expected} acquisitions, got {signal.size}"
            )
        rows = signal[:expected].reshape(len(self._amplitudes), columns)
        fitted = fit_spectroscopy_power(self._amplitudes, self._frequencies, rows)
        require_resolved_line(fitted, self._frequencies)
        return fitted

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
        amplitude = float(config.get("drive_amp", 0.10))
        duration = float(config.get("duration", 20e-9))
        schedule = backend.new_schedule(
            self.name, repetitions=int(config.get("shots", 1024))
        )
        for index, frequency in enumerate(self._frequencies):
            schedule.add(backend.Reset(target))
            # Into |1> first, which is what makes this the *ef* transition rather than
            # a second look at 0-1.
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
        return schedule

    def analyse(
        self, dataset: xr.Dataset, target: str, device: Any, config: RoutineConfig
    ) -> dict[str, Any]:
        fitted = fit_qubit_spectroscopy(self._frequencies, signal_of(dataset))
        require_resolved_line(fitted, self._frequencies)
        return {
            "clock_freq_12": fitted["clock_freq_01"],
            "linewidth": fitted["linewidth"],
            "quality_factor": fitted["quality_factor"],
            # Reported because it is the number a reader wants and nothing else
            # measures it: the anharmonicity is f12 - f01, and it sets both the DRAG
            # optimum and where |02> sits for a CZ.
            "anharmonicity": fitted["clock_freq_01"] - self._f01,
        }

    def apply(self, device: Any, target: str, params: dict[str, Any]) -> None:
        write_path(
            device.get_element(target), "clock_freqs.f12", params["clock_freq_12"]
        )


class FluxSpectroscopy(CalibrationRoutine):
    """Map a qubit's frequency against its own flux bias — the input to a DC-flux CZ."""

    name = "flux_spectroscopy"
    depends_on = ("qubit_spectroscopy",)
    updates = ()

    def applies_to(self, device: Any, target: str) -> bool:
        """Only to a qubit the wiring carries a flux line to.

        This sweeps *this qubit's* flux and watches its own frequency move, which a
        chip whose flux reaches only the couplers cannot do — and has no need to,
        since its CZ is found in frequency by `cz_spectroscopy` rather than in
        amplitude by `cz_chevron`, the node this one feeds.
        """
        return has_flux_port(device, target)

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
