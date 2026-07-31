"""Two-qubit gate calibration: the CZ chevron and its conditional phase (RFC 0004 §3).

These target edges rather than qubits. An edge is named ``<parent>_<child>``,
which is how the two qubits it acts on are recovered.
"""

from typing import Any

import numpy as np
import xarray as xr

from qpi_driver.tuners.base.backend import SchedulerBackend
from qpi_driver.tuners.base.config import RoutineConfig
from qpi_driver.tuners.base.device import write_path
from qpi_driver.tuners.base.routines import (
    CalibrationRoutine,
    RoutineError,
    linear_setpoints,
    setpoints_of,
)
from qpi_driver.tuners.fitting import fit_chevron, fit_conditional_phase, signal_of


def qubits_of(edge: str) -> tuple[str, str]:
    """The two element names an edge joins.

    Raises:
        RoutineError: if *edge* is not a ``<parent>_<child>`` pair, which is the
            only form the device config and ``CompositeSquareEdge`` accept.
    """
    parts = edge.split("_")
    if len(parts) != 2 or not all(parts):
        raise RoutineError(
            f"edge {edge!r} is not a '<parent>_<child>' pair, so its qubits "
            "cannot be determined"
        )
    return parts[0], parts[1]


class CZChevron(CalibrationRoutine):
    """Sweep flux amplitude and duration to find the CZ (DiCarlo et al., Nature 460, 240)."""

    name = "cz_chevron"
    depends_on = ("rb", "flux_spectroscopy")
    targets = "edges"
    updates = ("cz.amp", "cz.duration")

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        control, _child = qubits_of(target)
        self._amplitudes = setpoints_of(
            config, "amplitudes", linear_setpoints(0.1, 0.6, 11)
        )
        self._durations = setpoints_of(
            config, "durations", linear_setpoints(20e-9, 200e-9, 11)
        )
        port = f"{control}:fl"
        schedule = backend.new_schedule(
            self.name, repetitions=int(config.get("shots", 512))
        )

        index = 0
        for amplitude in self._amplitudes:
            for duration in self._durations:
                parent, child = qubits_of(target)
                schedule.add(backend.Reset(parent))
                schedule.add(backend.Reset(child))
                # |11> is the state that exchanges with |02>, so both qubits are
                # excited before the flux pulse brings them into resonance.
                schedule.add(backend.X(parent))
                schedule.add(backend.X(child))
                schedule.add(
                    backend.SquarePulse(
                        amp=amplitude,
                        duration=duration,
                        port=port,
                        clock="cl0.baseband",
                    )
                )
                schedule.add(
                    backend.Measure(
                        parent, acq_index=index, bin_mode=backend.BinMode.AVERAGE
                    )
                )
                index += 1
        return schedule

    def analyse(
        self, dataset: xr.Dataset, target: str, device: Any, config: RoutineConfig
    ) -> dict[str, Any]:
        return fit_chevron(
            np.asarray(self._amplitudes),
            np.asarray(self._durations),
            signal_of(dataset),
        )

    def apply(self, device: Any, target: str, params: dict[str, Any]) -> None:
        edge = device.get_edge(target)
        write_path(edge, "cz.amp", params["cz_amplitude"])
        write_path(edge, "cz.duration", params["cz_duration"])


class ConditionalPhase(CalibrationRoutine):
    """Tune the CZ's conditional phase to π (Sung et al., PRX 11, 021058)."""

    name = "conditional_phase"
    depends_on = ("cz_chevron",)
    targets = "edges"
    updates = ("cz.phase_correction",)

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        control, spectator = qubits_of(target)
        self._phases = setpoints_of(config, "phases", linear_setpoints(0.0, 360.0, 25))
        schedule = backend.new_schedule(
            self.name, repetitions=int(config.get("shots", 512))
        )

        # A Ramsey on the control, run twice: with the spectator in |0> and in
        # |1>. The offset between the two fringes is the conditional phase.
        index = 0
        for spectator_excited in (False, True):
            for phase in self._phases:
                schedule.add(backend.Reset(control))
                schedule.add(backend.Reset(spectator))
                if spectator_excited:
                    schedule.add(backend.X(spectator))
                schedule.add(backend.Rxy(theta=90, phi=0, qubit=control))
                schedule.add(backend.CZ(control, spectator))
                schedule.add(backend.Rxy(theta=90, phi=phase, qubit=control))
                schedule.add(
                    backend.Measure(
                        control, acq_index=index, bin_mode=backend.BinMode.AVERAGE
                    )
                )
                index += 1
        return schedule

    def analyse(
        self, dataset: xr.Dataset, target: str, device: Any, config: RoutineConfig
    ) -> dict[str, Any]:
        signal = signal_of(dataset)
        count = len(self._phases)
        if signal.size < 2 * count:
            raise RoutineError(
                f"conditional phase expected {2 * count} acquisitions, got {signal.size}"
            )
        # Both fringes, not their difference: the control's dynamical phase over
        # the flux pulse cancels between them and does not cancel within either.
        ground = signal[:count]
        excited = signal[count : 2 * count]
        return fit_conditional_phase(np.asarray(self._phases), ground, excited)

    def apply(self, device: Any, target: str, params: dict[str, Any]) -> None:
        edge = device.get_edge(target)
        # Not every edge model carries a phase correction; where it does not,
        # the conditional phase is reported and applied by the CZ amplitude.
        if hasattr(edge.cz, "phase_correction"):
            write_path(edge, "cz.phase_correction", params["phase_correction"])
