"""Benchmarks: the leaves of the DAG, which measure without calibrating (RFC 0004 §6.7).

A benchmark writes no device parameter and declares ``benchmark = True``. The
flag is what marks it, not the empty ``updates`` — T1 writes nothing either, and
it has no fidelity for the drift check to read. What a benchmark returns is a
``fidelity``, and that is what the periodic check compares against its threshold.
"""

import logging
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


log = logging.getLogger(__name__)

#: The most Cliffords one RB *schedule* may hold, across every depth and circuit in it.
#:
#: A chunk size, not a ceiling. RB's cost is per gate, so averaging harder eventually
#: exceeds any program a sequencer will take — and the answer RFC 0007 §5 gives for a sweep
#: too large for one schedule is to chunk it across acquisitions rather than refuse it.
#: `RandomizedBenchmarking.acquire` splits on this number and combines the results, so the
#: circuit count an operator asks for is honoured however large it is.
#:
#: **Measured, at the second attempt.** It was 2500, derived from a Clifford averaging 1.875
#: pulses at a couple of instructions each — near four — and its docstring said plainly that
#: the figure had never been checked against a real program and should be measured if it ever
#: bound. It bound, and was still twice too high: on the August 2026 B chip a sweep of 2413
#: Cliffords compiled to 1,133,426 bytes, which the driver's own error reported in its SCPI
#: block header (``PROGram  #71133426``). At roughly 45 characters a line that is some 25,000
#: instructions — **10.4 per Clifford**, not four — and the 12288 a sequencer accepts affords
#: about 1180. 1000 leaves the 15% headroom `MAX_SWEEP_POINTS` leaves for the same reason.
#:
#: Circuits can be split across schedules; a single *sequence* cannot. So this also bounds
#: the deepest sequence escalation will reach, and that one is a real ceiling — see
#: `RandomizedBenchmarking.build_schedule`.
MAX_RB_CLIFFORDS = 1000

#: What an RB sweep is when the operator names nothing. Shared with `acquire`, which has to
#: know the sweep before `build_schedule` has run.
DEFAULT_RB_DEPTHS = (1, 2, 4, 8, 16, 32, 64)
DEFAULT_RB_CIRCUITS = 10

#: Seeded so a rerun benchmarks the same circuits: an unseeded RB would move under the
#: drift check it exists to detect.
DEFAULT_RB_SEED = 20260730

#: ``|0>`` and ``X|0>``, played before the sequences and read on the same axis.
#:
#: Two acquisitions against several hundred, and they are what make the rest a *survival*
#: rather than a shape. The same references `fine_amplitude` and `rabi`'s check already
#: measure, for the same reason: without them the only scale available is the sweep's own
#: range, which forces its extremes to 0 and 1 and cannot see which way the decay runs.
REFERENCE_ACQUISITIONS = 2


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

    def acquire(
        self,
        target: str,
        device: Any,
        config: RoutineConfig,
        backend: SchedulerBackend,
        timeout_s: float,
    ) -> Any:
        """Run this sweep as however many schedules it takes, and combine them.

        RB's cost is per gate, so a circuit count large enough to resolve a shallow decay
        eventually exceeds the 12288 Q1ASM instructions a sequencer takes. Capping it was
        the wrong answer twice over: the operator asked for that much averaging because
        less of it did not resolve, and a program that will not assemble comes back as a
        2.4 MB exception rather than a small one.

        Splitting is exact here, which is what makes it the right answer rather than a
        compromise. `analyse` reduces each depth by the mean over its circuits, and the
        mean of a partition equals the mean of the whole — so N circuits in one schedule
        and N circuits across four schedules give the same number. Each chunk is seeded
        apart, or they would be four copies of the same circuits and average to nothing.
        """
        depths = [int(d) for d in config.get("depths", DEFAULT_RB_DEPTHS)]
        wanted = int(config.get("circuits_per_depth", DEFAULT_RB_CIRCUITS))
        per_schedule = max(1, MAX_RB_CLIFFORDS // max(sum(depths), 1))
        if wanted <= per_schedule or not depths:
            return super().acquire(target, device, config, backend, timeout_s)

        seed = int(config.get("seed", DEFAULT_RB_SEED))
        sizes = [per_schedule] * (wanted // per_schedule)
        if wanted % per_schedule:
            sizes.append(wanted % per_schedule)
        log.info(
            "%s on %s: %d circuits over depths summing %d is %d Cliffords, past the %d one "
            "schedule holds — running %d schedules of %s",
            self.name,
            target,
            wanted,
            sum(depths),
            wanted * sum(depths),
            MAX_RB_CLIFFORDS,
            len(sizes),
            sizes,
        )

        rows = []
        references = []
        for index, size in enumerate(sizes):
            chunk = RoutineConfig(
                enabled=config.enabled,
                params={
                    **config.params,
                    "depths": depths,
                    "circuits_per_depth": size,
                    # Apart, or every chunk benchmarks the same circuits.
                    "seed": seed + index,
                },
            )
            dataset = super().acquire(target, device, chunk, backend, timeout_s)
            signal = np.asarray(signal_of(dataset), dtype=float)
            taken = len(depths) * size
            expected = taken + REFERENCE_ACQUISITIONS
            if signal.size < expected:
                raise RoutineError(
                    f"RB chunk {index + 1} of {len(sizes)} expected {expected} "
                    f"acquisitions, got {signal.size}"
                )
            # Every chunk carries its own pair, so averaging them is free shots on the
            # scale the whole fit divides by — and drift between chunks shows up in it.
            references.append(signal[:REFERENCE_ACQUISITIONS])
            rows.append(
                signal[REFERENCE_ACQUISITIONS:expected].reshape(len(depths), size)
            )

        # Each depth's circuits from every chunk, side by side, so `analyse` reshapes it
        # exactly as it would one schedule's worth, references included.
        combined = np.hstack(rows)
        self._depths = depths
        self._circuits = self._circuits_per_depth = int(combined.shape[1])
        return xr.Dataset(
            {
                "y0": (
                    "acq_index",
                    np.concatenate([np.mean(references, axis=0), combined.reshape(-1)]),
                )
            }
        )

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        self._depths = [int(d) for d in config.get("depths", DEFAULT_RB_DEPTHS)]
        # Named `_circuits_per_depth` as well, because escalation reads the setpoints a
        # routine actually used off `_<axis>` — see `_widened`.
        self._circuits = self._circuits_per_depth = int(
            config.get("circuits_per_depth", DEFAULT_RB_CIRCUITS)
        )
        if not self._depths or self._circuits < 1:
            raise RoutineError("RB needs at least one depth and one circuit per depth")

        # How deep escalation may go — see `MAX_RB_CLIFFORDS`. Independent of the circuit
        # count, which is the whole point of chunking: circuits are split across schedules
        # by `acquire`, so only *one circuit's worth of every depth* has to fit in a
        # program. Widening builds `linear_setpoints(1, top, n)`, whose sum is
        # `n*(1+top)/2`, so the budget inverts to a bound on `top`. Read by `_widened` off
        # `_<axis>_ceiling`; when it bites the config comes back unchanged and the refusal
        # is re-raised rather than the same sweep re-run.
        #
        # This one is a real ceiling and cannot become a chunk size. A single sequence of
        # depth m is m Cliffords in one program and there is nowhere to cut it: a Clifford
        # sequence is only an RB sequence closed by its own recovery gate.
        self._depths_ceiling = 2.0 * MAX_RB_CLIFFORDS / len(self._depths) - 1.0

        # Seeded so a rerun benchmarks the same circuits: an unseeded RB would
        # move under the drift check it exists to detect.
        rng = random.Random(int(config.get("seed", DEFAULT_RB_SEED)))
        schedule = backend.new_schedule(
            self.name, repetitions=int(config.get("shots", 1024))
        )

        # |0> and X|0> first, so the decay is read as a survival probability rather than
        # scaled against its own extremes — see :meth:`analyse`.
        for index, prepare in enumerate((0, 1)):
            schedule.add(backend.Reset(target))
            if prepare:
                schedule.add(backend.X(target))
            schedule.add(
                backend.Measure(
                    target, acq_index=index, bin_mode=backend.BinMode.AVERAGE
                )
            )

        index = REFERENCE_ACQUISITIONS
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
        circuits = len(self._depths) * self._circuits
        expected = circuits + REFERENCE_ACQUISITIONS
        if signal.size < expected:
            raise RoutineError(
                f"RB expected {expected} acquisitions, got {signal.size}"
            )

        ground, excited = float(signal[0]), float(signal[1])
        contrast = ground - excited
        if abs(contrast) < 1e-12:
            raise RoutineError(
                "RB's |0> and X|0> references read the same, so no sequence can be scored "
                "against them — the qubit is not responding, or the readout cannot tell "
                "the two states apart"
            )

        # Average the circuits at each depth; the decay is over depth, and the
        # spread within a depth is what averaging is for.
        survival = signal[REFERENCE_ACQUISITIONS:expected].reshape(
            len(self._depths), self._circuits
        )
        mean = survival.mean(axis=1)

        # Against the references, not against the sweep's own extremes. Scaling to the
        # extremes pins the shallowest and deepest points to exactly 1 and 0 whatever they
        # measured, which is a straight line through two invented values: it cannot see
        # that a survival is *rising*, and it destroys the amplitude the fit reports its
        # confidence through. The 2026-08-15 B chip returned 0 at depth 2 and 1 at depth
        # 64 with the decay running the wrong way, and no number of circuits per depth
        # could have changed either — the endpoints were arithmetic, not measurement.
        normalised = (mean - excited) / contrast

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
