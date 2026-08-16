"""Calibrating the readout chain itself (RFC 0005 §7).

The routines in `spectroscopy` find the resonator and how hard to drive it. This one
answers the question that comes after: given the two clouds the readout produces,
which line separates them?

That line is `measure.acq_rotation` and `measure.acq_threshold`, and until now
nothing produced them. They default to zero, a working chip's device config does not
carry them at all, and **every** ``meas_level=2`` shot is assigned by comparing a
rotated real part against them — so the default job path was discriminating with an
uncalibrated rule. Zero is correct only for a readout chain that happens to place the
clouds either side of the imaginary axis, which nothing arranges.

This needs the state-resolved readout the simulator gained alongside it: with two
clouds placed by hand there was no rotation to find, because the placement *was* the
answer.
"""

from typing import Any

import math

import numpy as np
import xarray as xr

from qpi_driver.tuners.base.backend import SchedulerBackend
from qpi_driver.tuners.base.config import RoutineConfig
from qpi_driver.tuners.base.device import (
    measured_linewidth,
    read_path,
    write_path,
)
from qpi_driver.tuners.base.routines import (
    CalibrationRoutine,
    CheckOutcome,
    RoutineError,
    grid_duration,
    linear_setpoints,
    setpoints_of,
)
from qpi_driver.tuners.fitting import (
    fit_readout_discrimination,
    fit_readout_integration_time,
    fit_readout_operating_point,
)

#: Where a `CalibratedTransmon` keeps the readout point used for discriminating. An
#: element without it is not a failure — the routines fall back to `measure`, which is
#: what every config written before that element has.
TWO_STATE = "measure_2state"


def _two_state_path(element: Any, name: str) -> str | None:
    """``measure_2state.<name>`` if this element has one, else ``None``."""
    submodule = getattr(element, TWO_STATE, None)
    if submodule is None or not hasattr(submodule, name):
        return None
    return f"{TWO_STATE}.{name}"


#: How wide an operating-point sweep is, as a fraction of the measured resonator
#: linewidth. Derived from two chips that agree: see `_grid`.
SPAN_IN_LINEWIDTHS = 0.6


class ReadoutOperatingPoint(CalibrationRoutine):
    """Where to interrogate the resonator, and how hard, so the states look least alike.

    Not where `resonator_spectroscopy` and `resonator_punchout` put it. Those find
    where the most signal comes back, which is the right question for every routine
    that reduces an acquisition to a magnitude — nearly all of them. A discriminator
    uses the *complex* separation between the clouds, and once the drive is off
    resonance most of that is phase, which a magnitude sees none of. The two answers
    genuinely differ: 2.6% more separation for 14% less contrast, measured, which
    moved the CZ chevron's answer by 10 ns the one time both readouts shared a point.

    So this writes its own, and the executor uses it only for ``meas_level=2``.

    One node over both axes rather than one each, because the resonance moves with
    power. Choosing a frequency and then a power leaves the frequency stale — 183 kHz
    on the simulated chip, a tenth of a linewidth — which is the mistake
    `resonator_punchout` already exists to not make.
    """

    name = "readout_operating_point"
    depends_on = ("readout_integration_time",)
    updates = (f"{TWO_STATE}.frequency", f"{TWO_STATE}.pulse_amp")
    reads = (
        "clock_freqs.readout",
        "measure.pulse_amp",
        "resonator.linewidth",
        "clock_freqs.f01",
        "rxy.amp180",
    )

    def applies_to(self, device: Any, target: str) -> bool:
        """Only to an element that can keep a discriminated readout point.

        A `BasicTransmonElement` cannot, and a chip using one is not misconfigured —
        it discriminates at the calibration point, which is what every config written
        before `measure_2state` existed does. Running the sweep and discarding the
        answer would spend the shots and change nothing.
        """
        return _two_state_path(device.get_element(target), "frequency") is not None

    #: Multiples of the readout power punchout chose. Most of the grid goes here
    #: rather than on frequency, and that split is measured rather than assumed: the
    #: frequency optimum sits about a tenth of a linewidth off the resonance, so that
    #: axis is nearly flat, while the amplitude has a real interior optimum — signal
    #: grows linearly with drive and punch-through only bends it.
    AMPLITUDE_FACTORS = (0.5, 0.75, 1.0, 1.25, 1.5)

    #: How many single-shot acquisitions one schedule may ask for.
    #:
    #: An instrument constraint, not a preference. Every appended bin takes a
    #: sequencer register, and a Q1 sequencer has 64 in total — the rest go to loop
    #: counters and the acquisition machinery, so half is the headroom this leaves.
    #: Past it the qblox backend dies inside its register allocator with a bare
    #: `IndexError`, a long way from the sweep that asked for too much.
    #:
    #: `resonator_punchout` sweeps a far larger grid and is unaffected because it
    #: averages: an averaged bin costs no register. This cannot average — the *width*
    #: of each cloud is exactly what it measures.
    MAX_SINGLE_SHOT_ACQUISITIONS = 32

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        element = device.get_element(target)
        self._settings = self._grid(element, config)
        shots = int(config.get("shots", 300))
        schedule = backend.new_schedule(self.name, repetitions=shots)
        index = 0
        for frequency, amplitude in self._settings:
            schedule.add(
                backend.SetClockFrequency(
                    clock=f"{target}.ro", clock_freq_new=frequency
                )
            )
            for prepare in (0, 1):
                schedule.add(backend.Reset(target))
                if prepare:
                    schedule.add(backend.X(target))
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
        # A refinement, not a scan: the optimum sits a fraction of a linewidth off the
        # resonance, so a wide span spends the register budget resolving nothing. Sized
        # from the linewidth `resonator_spectroscopy` measured rather than from a
        # constant, and 0.6 of it because that is what two independent chips agree on —
        # the 2 MHz default that worked on the simulated chip is 0.60 of its measured
        # 3.31 MHz, and the 200 kHz an operator hand-tuned on a 370 kHz resonator is
        # 0.54 of that. The same constant was 5.4 linewidths on the second chip, which
        # put the outer setpoints off resonance altogether and the node chose one.
        span = float(
            config.get("span", SPAN_IN_LINEWIDTHS * measured_linewidth(element, 2e6))
        )
        points = int(config.get("points", 3))
        frequencies = (
            setpoints_of(config, "frequencies", [])
            if "frequencies" in config
            else linear_setpoints(centre - span / 2, centre + span / 2, points)
        )
        current = float(read_path(element, "measure.pulse_amp"))
        amplitudes = (
            setpoints_of(config, "amplitudes", [])
            if "amplitudes" in config
            else [min(factor * current, 1.0) for factor in self.AMPLITUDE_FACTORS]
        )
        grid = [(f, a) for a in amplitudes for f in frequencies]
        if 2 * len(grid) > self.MAX_SINGLE_SHOT_ACQUISITIONS:
            raise RoutineError(
                f"{len(frequencies)} frequencies x {len(amplitudes)} amplitudes needs "
                f"{2 * len(grid)} single-shot acquisitions, and a sequencer has "
                f"registers for {self.MAX_SINGLE_SHOT_ACQUISITIONS}. Narrow the grid: "
                f"this looks for a broad optimum, not a sharp one."
            )
        return grid

    def analyse(
        self, dataset: xr.Dataset, target: str, device: Any, config: RoutineConfig
    ) -> dict[str, Any]:
        ground, excited = _swept_clouds(dataset, len(self._settings))
        return fit_readout_operating_point(self._settings, ground, excited)

    def apply(self, device: Any, target: str, params: dict[str, Any]) -> None:
        element = device.get_element(target)
        for name, key in (
            ("frequency", "readout_frequency"),
            ("pulse_amp", "readout_amplitude"),
        ):
            path = _two_state_path(element, name)
            if path:
                write_path(element, path, params[key])


class ReadoutIntegrationTime(CalibrationRoutine):
    """How long to integrate the readout — the last free parameter in its signal-to-noise.

    Signal accumulates with the window and noise only with its square root, so separation
    climbs as ``sqrt(t)`` until the qubit starts relaxing inside the window, after which a
    longer one only adds shots of the wrong state. That crossing is set by T1 and chi, so
    it is a property of the chip and cannot be a constant. Left as one it was whatever the
    config happened to be written with, and every discriminating node inherited it.

    `resonator_relaxation` measures the ring-up and deliberately does not write this — the
    ring-up is a *floor*, a statement about the resonator that mentions neither noise nor
    relaxation. Its docstring asks for this node by name, and the discrimination fidelity
    it was waiting on now exists.

    Ahead of the rest of the readout chain, because every node after it measures a
    separation that this scales. Choosing it afterwards would restate the same sweeps.
    """

    name = "readout_integration_time"
    depends_on = ("rabi",)
    updates = ("measure.integration_time",)
    reads = (
        "measure.integration_time",
        "measure.pulse_duration",
        "measure.acq_delay",
        "resonator.linewidth",
        "clock_freqs.readout",
        "clock_freqs.f01",
        "rxy.amp180",
    )

    #: How far below the window the config arrived with the ladder starts, and the ratio
    #: between its rungs. It runs from there to the hardware ceiling.
    #:
    #: Spanning the whole reachable range rather than a fixed few factors either side. A
    #: ladder that stops short can only report its own top rung, which is not a measured
    #: optimum but the edge of where it looked — and that is what the 2026-08-15 B chip
    #: returned, choosing 3.6 us at the top of a ladder reaching exactly 3.6 us.
    #:
    #: Nothing forbids the width here: each rung is its own schedule, because a Qblox
    #: program takes one integration length, so the two-acquisition register budget that
    #: bounds every other sweep in this file does not apply across rungs. What it costs is
    #: one short schedule per rung, and the whole reachable range is six or seven of them.
    WINDOW_FLOOR_FACTOR = 0.25
    WINDOW_STEP = 2.0

    #: A bound on runtime rather than on physics, for a config whose window is so far under
    #: the ceiling that doubling to it would take all afternoon.
    MAX_WINDOWS = 12

    #: How far past the readout pulse a window may still reach, in resonator time constants.
    #:
    #: The real ceiling on this axis is not the instrument, it is the pulse: once the drive
    #: stops there is no more signal to integrate, only the resonator ringing down and then
    #: noise. Integrating past it lowers SNR — the numerator stops growing and the
    #: denominator does not.
    #:
    #: The 2026-08-16 run is what this is for. q5's readout pulse is 3.8 us behind a 200 ns
    #: delay, so 3.6 us is every sample that carries anything, and the sweep — reaching to
    #: the instrument's 16.384 us because nothing told it otherwise — chose 7.2. Half of
    #: that window was noise. Discrimination still improved, because it came from 0.9 us and
    #: gained more signal than it lost, but every *magnitude* node paid: contrast fell 27%,
    #: `qubit_spectroscopy` fitted a 573 MHz linewidth on a transmon whose anharmonicity is
    #: 253, and `f12_spectroscopy` stopped seeing its line at all.
    #:
    #: Two time constants of headroom rather than none, because the ring-down does carry
    #: signal: a 322 kHz linewidth rings for about a microsecond, and cutting exactly at the
    #: pulse would throw that away.
    RINGDOWN_TIME_CONSTANTS = 2.0

    #: Longest acquisition a Qblox sequencer integrates into one bin.
    #:
    #: A hardware ceiling rather than a physical one — the optimum normally sits far below
    #: it — and it is here so the sweep clamps rather than the backend raising from inside
    #: its own allocator. Other hardware overrides it through ``max_integration_time``.
    MAX_INTEGRATION_TIME_S = 16.384e-6

    def applies_to(self, device: Any, target: str) -> bool:
        """Only where there is an integration time to write."""
        measure = getattr(device.get_element(target), "measure", None)
        return measure is not None and hasattr(measure, "integration_time")

    def acquire(
        self,
        target: str,
        device: Any,
        config: RoutineConfig,
        backend: SchedulerBackend,
        timeout_s: float,
    ) -> Any:
        """One schedule per window, because a schedule may only have one of them.

        Not a chunking optimisation like `rb`'s — a hardware rule. Every square
        acquisition compiled into one Qblox program shares an integration length, and a
        second one raises from inside the backend: "attempting to set an integration_length
        of 500 ns, while this was previously determined to be 250". So the axis this node
        exists to sweep is the one axis that cannot be swept within a schedule.

        The windows are concatenated in order, so `analyse` unpacks them exactly as it
        would one schedule's worth of settings.
        """
        windows = self._grid(device.get_element(target), config)
        rows = []
        for window in windows:
            single = RoutineConfig(
                enabled=config.enabled,
                params={**config.params, "windows": [window]},
            )
            dataset = super().acquire(target, device, single, backend, timeout_s)
            # ``(shots, 2)`` — the two prepared states of this one window. Kept 2-D,
            # because the shots *are* the measurement here: their spread is the noise the
            # separation is quoted in, and flattening them reads as one shot per state.
            values = np.atleast_2d(_acquisition_values(dataset))
            if values.shape[-1] < 2:
                raise RoutineError(
                    f"window {window:.4g} s returned {values.shape[-1]} acquisitions, "
                    f"expected |0> and |1>"
                )
            rows.append(values[..., :2])
        self._windows = windows
        # Side by side, so the acquisition axis unpacks as |0>,|1> per window — the same
        # interleaving `_swept_clouds` expects from a single-schedule sweep.
        return xr.Dataset(
            {"y0": (("shot", "acq_index"), np.concatenate(rows, axis=-1))}
        )

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        """``|0>`` and ``|1>`` at *one* window — see :meth:`acquire` for why only one."""
        windows = self._grid(device.get_element(target), config)
        self._windows = windows[:1]
        schedule = backend.new_schedule(
            self.name, repetitions=int(config.get("shots", 300))
        )
        for index, prepare in enumerate((0, 1)):
            schedule.add(backend.Reset(target))
            if prepare:
                schedule.add(backend.X(target))
            schedule.add(
                backend.Measure(
                    target,
                    acq_index=index,
                    bin_mode=backend.BinMode.APPEND,
                    acq_duration=self._windows[0],
                )
            )
        return schedule

    def _grid(self, element: Any, config: RoutineConfig) -> list[float]:
        ceiling = float(
            config.get("max_integration_time", self._usable_window(element))
        )
        if "windows" in config:
            windows = [float(w) for w in setpoints_of(config, "windows", [])]
        else:
            current = float(read_path(element, "measure.integration_time"))
            if not current:
                raise RoutineError(
                    "no measure.integration_time to scale a sweep from; set an explicit "
                    "'windows' for this routine"
                )
            # Doubling from below the incumbent up to the ceiling, so the chosen rung is an
            # optimum the sweep bracketed rather than the edge it stopped at.
            windows = []
            window = current * self.WINDOW_FLOOR_FACTOR
            while window <= ceiling and len(windows) < self.MAX_WINDOWS:
                windows.append(window)
                window *= self.WINDOW_STEP
            windows.append(ceiling)
        # Clamped first, then the incumbent joins *unclamped* — which is the whole point of
        # it being here. A config integrating past its own pulse is exactly what this node
        # should shorten, and it can only shorten on evidence if the value being replaced
        # was measured beside the alternatives. Clamp it and the change becomes an
        # assumption, while the tie-hold that protects a flat landscape loses the one rung
        # it compares against: the simulated chip integrates 1 us behind a 300 ns pulse, and
        # with the incumbent clamped away the sweep quartered it on nothing but noise.
        #
        # Deduplicated after rounding, or a config already at the ceiling sweeps one window
        # twice and the winner reads as a choice the ladder made.
        windows = [min(w, ceiling) for w in windows if w > 0.0]
        if "windows" not in config:
            windows.append(float(read_path(element, "measure.integration_time")))
        windows = sorted({grid_duration(w) for w in windows if w > 0.0})
        if not windows:
            raise RoutineError("readout integration sweep is empty")
        needed = 2 * len(windows)
        if needed > ReadoutOperatingPoint.MAX_SINGLE_SHOT_ACQUISITIONS:
            raise RoutineError(
                f"{len(windows)} windows needs {needed} single-shot acquisitions, past "
                f"the {ReadoutOperatingPoint.MAX_SINGLE_SHOT_ACQUISITIONS} a sequencer "
                f"has registers for"
            )
        return windows

    def _usable_window(self, element: Any) -> float:
        """The longest window that still carries signal, and the instrument's own limit.

        The pulse is the real ceiling here — see :attr:`RINGDOWN_TIME_CONSTANTS`. Reading it
        rather than assuming it, because it is a chip fact: a 3.8 us pulse and a 1 us one
        want windows an octave apart, and neither is wrong.

        Falls back to the instrument limit when the element cannot say, which keeps this
        working on a `BasicTransmonElement` and on any config predating these fields.
        """
        instrument = self.MAX_INTEGRATION_TIME_S
        try:
            pulse = float(read_path(element, "measure.pulse_duration"))
            delay = float(read_path(element, "measure.acq_delay"))
        except Exception:  # noqa: BLE001 - an unreadable pulse is not a shorter one
            return instrument
        if pulse <= 0.0:
            return instrument
        driven = pulse - max(delay, 0.0)
        if driven <= 0.0:
            return instrument
        linewidth = measured_linewidth(element, 0.0)
        ringdown = (
            self.RINGDOWN_TIME_CONSTANTS / (math.pi * linewidth) if linewidth else 0.0
        )
        return min(instrument, driven + ringdown)

    def analyse(
        self, dataset: xr.Dataset, target: str, device: Any, config: RoutineConfig
    ) -> dict[str, Any]:
        ground, excited = _swept_clouds(dataset, len(self._windows))
        return fit_readout_integration_time(
            self._windows,
            ground,
            excited,
            incumbent=float(
                read_path(device.get_element(target), "measure.integration_time")
            ),
        )

    def apply(self, device: Any, target: str, params: dict[str, Any]) -> None:
        write_path(
            device.get_element(target),
            "measure.integration_time",
            params["integration_time"],
        )


class ReadoutDiscrimination(CalibrationRoutine):
    """Prepare ``|0>`` and ``|1>``, then find the line that tells them apart.

    Depends on `rabi` rather than sitting with the other readout routines, and that
    is the ordering point RFC 0005 §1 makes: preparing ``|1>`` needs a calibrated π
    pulse, so the readout chain cannot be finished before the qubit chain starts. It
    straddles it.

    Reports the assignment fidelity from the same shots rather than leaving it to a
    separate node. Splitting them would mean measuring the same two clouds twice, and
    the fidelity is exactly what says whether the fitted line is any good.
    """

    name = "readout_discrimination"
    depends_on = ("readout_operating_point",)
    updates = ("measure.acq_rotation", "measure.acq_threshold")
    reads = (
        "measure_2state.frequency",
        "measure_2state.pulse_amp",
        "clock_freqs.f01",
        "rxy.amp180",
    )

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        shots = int(config.get("shots", 2000))
        schedule = backend.new_schedule(f"{self.name}", repetitions=shots)
        point = _operating_point(device.get_element(target))
        if point:
            # At the point that will be used, not at the one the calibration reads on.
            # A line fitted where the clouds are not is a line fitted somewhere else,
            # which is the whole reason this depends on `readout_operating_point`.
            schedule.add(
                backend.SetClockFrequency(
                    clock=f"{target}.ro", clock_freq_new=point["frequency"]
                )
            )
        measure_kwargs = {"pulse_amp": point["pulse_amp"]} if point else {}
        # Single shots, not an average: the whole measurement is the *distribution* of
        # each cloud, and its width is what sets the threshold and the fidelity. An
        # averaged acquisition gives two points and no way to say how often they are
        # confused.
        for index, prepare in enumerate((0, 1)):
            schedule.add(backend.Reset(target))
            if prepare:
                schedule.add(backend.X(target))
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
        ground, excited = _shot_clouds(dataset)
        return fit_readout_discrimination(ground, excited)

    def apply(self, device: Any, target: str, params: dict[str, Any]) -> None:
        element = device.get_element(target)
        # Beside the operating point it was measured at, when there is one — and the
        # condition is that the point is *calibrated*, not merely that the element
        # could hold one. `readout_operating_point` can be disabled in a config, and
        # then the schedule above reads on the calibration point; writing the line
        # into `measure_2state` anyway would put it exactly where the executor does
        # not look, since it falls back on an uncalibrated point too.
        two_state = _operating_point(element) is not None
        for name in ("acq_rotation", "acq_threshold"):
            path = (two_state and _two_state_path(element, name)) or f"measure.{name}"
            write_path(element, path, params[name])

    #: Assignment fidelity below which the discriminator counts as stale. A readout
    #: that has drifted far enough to misassign one shot in twenty is worth
    #: re-fitting; tighter than this and normal shot noise on a few thousand shots
    #: would trip it.
    CHECK_MIN_FIDELITY = 0.95

    def build_check_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        """The same experiment with fewer shots — the one case where that is right.

        A check is normally a *different* experiment because a cheaper version of the
        calibration usually cannot answer the question. Here it can: the question is
        "how often does the stored line misassign a shot?", and that is answered by
        counting misassignments over a few hundred shots rather than the few thousand
        needed to place the line precisely.
        """
        return self.build_schedule(
            target,
            device,
            RoutineConfig(params={"shots": int(config.get("check_shots", 400))}),
            backend,
        )

    def analyse_check(
        self, dataset: xr.Dataset, target: str, device: Any, config: RoutineConfig
    ) -> CheckOutcome:
        ground, excited = _shot_clouds(dataset)
        fitted = fit_readout_discrimination(ground, excited)
        floor = float(config.get("check_min_fidelity", self.CHECK_MIN_FIDELITY))
        fidelity = float(fitted["assignment_fidelity"])
        return CheckOutcome(
            passed=fidelity >= floor,
            margin=(1.0 - fidelity) / max(1.0 - floor, 1e-9),
            detail=f"assignment fidelity {fidelity:.4f}",
        )


def _swept_clouds(dataset: Any, points: int) -> tuple[np.ndarray, np.ndarray]:
    """The two clouds at each setting, as ``(points, shots)`` complex arrays.

    The schedule interleaves them — ``|0>`` then ``|1>`` at each setting — so the
    acquisition axis unpacks as pairs, not as two halves.
    """
    values = _acquisition_values(dataset)
    if values.shape[-1] < 2 * points:
        raise RoutineError(
            f"expected {2 * points} acquisitions for {points} settings, got "
            f"{values.shape[-1]} — one prepared state per setting is missing"
        )
    shots = values[..., : 2 * points].reshape(-1, points, 2)
    return shots[..., 0].T, shots[..., 1].T


def _acquisition_values(dataset: Any) -> np.ndarray:
    data_vars = getattr(dataset, "data_vars", None)
    if data_vars is None or not list(data_vars):
        raise RoutineError("the discrimination acquisition returned no data variables")
    return np.asarray(dataset[list(data_vars)[0]].values)


class ReadoutFidelity(CalibrationRoutine):
    """How often the calibrated discriminator assigns a shot correctly.

    A node rather than a field on `readout_discrimination`, which is where this
    number used to live. The two arguments pull opposite ways and the deciding one is
    not tidiness:

    - Against: it measures the same two clouds twice, so the chip pays for the shots
      again.
    - For: **only a node participates in drift monitoring.** A benchmark is what a
      drift check runs and what queues a recalibration when it falls; a value buried
      in another routine's reported parameters is read by nobody and triggers
      nothing. Readout fidelity is exactly the quantity that degrades quietly — every
      gate fidelity measured on top of it inherits the error — so it is the last one
      that should be invisible to monitoring.

    The shots are the cost of that, and they are cheap next to RB.

    It writes nothing, like every other benchmark. `readout_discrimination` still
    reports its own fidelity from the shots it already has, which is what says
    whether the line it just fitted is any good; this is the standing measurement of
    whether the line still is.
    """

    name = "readout_fidelity"
    depends_on = ("readout_discrimination",)
    updates = ()
    benchmark = True
    reads = (
        "measure_2state.frequency",
        "measure_2state.pulse_amp",
        "clock_freqs.f01",
        "rxy.amp180",
    )

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        shots = int(config.get("shots", 2000))
        schedule = backend.new_schedule(self.name, repetitions=shots)
        point = _operating_point(device.get_element(target))
        if point:
            # Where the discriminator was fitted, which is where the shots it grades
            # will be taken. Reading anywhere else measures a line against clouds it
            # was not drawn for.
            schedule.add(
                backend.SetClockFrequency(
                    clock=f"{target}.ro", clock_freq_new=point["frequency"]
                )
            )
        measure_kwargs = {"pulse_amp": point["pulse_amp"]} if point else {}
        for index, prepare in enumerate((0, 1)):
            schedule.add(backend.Reset(target))
            if prepare:
                schedule.add(backend.X(target))
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
        ground, excited = _shot_clouds(dataset)
        fitted = fit_readout_discrimination(ground, excited)
        # Named `fidelity` as well, because that is the key a benchmark is read by.
        return {"fidelity": fitted["assignment_fidelity"], **fitted}

    def apply(self, device: Any, target: str, params: dict[str, Any]) -> None:
        """Nothing to write — a benchmark measures, it does not tune."""


def _shot_clouds(dataset: Any) -> tuple[np.ndarray, np.ndarray]:
    """The ``|0>`` and ``|1>`` shot clouds, as complex arrays.

    `signal_of` is the wrong reducer: it takes magnitudes and flattens, and both the
    rotation and the threshold live in the complex plane. Nor can the two clouds be
    flattened together — which shot belongs to which prepared state is the entire
    content of the measurement.
    """
    values = _acquisition_values(dataset)
    if values.ndim < 2 or values.shape[-1] < 2:
        raise RoutineError(
            f"expected single shots of two prepared states, got shape {values.shape} "
            "— the acquisition was averaged rather than appended, so the width of "
            "each cloud is gone and no threshold can be placed"
        )
    return values[..., 0].reshape(-1), values[..., 1].reshape(-1)


def _operating_point(element: Any) -> dict[str, float] | None:
    """The frequency and amplitude discriminated shots are taken at, if calibrated.

    Zero frequency means nothing has run `readout_operating_point` yet, and the
    readout stays where the device config put it — the same fallback the executor
    makes, and it has to be the same one or the line is fitted somewhere the shots
    will not be taken.
    """
    paths = {
        name: _two_state_path(element, name) for name in ("frequency", "pulse_amp")
    }
    if not all(paths.values()):
        return None
    point = {name: float(read_path(element, path)) for name, path in paths.items()}
    if point["frequency"] <= 0.0 or point["pulse_amp"] <= 0.0:
        return None
    return point
