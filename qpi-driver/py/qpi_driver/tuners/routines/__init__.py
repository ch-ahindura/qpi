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
from qpi_driver.tuners.routines.ef import (
    Drag12,
    FineAmplitude12,
    Rabi12,
    Ramsey12,
    ResonatorSpectroscopySecondExcited,
    ThreeStateDiscrimination,
    ThreeStateOperatingPoint,
)
from qpi_driver.tuners.routines.readout import (
    ReadoutDiscrimination,
    ReadoutFidelity,
    ReadoutOperatingPoint,
)
from qpi_driver.tuners.routines.single_qubit import (
    T1,
    AllXY,
    Drag,
    FineAmplitude,
    FineAmplitude90,
    Rabi,
    Ramsey,
    T2Echo,
)
from qpi_driver.tuners.routines.spectroscopy import (
    F12Spectroscopy,
    FluxSpectroscopy,
    QubitSpectroscopy,
    ResonatorPunchout,
    ResonatorRelaxation,
    ResonatorSpectroscopy,
    ResonatorSpectroscopyExcited,
    TimeOfFlight,
)
from qpi_driver.tuners.routines.two_qubit import (
    ConditionalPhase,
    CouplerAnticrossing,
    CZChevron,
    CZParametrization,
    CZSpectroscopy,
)

#: Every routine class, in the order RFC 0004 §3 lists the experiments.
ROUTINE_CLASSES: tuple[type[CalibrationRoutine], ...] = (
    TimeOfFlight,
    ResonatorSpectroscopy,
    ResonatorRelaxation,
    ResonatorPunchout,
    QubitSpectroscopy,
    Rabi,
    ResonatorSpectroscopyExcited,
    ReadoutOperatingPoint,
    ReadoutDiscrimination,
    ReadoutFidelity,
    F12Spectroscopy,
    Rabi12,
    ResonatorSpectroscopySecondExcited,
    ThreeStateOperatingPoint,
    Ramsey12,
    Drag12,
    FineAmplitude12,
    ThreeStateDiscrimination,
    Ramsey,
    T1,
    T2Echo,
    Drag,
    AllXY,
    FineAmplitude,
    FineAmplitude90,
    RandomizedBenchmarking,
    FluxSpectroscopy,
    CouplerAnticrossing,
    CZSpectroscopy,
    CZParametrization,
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
    "CZParametrization",
    "CZSpectroscopy",
    "ConditionalPhase",
    "CouplerAnticrossing",
    "Drag",
    "Drag12",
    "F12Spectroscopy",
    "FineAmplitude",
    "FineAmplitude12",
    "FineAmplitude90",
    "FluxSpectroscopy",
    "InterleavedRB",
    "QubitSpectroscopy",
    "Rabi",
    "Rabi12",
    "Ramsey",
    "Ramsey12",
    "RandomizedBenchmarking",
    "ReadoutDiscrimination",
    "ReadoutFidelity",
    "ReadoutOperatingPoint",
    "ResonatorPunchout",
    "ResonatorRelaxation",
    "ResonatorSpectroscopy",
    "ResonatorSpectroscopyExcited",
    "ResonatorSpectroscopySecondExcited",
    "T1",
    "T2Echo",
    "ThreeStateDiscrimination",
    "ThreeStateOperatingPoint",
    "TimeOfFlight",
]
