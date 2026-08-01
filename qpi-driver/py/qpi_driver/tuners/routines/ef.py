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
    linear_setpoints,
    setpoints_of,
)
from qpi_driver.tuners.fitting import (
    fit_rabi,
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
) -> None:
    """One square pulse on the ``.12`` clock, into the port ``rxy`` uses."""
    schedule.add(
        backend.SquarePulse(
            amp=amplitude,
            duration=duration,
            port=f"{target}:mw",
            clock=f"{target}.12",
        )
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

    AMPLITUDE_FACTORS = (1.0, 1.5, 2.0)

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
        span = float(config.get("span", 6e6))
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
        point = three_state_point(element)
        if point:
            schedule.add(
                backend.SetClockFrequency(
                    clock=f"{target}.ro", clock_freq_new=point["frequency"]
                )
            )
        measure_kwargs = {"pulse_amp": point["pulse_amp"]} if point else {}
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
