"""Routines for Quantify Tuner."""

from .allxy import AllXY
from .benchmarks.allxy_check import AllXYCheck
from .benchmarks.interleaved_rb import InterleavedRB
from .benchmarks.rb import RB
from .conditional_phase import ConditionalPhase
from .cz_chevron import CZChevron
from .drag import DRAG
from .fine_amplitude import FineAmplitude
from .flux_spectroscopy import FluxSpectroscopy
from .qubit_spectroscopy import QubitSpectroscopy
from .rabi import Rabi
from .ramsey import Ramsey
from .resonator_punchout import ResonatorPunchout
from .resonator_spectroscopy import ResonatorSpectroscopy
from .t1 import T1
from .t2_echo import T2Echo

ALL_ROUTINES = [
    ResonatorSpectroscopy(),
    ResonatorPunchout(),
    QubitSpectroscopy(),
    Rabi(),
    Ramsey(),
    T1(),
    T2Echo(),
    DRAG(),
    AllXY(),
    FineAmplitude(),
    FluxSpectroscopy(),
    CZChevron(),
    ConditionalPhase(),
    RB(),
    InterleavedRB(),
    AllXYCheck(),
]


def routine_registry() -> dict:
    return {r.name: r for r in ALL_ROUTINES}
