"""A simulated chip to put where the hardware goes (RFC 0004 §7).

Three pieces, and the last is the one that makes the others useful:

- :class:`~qpi_driver.simulation.transmon.TransmonSimulator` is a transmon as
  physics — levels from `scqubits`, dynamics from `qutip`.
- :class:`~qpi_driver.simulation.resonator.ReadoutResonator` is what it is read
  out through, so the readout chain has parameters to calibrate rather than being
  assumed correct.
- :class:`~qpi_driver.simulation.coordinator.SimulatedCoordinator` puts them
  behind quantify's four-call instrument interface, so it drops in wherever a
  cluster goes. It reads the compiled schedule and plays it, which a dummy
  cluster does not: the dummy returns ``nan+nanj`` regardless, so an ``amp180``
  off by a factor of ten looks exactly like a correct one.

That is what closes the loop between the two operations. A tuner calibrates
against this, writes ``quantify.device.yml``, and the `process` driver beside it
compiles jobs against that same file — so a calibration that is wrong shows up
as circuits that give the wrong answer, which is the only way to find out
without a lab.

`scqubits` and `qutip` are needed at run time, from the ``sim`` extra. They are
imported lazily so a base install still imports this package — and
:func:`require_simulation_deps` checks for them when a simulated chip is built, so
a missing extra is a startup error naming what to install rather than a
`ModuleNotFoundError` from inside a fit.
"""

from qpi_driver.simulation.bias import SimulatedBias
from qpi_driver.simulation.coordinator import (
    DEFAULT_DRIVE_STRENGTH,
    SimulatedCoordinator,
    SimulationError,
)
from qpi_driver.simulation.resonator import ReadoutResonator
from qpi_driver.simulation.transmon import (
    GHZ,
    NS,
    TransmonSimulator,
    require_simulation_deps,
)

__all__ = [
    "TransmonSimulator",
    "require_simulation_deps",
    "ReadoutResonator",
    "SimulatedBias",
    "SimulatedCoordinator",
    "SimulationError",
    "DEFAULT_DRIVE_STRENGTH",
    "GHZ",
    "NS",
]
