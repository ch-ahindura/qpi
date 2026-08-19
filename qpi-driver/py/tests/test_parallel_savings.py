"""What grouping actually saves, measured rather than argued (RFC 0009 §5.5).

Skipped unless ``QPI_BENCH=1``, because it walks the whole graph twice and that is minutes
rather than the seconds the rest of the suite takes. Run it with::

    make bench-parallel

The number that matters is **acquisitions**, not wall clock. Each acquisition is one
arm-and-wait cycle on the cluster, and a fused schedule's pulses are one target's — the
sequencers play concurrently — so on hardware the saving is the ratio of cycles. Wall clock
here is the simulator's, which integrates a Hamiltonian per operation and so scales with
the *schedule* rather than with the instrument; it is reported for information and is not
the claim.
"""

import os
import time

import pytest
from qpi_driver.tuners.base.config import (
    CalibrationConfig,
    ParallelConfig,
    RoutineConfig,
)
from qpi_driver.tuners.base.dag import CalibrationDAG
from qpi_driver.tuners.routines import all_routines

pytestmark = pytest.mark.skipif(
    os.environ.get("QPI_BENCH") != "1",
    reason="a whole-graph benchmark; set QPI_BENCH=1 or run `make bench-parallel`",
)

#: The chip RFC 0009 §5.5's table is about at its smallest interesting size: a linear chain
#: of three qubits and the two couplers between them. Small enough to walk twice in a test,
#: and large enough that a spacing of 2 gives two qubit groups and one edge group.
QUBITS = ["q0", "q1", "q2"]
EDGES = ["q0_q1", "q1_q2"]

#: Shots trimmed to the smallest thing that still walks every routine. The benchmark counts
#: acquisitions, and how long each one takes does not change the ratio — but it does change
#: how long the benchmark takes, and a full-size walk is not something to put in front of a
#: reviewer.
BENCH_SHOTS = 8


def _config(**parallel):
    routines = {
        r.name: RoutineConfig(params={"shots": BENCH_SHOTS}) for r in all_routines()
    }
    return CalibrationConfig(
        target_qubits=list(QUBITS),
        target_edges=list(EDGES),
        routines=routines,
        parallel=ParallelConfig(**parallel) if parallel else ParallelConfig(),
    )


class _CountingBackend:
    """Wraps a backend and counts the acquisitions asked of it."""

    def __init__(self, inner):
        self._inner = inner
        self.acquisitions = 0

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def run(self, schedule, timeout_s=None):
        self.acquisitions += 1
        return self._inner.run(schedule, timeout_s=timeout_s)


def _walk(parallel: dict) -> tuple[int, float, int]:
    """Walk the whole graph once. Returns (acquisitions, seconds, routines that ran)."""
    from tests.utils.simulation import SimulatedTuner

    tuner = SimulatedTuner(qubits=tuple(QUBITS), edges=tuple(EDGES))
    config = _config(**parallel)
    backend = _CountingBackend(tuner.backend)
    started = time.monotonic()
    report = CalibrationDAG(all_routines(), config).run(
        device=tuner.device, backend=backend, config=config
    )
    elapsed = time.monotonic() - started
    return backend.acquisitions, elapsed, len(report.routine_results)


def test_grouping_costs_fewer_acquisitions_than_walking_one_at_a_time():
    """The claim of RFC 0009 §1, on the smallest chip that can show it."""
    serial, serial_s, serial_ran = _walk({})
    grouped, grouped_s, grouped_ran = _walk(
        {"enabled": True, "qubit_spacing": 2, "edge_spacing": 1}
    )

    reachable = len(QUBITS) * sum(
        1 for r in all_routines() if r.targets == "qubits"
    ) + len(EDGES) * sum(1 for r in all_routines() if r.targets == "edges")
    print(
        f"\n  chain of {len(QUBITS)} qubits and {len(EDGES)} couplers\n"
        f"  serial : {serial:5d} acquisitions, {serial_s:7.1f}s\n"
        f"  grouped: {grouped:5d} acquisitions, {grouped_s:7.1f}s\n"
        f"  saving : {serial / max(grouped, 1):.2f}x fewer acquisitions\n"
        f"\n  Measured over the {serial_ran} routine-targets that complete against the\n"
        f"  simulator, of {reachable} the graph defines — most of the rest have no physics\n"
        f"  here and are skipped, so this is a floor rather than the full-graph figure.\n"
        f"  RFC 0009 §5.5 gives that one as arithmetic: routines x groups against\n"
        f"  routines x targets, which on this chain is 2 qubit groups and 1 edge group.\n"
        f"  Wall clock is the simulator's, which integrates per operation and so scales\n"
        f"  with the schedule rather than with an instrument. It is not the claim."
    )

    assert grouped < serial, (
        f"grouping took {grouped} acquisitions against {serial} sequentially, "
        f"which is no saving at all"
    )
    # Both walks have to measure the same chip, or the comparison is between two
    # different runs rather than between two ways of doing one.
    assert grouped_ran == serial_ran, (
        f"grouped produced {grouped_ran} results against {serial_ran} sequentially"
    )
