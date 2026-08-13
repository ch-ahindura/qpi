"""Benchmarks: the leaves of the DAG, which measure without calibrating (RFC 0004 §6.7).

A benchmark writes no device parameter and declares ``benchmark = True``. The
flag is what marks it, not the empty ``updates`` — T1 writes nothing either, and
it has no fidelity for the drift check to read. What a benchmark returns is a
``fidelity``, and that is what the periodic check compares against its threshold.
"""

import random
from typing import Any

import numpy as np
import xarray as xr

from qpi_driver.tuners.base.backend import SchedulerBackend
from qpi_driver.tuners.base.config import RoutineConfig
from qpi_driver.tuners.base.routines import CalibrationRoutine, RoutineError
from qpi_driver.tuners.fitting import fit_rb_decay, signal_of
from qpi_driver.tuners.routines.single_qubit import ALLXY_IDEAL, ALLXY_PAIRS
from qpi_driver.tuners.routines.two_qubit import qubits_of
from qpi_driver.tuners.utils.clifford import (
    clifford_to_gates,
    generate_clifford_sequence,
    sequence_with_recovery,
)


class RandomizedBenchmarking(CalibrationRoutine):
    """Standard Clifford RB (Magesan et al., PRL 106, 180504).

    Random Clifford sequences of increasing depth, each closed by the exact
    recovery gate, fitted to ``A·r^m + B``.
    """

    name = "rb"
    depends_on = ("fine_amplitude",)
    updates = ()
    reads = ("clock_freqs.f01", "rxy.amp180")
    benchmark = True

    #: The gate interleaved between Cliffords. None for standard RB.
    interleaved: str | None = None

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        self._depths = [int(d) for d in config.get("depths", [1, 2, 4, 8, 16, 32, 64])]
        self._circuits = int(config.get("circuits_per_depth", 10))
        if not self._depths or self._circuits < 1:
            raise RoutineError("RB needs at least one depth and one circuit per depth")

        # Seeded so a rerun benchmarks the same circuits: an unseeded RB would
        # move under the drift check it exists to detect.
        rng = random.Random(int(config.get("seed", 20260730)))
        schedule = backend.new_schedule(
            self.name, repetitions=int(config.get("shots", 1024))
        )

        index = 0
        for depth in self._depths:
            for _ in range(self._circuits):
                sequence = sequence_with_recovery(
                    generate_clifford_sequence(depth, rng)
                )
                schedule.add(backend.Reset(target))
                self._add_sequence(schedule, target, sequence, backend)
                schedule.add(
                    backend.Measure(
                        target, acq_index=index, bin_mode=backend.BinMode.AVERAGE
                    )
                )
                index += 1
        return schedule

    def _add_sequence(
        self,
        schedule: Any,
        target: str,
        sequence: list[int],
        backend: SchedulerBackend,
    ) -> None:
        """Play *sequence* as native rotations, interleaving where asked."""
        for position, clifford in enumerate(sequence):
            for theta, phi in clifford_to_gates(clifford):
                schedule.add(backend.Rxy(theta=theta, phi=phi, qubit=target))
            # The recovery Clifford closes the sequence, so nothing follows it.
            if self.interleaved and position < len(sequence) - 1:
                self._add_interleaved(schedule, target, backend)

    def _add_interleaved(
        self, schedule: Any, target: str, backend: SchedulerBackend
    ) -> None:  # pragma: no cover - overridden where it matters
        raise NotImplementedError

    def analyse(
        self, dataset: xr.Dataset, target: str, device: Any, config: RoutineConfig
    ) -> dict[str, Any]:
        signal = signal_of(dataset)
        expected = len(self._depths) * self._circuits
        if signal.size < expected:
            raise RoutineError(
                f"RB expected {expected} acquisitions, got {signal.size}"
            )

        # Average the circuits at each depth; the decay is over depth, and the
        # spread within a depth is what averaging is for.
        survival = signal[:expected].reshape(len(self._depths), self._circuits)
        mean = survival.mean(axis=1)

        # Normalise so the fit sees a survival probability rather than raw
        # demodulated units, which is what the RB model is written in.
        low, high = float(np.min(mean)), float(np.max(mean))
        if high - low < 1e-12:
            raise RoutineError("RB response is flat across depths — nothing to fit")
        normalised = (mean - low) / (high - low)

        fitted = fit_rb_decay(np.asarray(self._depths, dtype=float), normalised)
        fitted["depths"] = list(self._depths)
        fitted["circuits_per_depth"] = self._circuits
        return fitted


class InterleavedRB(RandomizedBenchmarking):
    """Interleaved RB isolating the CZ (Magesan et al., PRL 109, 080505).

    A CZ between every pair of Cliffords; comparing this decay against standard
    RB's gives the CZ's own error rate.
    """

    name = "interleaved_rb"
    depends_on = ("conditional_phase",)
    targets = "edges"
    updates = ()
    benchmark = True
    interleaved = "CZ"

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        self._control, self._spectator = qubits_of(target)
        return super().build_schedule(self._control, device, config, backend)

    def _add_interleaved(
        self, schedule: Any, target: str, backend: SchedulerBackend
    ) -> None:
        schedule.add(backend.CZ(self._control, self._spectator))

    def analyse(
        self, dataset: xr.Dataset, target: str, device: Any, config: RoutineConfig
    ) -> dict[str, Any]:
        fitted = super().analyse(dataset, self._control, device, config)
        fitted["interleaved_gate"] = "CZ"
        return fitted


class AllXYCheck(CalibrationRoutine):
    """A 21-point AllXY used as a fast smoke test rather than a diagnostic.

    Cheaper than RB by two orders of magnitude, which is what makes it usable as
    the first thing a drift check runs: a chip that fails AllXY will fail RB too,
    and failing in twenty milliseconds is better than failing in two minutes.
    """

    name = "allxy_check"
    depends_on = ("fine_amplitude",)
    updates = ()
    reads = ("clock_freqs.f01", "rxy.amp180")
    benchmark = True

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        schedule = backend.new_schedule(
            self.name, repetitions=int(config.get("shots", 512))
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
                f"AllXY check expected {len(ALLXY_PAIRS)} acquisitions, got {signal.size}"
            )
        measured = signal[: len(ALLXY_PAIRS)]
        low, high = float(np.min(measured)), float(np.max(measured))
        if high - low < 1e-12:
            raise RoutineError(
                "AllXY check response is flat — the qubit is not responding"
            )

        normalised = (measured - low) / (high - low)
        deviation = float(np.sqrt(np.mean((normalised - np.asarray(ALLXY_IDEAL)) ** 2)))

        # Reported as a fidelity so the drift check compares it the same way it
        # compares RB, rather than needing a second notion of "good".
        return {
            "fidelity": max(0.0, 1.0 - deviation),
            "error_per_gate": deviation,
            "rms_deviation": deviation,
        }
