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
from qpi_driver.tuners.fitting import fit_rabi, signal_of

#: Where a `CalibratedTransmon` keeps its EF pulse.
EF = "r12"

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
