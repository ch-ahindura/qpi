"""Module containing compatibility imports for quantify-related libraries"""

import logging
from enum import Enum
from typing import Any, TypeAlias

from qpi_driver.compat.shared import BasicCompatClass

try:
    from pydantic import BaseModel, ImportString, field_validator
    from qblox_instruments import Cluster, ClusterType, InstrumentType
    from qcodes.instrument import InstrumentModule
    from qcodes.instrument.base import Instrument
    from qcodes.instrument.channel import InstrumentChannel
    from qcodes.instrument.parameter import ManualParameter
    from qcodes.parameters import ParameterBase
    from quantify_core.data.handling import set_datadir
    from quantify_scheduler import Operation, Schedule
    from quantify_scheduler.backends.graph_compilation import SerialCompiler
    from quantify_scheduler.backends.qblox_backend import (
        QbloxHardwareCompilationConfig,
    )
    from quantify_scheduler.backends.types.qblox import ClusterDescription
    from quantify_scheduler.device_under_test.composite_square_edge import (
        CompositeSquareEdge,
    )
    from quantify_scheduler.device_under_test.quantum_device import (
        DeviceElement,
        Edge,
        QuantumDevice,
    )
    from quantify_scheduler.device_under_test.transmon_element import (
        BasicTransmonElement,
    )
    from quantify_scheduler.enums import BinMode
    from quantify_scheduler.helpers.validators import Numbers
    from quantify_scheduler.instrument_coordinator import InstrumentCoordinator
    from quantify_scheduler.operations.gate_library import (
        CNOT,
        CZ,
        H,
        Measure,
        Reset,
        Rxy,
        Rz,
        S,
        SDagger,
        T,
        TDagger,
        X,
        Y,
        Z,
    )
    from quantify_scheduler.operations.pulse_library import (
        DRAGPulse,
        IdlePulse,
        SetClockFrequency,
        ShiftClockPhase,
        SoftSquarePulse,
        SquarePulse,
    )
    from quantify_scheduler.qblox import ClusterComponent
    from quantify_scheduler.resources import ClockResource
    from quantify_scheduler.schedules import Schedulable

    IS_QUANTIFY_INSTALLED: bool = True

except ImportError as exp:
    logging.debug(f"failed importing from quantify.compat {exp}")
    IS_QUANTIFY_INSTALLED: bool = False

    def set_datadir(*args, **kwargs):
        pass

    def field_validator(*args, **kwargs):
        def decor(*args, **kwargs):
            return args

        return decor

    QbloxHardwareCompilationConfig: TypeAlias = Any
    ClusterComponent: TypeAlias = Any
    InstrumentCoordinator: TypeAlias = Any
    BasicTransmonElement: TypeAlias = Any
    QuantumDevice: TypeAlias = Any
    SerialCompiler: TypeAlias = Any
    Cluster: TypeAlias = Any
    ImportString: TypeAlias = Any
    Schedulable: TypeAlias = Any

    class DeviceElement(BasicCompatClass): ...

    class ShiftClockPhase(BasicCompatClass): ...

    class DRAGPulse(BasicCompatClass): ...

    class SetClockFrequency(BasicCompatClass): ...

    class SquarePulse(BasicCompatClass): ...

    class SoftSquarePulse(BasicCompatClass): ...

    class ClockResource(BasicCompatClass): ...

    class ManualParameter(BasicCompatClass): ...

    class Numbers(BasicCompatClass): ...

    class InstrumentChannel(BasicCompatClass): ...

    class Edge(BasicCompatClass): ...

    class BaseModel(BasicCompatClass): ...

    class CNOT(BasicCompatClass): ...

    class CZ(BasicCompatClass): ...

    class H(BasicCompatClass): ...

    class BinMode(str, Enum):
        APPEND = "append"
        AVERAGE = "average"

    class Measure(BasicCompatClass): ...

    class Reset(BasicCompatClass): ...

    class Rxy(BasicCompatClass): ...

    class Rz(BasicCompatClass): ...

    class X(BasicCompatClass): ...

    class Y(BasicCompatClass): ...

    class Z(BasicCompatClass): ...

    class S(BasicCompatClass): ...

    class T(BasicCompatClass): ...

    class SDagger(BasicCompatClass): ...

    class TDagger(BasicCompatClass): ...

    class IdlePulse(BasicCompatClass): ...

    class InstrumentModule(BasicCompatClass): ...

    class ParameterBase(BasicCompatClass): ...

    class ClusterDescription(BasicCompatClass): ...

    class CompositeSquareEdge(BasicCompatClass): ...

    class InstrumentType(Enum): ...

    class ClusterType(Enum): ...

    class Operation(BasicCompatClass): ...

    class Schedule(BasicCompatClass):
        def add(self, *args, **kwargs):
            pass

    class Instrument(BasicCompatClass):
        @classmethod
        def close_all(cls):
            pass
