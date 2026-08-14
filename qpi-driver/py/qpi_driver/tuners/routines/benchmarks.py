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
from qpi_driver.tuners.base.routines import (
    DEFAULT_ROUTINE_TIMEOUT_S,
    CalibrationRoutine,
    RoutineError,
)
from qpi_driver.tuners.fitting import fit_rb_decay, signal_of
from qpi_driver.tuners.routines.single_qubit import (
    ALLXY_IDEAL,
    ALLXY_PAIRS,
    normalised_allxy,
)
from qpi_driver.tuners.routines.two_qubit import qubits_of
from qpi_driver.tuners.utils.clifford import (
    clifford_to_gates,
    generate_clifford_sequence,
    sequence_with_recovery,
)


#: The most Cliffords escalation will put in one RB schedule, across every depth and
#: circuit.
#:
#: `depths` and `circuits_per_depth` both escalate, so both need the ceiling
#: `MAX_SWEEP_POINTS` is for a scalar sweep — and RB needs its own, because its cost is per
#: *gate* where a frequency sweep's is per acquisition. `MAX_CIRCUITS_PER_DEPTH` bounds how
#: long the node may take; this bounds how large its program may get, which is a different
#: limit and the one that bites.
#:
#: **Measured, at the second attempt.** This was 2500, derived from a Clifford averaging
#: 1.875 pulses at a couple of instructions each — near four — and the docstring said
#: plainly that it had never been checked against a real program and should be measured if
#: it ever bound. It bound, and it was still twice too high. On the August 2026 B chip a
#: sweep of 2413 Cliffords compiled to a program of 1,133,426 bytes, which the driver's own
#: error reported in its SCPI block header (``PROGram  #71133426``). At roughly 45
#: characters a line that is some 25,000 instructions — **10.4 per Clifford**, not four —
#: and the 12288 a sequencer accepts affords about 1180.
#:
#: 1000, for the 15% headroom `MAX_SWEEP_POINTS` leaves for the same reason. The shipped
#: default of 7 depths at 10 circuits is 1270 Cliffords and does assemble, so this sits
#: *below* a working configuration — deliberately. It bounds widening only, never an
#: operator's own sweep, and what it says about that config is true: there is no room to
#: average harder without making the sequences shallower first.
MAX_RB_CLIFFORDS = 1000


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

    def measure(
        self,
        target: str,
        device: Any,
        config: RoutineConfig,
        backend: SchedulerBackend,
        bias: Any = None,
        timeout_s: float = DEFAULT_ROUTINE_TIMEOUT_S,
    ) -> dict[str, Any]:
        """Average harder when the decay cannot be told from the scatter around it.

        "Average more circuits per depth" was the advice this node's refusal already gave,
        and nothing acted on it: the August 2026 B chip refused with a decay spanning
        0.6214 against a scatter of 0.3231, and reported no fidelity at all. Scatter falls
        as ``1/sqrt(N)``, so the axis is the circuit count and the sweep itself is
        untouched — which matters here, because RB's depths are a statement about what the
        operator wants benchmarked.
        """
        return self.escalating(target, device, config, backend, timeout_s)

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        self._depths = [int(d) for d in config.get("depths", [1, 2, 4, 8, 16, 32, 64])]
        # Named `_circuits_per_depth` as well, because escalation reads the setpoints a
        # routine actually used off `_<axis>` — see `_widened`.
        self._circuits = self._circuits_per_depth = int(
            config.get("circuits_per_depth", 10)
        )
        if not self._depths or self._circuits < 1:
            raise RoutineError("RB needs at least one depth and one circuit per depth")

        # How deep escalation may go, given how many circuits each depth already costs —
        # see `MAX_RB_CLIFFORDS`. Widening builds `linear_setpoints(1, top, n)`, whose sum
        # is `n*(1+top)/2`, so the budget inverts to a bound on `top`. Read by `_widened`
        # off `_<axis>_ceiling`, and when it bites the config comes back unchanged and the
        # refusal is re-raised rather than the same sweep re-run.
        self._depths_ceiling = (
            2.0 * MAX_RB_CLIFFORDS / (self._circuits * len(self._depths)) - 1.0
        )

        # And the same budget read the other way: how many circuits these depths afford.
        # Escalation moves this axis when the decay is lost in scatter, and averaging is
        # the right answer — but not past a program the sequencer will not assemble.
        self._circuits_per_depth_ceiling = MAX_RB_CLIFFORDS / max(sum(self._depths), 1)

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
    reads = ("clock_freqs.f01", "rxy.amp180")
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
        # The same normalisation `allxy` uses, and for the reasons recorded there: this
        # used to divide by its own min and max, which inverts on half of all readout
        # chains and — worse on an unresponsive qubit — stretches noise to full scale and
        # calls the result a fidelity. On the August 2026 B chip that reported 0.533 to the
        # drift check.
        normalised = normalised_allxy(measured)
        deviation = float(np.sqrt(np.mean((normalised - np.asarray(ALLXY_IDEAL)) ** 2)))

        # Reported as a fidelity so the drift check compares it the same way it
        # compares RB, rather than needing a second notion of "good".
        #
        # The response goes out alongside it because the rms alone cannot say *which*
        # miscalibration it is measuring, and this is the only AllXY that runs after the
        # single-qubit chain finishes. `allxy` sits before `fine_amplitude` and
        # `fine_amplitude_90` in the graph, so it can never show whether either helped:
        # it is a diagnostic positioned where the thing it would diagnose has not
        # happened yet. Same field name and same normalisation as `allxy`'s, so the two
        # are subtractable.
        return {
            "fidelity": max(0.0, 1.0 - deviation),
            "error_per_gate": deviation,
            "rms_deviation": deviation,
            "normalised_response": [float(value) for value in normalised],
        }
