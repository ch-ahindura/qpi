"""The calibration routines, in one set shared by every tuner (RFC 0004 §6.2).

There is deliberately no per-backend copy. Both schedulers expose the same gate
vocabulary, so a routine written against a
:class:`~qpi_driver.tuners.base.backend.SchedulerBackend` runs under either; two
copies would be two things to keep in step and one of them would drift.

:data:`ALL_ROUTINES` is the whole graph. Its shape comes only from each
routine's ``depends_on`` — there is no separate ordering to maintain.
"""

from qpi_driver.tuners.base.routines import CalibrationRoutine
from qpi_driver.tuners.routines.benchmarks import (
    AllXYCheck,
    InterleavedRB,
    RandomizedBenchmarking,
)
from qpi_driver.tuners.routines.single_qubit import (
    T1,
    AllXY,
    Drag,
    FineAmplitude,
    Rabi,
    Ramsey,
    T2Echo,
)
from qpi_driver.tuners.routines.spectroscopy import (
    FluxSpectroscopy,
    QubitSpectroscopy,
    ResonatorPunchout,
    ResonatorRelaxation,
    ResonatorSpectroscopy,
    TimeOfFlight,
)
from qpi_driver.tuners.routines.two_qubit import ConditionalPhase, CZChevron

#: Every routine class, in the order RFC 0004 §3 lists the experiments.
ROUTINE_CLASSES: tuple[type[CalibrationRoutine], ...] = (
    TimeOfFlight,
    ResonatorSpectroscopy,
    ResonatorRelaxation,
    ResonatorPunchout,
    QubitSpectroscopy,
    Rabi,
    Ramsey,
    T1,
    T2Echo,
    Drag,
    AllXY,
    FineAmplitude,
    RandomizedBenchmarking,
    FluxSpectroscopy,
    CZChevron,
    ConditionalPhase,
    InterleavedRB,
    AllXYCheck,
)


def all_routines() -> list[CalibrationRoutine]:
    """A fresh instance of every routine.

    Fresh per call because a routine remembers the setpoints of the sweep it
    just built, for its own ``analyse`` to fit against. Within one walk that is
    safe — a routine is analysed immediately after it is built — but two tuners
    sharing instances would not be.
    """
    return [cls() for cls in ROUTINE_CLASSES]


def routine_names() -> set[str]:
    """Every routine name, for validating ``calibration.yml`` against."""
    return {cls.name for cls in ROUTINE_CLASSES}


__all__ = [
    "ROUTINE_CLASSES",
    "all_routines",
    "routine_names",
    "AllXY",
    "AllXYCheck",
    "CZChevron",
    "ConditionalPhase",
    "Drag",
    "FineAmplitude",
    "FluxSpectroscopy",
    "InterleavedRB",
    "QubitSpectroscopy",
    "Rabi",
    "Ramsey",
    "RandomizedBenchmarking",
    "ResonatorPunchout",
    "ResonatorSpectroscopy",
    "T1",
    "T2Echo",
]
