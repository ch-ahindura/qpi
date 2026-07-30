"""What a routine needs from a scheduler, and nothing more (RFC 0004 §6.2).

`quantify-scheduler` and `qblox-scheduler` expose the same gate vocabulary —
``Rxy``, ``X``, ``Reset``, ``Measure``, ``CZ``, ``IdlePulse`` — under two import
paths, and differ only in how a schedule is built and run. A routine written
against that vocabulary is therefore backend-agnostic, which is why there is one
set of routines here rather than one per tuner.

A :class:`SchedulerBackend` is the seam: it supplies the operation classes a
routine composes, and it knows how to turn a finished schedule into a dataset.
Everything backend-specific lives behind it.
"""

from abc import ABC, abstractmethod
from typing import Any

import xarray as xr


class SchedulerBackend(ABC):
    """The scheduler operations a routine composes, plus a way to run the result.

    Subclasses bind the concrete classes from one scheduler package. The
    attribute names are deliberately the scheduler's own, so a routine reads
    like the scheduler's documentation.
    """

    #: Scheduler operation classes, bound by the subclass. Named as the
    #: schedulers name them so routines stay readable.
    Schedule: Any
    Reset: Any
    Measure: Any
    Rxy: Any
    X: Any
    Y: Any
    Rz: Any
    CZ: Any
    IdlePulse: Any
    SquarePulse: Any
    BinMode: Any

    @abstractmethod
    def new_schedule(self, name: str, repetitions: int = 1) -> Any:
        """An empty schedule this backend's compiler will accept."""

    @abstractmethod
    def run(self, schedule: Any) -> xr.Dataset:
        """Compile, execute and retrieve *schedule* as an acquisition dataset."""

    def idle(self, schedule: Any, duration: float) -> None:
        """Append an idle of *duration* seconds — the delay every T1/T2 sweep needs.

        Both schedulers spell this ``IdlePulse``; it is a method rather than a
        raw class so a backend whose idle is expressed differently has one place
        to say so.
        """
        schedule.add(self.IdlePulse(duration=duration))
