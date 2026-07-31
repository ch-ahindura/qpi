"""Two-qubit gate calibration: the CZ chevron and its conditional phase (RFC 0004 §3).

These target edges rather than qubits. An edge is named ``<parent>_<child>``,
which is how the two qubits it acts on are recovered.
"""

from typing import Any

import numpy as np
import xarray as xr

from qpi_driver.tuners.base.backend import SchedulerBackend
from qpi_driver.tuners.base.config import RoutineConfig
from qpi_driver.tuners.base.device import phase_correction_names, write_path
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
    # Named by role; `apply` resolves them to whatever this edge actually calls
    # them, which differs between the two schedulers.
    updates = ("cz.parent_phase_correction", "cz.child_phase_correction")

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        parent, child = qubits_of(target)
        self._phases = setpoints_of(config, "phases", linear_setpoints(0.0, 360.0, 25))
        schedule = backend.new_schedule(
            self.name, repetitions=int(config.get("shots", 512))
        )

        # Four fringes, not two. A Ramsey on one qubit with the other down and
        # then up gives the conditional phase from the offset between them, and
        # the *ground* fringe's own phase gives that qubit's single-qubit phase
        # over the CZ. Both qubits are needed because each carries its own, and
        # the edge has a separate correction for each — measuring one and
        # assuming the other is how a CZ ends up building the wrong Bell state
        # while every reported number looks right.
        index = 0
        for measured, spectator in ((parent, child), (child, parent)):
            for spectator_excited in (False, True):
                for phase in self._phases:
                    schedule.add(backend.Reset(measured))
                    schedule.add(backend.Reset(spectator))
                    if spectator_excited:
                        schedule.add(backend.X(spectator))
                    schedule.add(backend.Rxy(theta=90, phi=0, qubit=measured))
                    schedule.add(backend.CZ(parent, child))
                    schedule.add(backend.Rxy(theta=90, phi=phase, qubit=measured))
                    schedule.add(
                        backend.Measure(
                            measured,
                            acq_index=index,
                            bin_mode=backend.BinMode.AVERAGE,
                        )
                    )
                    index += 1
        return schedule

    def analyse(
        self, dataset: xr.Dataset, target: str, device: Any, config: RoutineConfig
    ) -> dict[str, Any]:
        signal = signal_of(dataset)
        count = len(self._phases)
        if signal.size < 4 * count:
            raise RoutineError(
                f"conditional phase expected {4 * count} acquisitions, got {signal.size}"
            )

        phases = np.asarray(self._phases)
        # Both fringes of a pair, not their difference: the measured qubit's
        # dynamical phase over the flux pulse cancels between them and does not
        # cancel within either.
        parent_fit = fit_conditional_phase(
            phases, signal[:count], signal[count : 2 * count]
        )
        child_fit = fit_conditional_phase(
            phases, signal[2 * count : 3 * count], signal[3 * count : 4 * count]
        )

        return {
            **parent_fit,
            "parent_phase_correction": parent_fit["reference_phase"],
            "child_phase_correction": child_fit["reference_phase"],
            # The same gate seen from either qubit, so the two conditional
            # phases are a consistency check rather than two measurements.
            "conditional_phase_from_child": child_fit["conditional_phase"],
        }

    def apply(self, device: Any, target: str, params: dict[str, Any]) -> None:
        """Write the two single-qubit corrections the CZ left behind.

        Not the conditional phase: that is fixed by the pulse's amplitude and
        duration, and no virtual Z can change it. What these two parameters
        cancel is the single-qubit phase each qubit accumulated during the flux
        pulse, which is what stops a conditional-phase-correct CZ from building
        the Bell state it should.

        The names are resolved from the edge rather than assumed, because the
        two schedulers spell them differently and the name this used to write —
        ``cz.phase_correction`` — is one neither has, so it silently applied
        nothing at all.
        """
        edge = device.get_edge(target)
        names = phase_correction_names(edge)
        if names is None:
            return  # an edge with no virtual-Z corrections to set

        parent_name, child_name = names
        write_path(edge, f"cz.{parent_name}", params["parent_phase_correction"])
        write_path(edge, f"cz.{child_name}", params["child_phase_correction"])
