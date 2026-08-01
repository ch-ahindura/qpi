"""Parking a simulated coupler, so a routine sweeping the bias changes the physics.

`RecordingBias` records a current and applies nothing, which is right for a dummy
cluster: there is no rack, and what the tests wanted was *which current went where*.
It is not enough for `coupler_anticrossing`, which sweeps the bias and reads a qubit
back — against a recorder the qubit never moves and the sweep is flat.

This closes that: the current reaches the coordinator's `parking_currents`, and from
there the coupler's frequency and the push on every qubit it touches. It is the same
out-of-band delivery a real rack does, with the rack replaced rather than removed.
"""

import logging
from typing import Any

log = logging.getLogger(__name__)


class SimulatedBias:
    """Parks a coupler by telling the simulated coordinator about it."""

    #: It really does change the chip — the simulated one. That is the whole point of
    #: it existing beside `RecordingBias`, which does not.
    holds_current = True

    def __init__(self, coordinator: Any) -> None:
        self._coordinator = coordinator
        #: What was applied, in the shape `RecordingBias` reports it, so a test can
        #: assert on the currents without caring which mechanism delivered them.
        self.applied: dict[str, float] = {}

    def apply(self, edge: str, current_a: float, settings: dict[str, Any]) -> None:
        self.applied[edge] = float(current_a)
        self._coordinator.parking_currents[edge] = float(current_a)
        log.debug("parked %s at %.6g A in the simulator", edge, current_a)

    def close(self) -> None:
        pass
