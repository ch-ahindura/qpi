"""A simulated chip to put where the hardware goes (RFC 0004 §7).

Two pieces, and the second is the one that makes the first useful:

- :class:`~qpi_driver.simulation.transmon.TransmonSimulator` is a transmon as
  physics — levels from `scqubits`, dynamics from `qutip`.
- :class:`~qpi_driver.simulation.coordinator.SimulatedCoordinator` puts it
  behind quantify's four-call instrument interface, so it drops in wherever a
  cluster goes. It reads the compiled schedule and plays it, which a dummy
  cluster does not: the dummy returns ``nan+nanj`` regardless, so an ``amp180``
  off by a factor of ten looks exactly like a correct one.

That is what closes the loop between the two operations. A tuner calibrates
against this, writes ``quantify.device.yml``, and the `process` driver beside it
compiles jobs against that same file — so a calibration that is wrong shows up
as circuits that give the wrong answer, which is the only way to find out
without a lab.

`scqubits` and `qutip` are needed at run time, from the ``sim`` dependency
group. They are imported lazily so a base install still imports this package.
"""

from qpi_driver.simulation.coordinator import (
    DEFAULT_DRIVE_STRENGTH,
    SimulatedCoordinator,
    SimulationError,
)
from qpi_driver.simulation.transmon import GHZ, NS, TransmonSimulator

__all__ = [
    "TransmonSimulator",
    "SimulatedCoordinator",
    "SimulationError",
    "DEFAULT_DRIVE_STRENGTH",
    "GHZ",
    "NS",
]
