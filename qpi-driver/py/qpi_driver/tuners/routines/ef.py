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

import numpy as np
import xarray as xr

from qpi_driver.tuners.base.backend import SchedulerBackend
from qpi_driver.tuners.base.config import RoutineConfig
from qpi_driver.tuners.base.device import read_path, write_path
from qpi_driver.tuners.base.routines import (
    CalibrationRoutine,
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
    fit_resonator_spectroscopy,
    fit_three_state_discrimination,
    fit_three_state_operating_point,
    signal_of,
)

#: Where a `CalibratedTransmon` keeps its EF pulse.
EF = "r12"

#: And the readout point that resolves all three levels.
THREE_STATE = "measure_3state"

#: Fallback length of an EF pulse, matching the 20 ns `rxy` plays. The amplitude a
#: sweep reports only means anything alongside a duration — "the amplitude that turns
#: pi" is a statement about a particular pulse length.
DEFAULT_EF_DURATION = 20e-9


def ef_path(element: Any, name: str) -> str | None:
    """``r12.<name>`` if this element has one, else ``None``."""
    submodule = getattr(element, EF, None)
    if submodule is None or not hasattr(submodule, name):
        return None
    return f"{EF}.{name}"


def ef_duration(element: Any, config: RoutineConfig) -> float:
    """How long an EF pulse plays, from the config, the element, or the default."""
    if "duration" in config:
        return float(config["duration"])
    path = ef_path(element, "ef_duration")
    if path:
        stored = read_path(element, path)
        if stored:
            return float(stored)
    return DEFAULT_EF_DURATION


def add_ef_pulse(
    schedule: Any,
    backend: SchedulerBackend,
    target: str,
    amplitude: float,
    duration: float,
    phase_deg: float = 0.0,
    drag: float = 0.0,
) -> None:
    """One pulse on the ``.12`` clock, into the port ``rxy`` uses.

    Square by default, and shaped as soon as a phase or a DRAG coefficient is asked
    for — `SquarePulse` carries neither. The pulse *area* is what sets the rotation
    and the simulator normalises a shaped envelope to unit mean, so a Gaussian of the
    same amplitude and duration turns the same angle: `ef_amp180` keeps its meaning
    across the switch.

    *drag* is in the backend's own units, which differ between the two schedulers —
    see `SchedulerBackend.drag_pulse`.
    """
    clock = f"{target}.12"
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

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        element = device.get_element(target)
        if ef_path(element, "ef_amp180") is None:
            raise RoutineError(
                f"{target} has no '{EF}' submodule to write an EF pi pulse to — this "
                f"routine needs a CalibratedTransmon, see the element's docstring"
            )
        self._amplitudes = setpoints_of(
            config, "amplitudes", linear_setpoints(0.0, 0.5, 41)
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
            schedule.add(
                backend.Measure(
                    target, acq_index=index, bin_mode=backend.BinMode.AVERAGE
                )
            )
        return schedule

    def analyse(
        self, dataset: xr.Dataset, target: str, device: Any, config: RoutineConfig
    ) -> dict[str, Any]:
        fitted = fit_rabi(np.asarray(self._amplitudes), signal_of(dataset))
        return {"ef_amp180": fitted["amp180"], "ef_duration": self._duration}

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
    depends_on = ("rabi_12", "readout_operating_point")
    updates = (f"{THREE_STATE}.frequency", f"{THREE_STATE}.pulse_amp")

    #: Two, not three. The register budget buys ten settings and they are better
    #: spent on frequency: the amplitude runs to the top of whatever range it is
    #: given — the separation peaks near twice full scale — while the frequency is
    #: where the choice actually is.
    AMPLITUDE_FACTORS = (1.0, 1.5)

    #: Three prepared states per setting against the register budget
    #: `ReadoutOperatingPoint` explains, so this grid is smaller than that node's.
    MAX_SINGLE_SHOT_ACQUISITIONS = 32

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
        # Five points across six megahertz. Three stepped 3 MHz against a 2 MHz
        # linewidth, which found a point good enough to classify three states and not
        # good enough for anything measured *at* it: `ramsey_12` reads |1> against
        # |2>, and on the coarse point its f12 came back a megahertz out where a
        # properly placed one gives kilohertz.
        span = float(config.get("span", 6e6))
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

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        element = device.get_element(target)
        amplitude = _required_ef_amplitude(element, target)
        duration = ef_duration(element, config)
        centre = float(read_path(element, "clock_freqs.readout"))
        span = float(config.get("span", 20e6))
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
        second = fitted["readout_frequency"]
        ground = float(read_path(device.get_element(target), "clock_freqs.readout"))
        return {
            "readout_frequency_second_excited": second,
            # Quarter of the gap, because |0> sits at +chi and |2> at -3chi: four
            # dispersive shifts apart. Equal to the shift the excited-state sweep
            # reports if the ladder is linear, and that equality is the measurement.
            "dispersive_shift": 0.25 * (second - ground),
            "linewidth": fitted["linewidth"],
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

    Reads at the three-state point, and needs to. Its two reference states are ``|1>``
    and ``|2>``, and at a 0-1 readout those two are all but on top of each other — the
    same 5% wiggle that put `f12_spectroscopy` 7 MHz out before its drive was raised.
    At the three-state point they are 14 sigma apart in magnitude alone, which is what
    makes this measurable at all.
    """

    name = "fine_amplitude_12"
    depends_on = ("three_state_operating_point",)
    updates = (f"{EF}.ef_amp180",)

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        element = device.get_element(target)
        self._amplitude = _required_ef_amplitude(element, target)
        self._duration = ef_duration(element, config)
        self._counts = [
            int(n) for n in setpoints_of(config, "repetitions", list(range(1, 26)))
        ]

        schedule = backend.new_schedule(
            self.name, repetitions=int(config.get("shots", 1024))
        )
        measure = open_three_state_readout(schedule, backend, target, element)

        for index, count in enumerate(self._counts):
            schedule.add(backend.Reset(target))
            schedule.add(backend.X(target))
            # Half the amplitude is half the rotation: the drive is linear in it at
            # fixed duration, which is the same assumption `rabi_12` fits under.
            add_ef_pulse(
                schedule, backend, target, self._amplitude / 2.0, self._duration
            )
            for _ in range(count):
                add_ef_pulse(schedule, backend, target, self._amplitude, self._duration)
            schedule.add(
                backend.Measure(
                    target,
                    acq_index=index,
                    bin_mode=backend.BinMode.AVERAGE,
                    **measure,
                )
            )

        # |1> and |2>, so the fit knows the full contrast. Without them only the
        # product of contrast and rotation error is recoverable, and the error comes
        # out scaled by whatever fraction of the contrast this sweep happened to
        # cover. Note these are the *EF* subspace's two states, not |0> and |1>.
        reference = len(self._counts)
        for offset, prepare_two in enumerate((False, True)):
            schedule.add(backend.Reset(target))
            schedule.add(backend.X(target))
            if prepare_two:
                add_ef_pulse(schedule, backend, target, self._amplitude, self._duration)
            schedule.add(
                backend.Measure(
                    target,
                    acq_index=reference + offset,
                    bin_mode=backend.BinMode.AVERAGE,
                    **measure,
                )
            )
        return schedule

    def analyse(
        self, dataset: xr.Dataset, target: str, device: Any, config: RoutineConfig
    ) -> dict[str, Any]:
        signal = signal_of(dataset)
        expected = len(self._counts) + 2
        if signal.size < expected:
            raise RoutineError(
                f"fine amplitude 12 expected {expected} acquisitions, got {signal.size}"
            )
        swept = signal[: len(self._counts)]
        in_one, in_two = (
            float(signal[len(self._counts)]),
            float(signal[len(self._counts) + 1]),
        )
        fitted = fit_fine_amplitude(
            np.asarray(self._counts, dtype=float),
            swept,
            self._amplitude,
            ground=in_one,
            excited=in_two,
        )
        return {"ef_amp180": fitted["amp180"], **fitted}

    def apply(self, device: Any, target: str, params: dict[str, Any]) -> None:
        element = device.get_element(target)
        path = ef_path(element, "ef_amp180")
        if path:
            write_path(element, path, params["ef_amp180"])


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

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        element = device.get_element(target)
        # Half the pi amplitude is half the rotation, at fixed duration.
        self._half = _required_ef_amplitude(element, target) / 2.0
        self._duration = ef_duration(element, config)
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
        current = float(read_path(device.get_element(target), "clock_freqs.f12"))
        fitted["clock_freq_12"] = current - fitted["detuning"]
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
        fitted = fit_drag(np.asarray(self._drags), paired[:, 0] - paired[:, 1])
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

    #: Prepared states, in the order the confusion matrix indexes them.
    STATES = (0, 1, 2)

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
