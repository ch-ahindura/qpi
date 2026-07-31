"""Module containing compatibility imports for qblox-scheduler-related libraries"""

import logging
from enum import Enum
from typing import Any, TypeAlias

from qpi_driver.compat.shared import BasicCompatClass

try:
    from pydantic import BaseModel, ImportString, field_validator
    from qblox_instruments import Cluster, ClusterType, InstrumentType  # noqa: F401
    from qblox_scheduler import HardwareAgent, Schedule  # noqa: F401
    from qblox_scheduler.device_under_test.composite_square_edge import (
        CompositeSquareEdge,
    )
    from qblox_scheduler.device_under_test.quantum_device import (
        DeviceElement,
        Edge,
        QuantumDevice,  # noqa: F401
    )
    from qblox_scheduler.device_under_test.transmon_element import (
        BasicTransmonElement,  # noqa: F401
    )
    from qblox_scheduler.enums import BinMode
    from qblox_scheduler.operations import (
        CNOT,
        CZ,
        H,
        IdlePulse,
        Measure,
        Operation,
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
    from qblox_scheduler.operations.pulse_library import (
        SetClockFrequency,
        ShiftClockPhase,
        SoftSquarePulse,
        SquarePulse,
    )
    from qblox_scheduler.qblox.hardware_agent import (
        QbloxHardwareCompilationConfig,  # noqa: F401
    )
    from qblox_scheduler.resources import ClockResource
    from qblox_scheduler.schedules import Schedulable
    from qblox_scheduler.schedules.schedule import TimeableSchedule
    from qblox_scheduler.structure.model import Numbers, Parameter, SchedulerSubmodule
    from qcodes.instrument import InstrumentModule  # noqa: F401
    from qcodes.instrument.base import Instrument  # noqa: F401
    from qcodes.parameters import ParameterBase  # noqa: F401

    IS_QBLOX_SCHEDULER_INSTALLED: bool = True

except ImportError as exp:
    logging.debug(f"failed importing from qblox.compat {exp}")
    IS_QBLOX_SCHEDULER_INSTALLED: bool = False

    HardwareAgent: TypeAlias = Any
    QbloxHardwareCompilationConfig: TypeAlias = Any
    QuantumDevice: TypeAlias = Any
    BasicTransmonElement: TypeAlias = Any
    Cluster: TypeAlias = Any
    ImportString: TypeAlias = Any
    Schedulable: TypeAlias = Any

    class BinMode(str, Enum):
        APPEND = "append"
        AVERAGE = "average"

    class DeviceElement(BasicCompatClass): ...

    class ShiftClockPhase(BasicCompatClass): ...

    class SetClockFrequency(BasicCompatClass): ...

    class SchedulerSubmodule(BasicCompatClass): ...

    class Numbers(BasicCompatClass): ...

    class SquarePulse(BasicCompatClass): ...

    class SoftSquarePulse(BasicCompatClass): ...

    class ClockResource(BasicCompatClass): ...

    class TimeableSchedule(BasicCompatClass): ...

    class Edge(BasicCompatClass): ...

    class BaseModel(BasicCompatClass): ...

    class CompositeSquareEdge(BasicCompatClass): ...

    class CNOT(BasicCompatClass): ...

    class CZ(BasicCompatClass): ...

    class H(BasicCompatClass): ...

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

    def field_validator(*args, **kwargs):
        def decor(*args, **kwargs):
            return args

        return decor

    def Parameter(*args, **kwargs):
        pass
