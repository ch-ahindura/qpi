"""A simulated chip behind qblox-scheduler's ``HardwareAgent`` (RFC 0004 §7).

quantify reaches its hardware through an instrument coordinator, so
:class:`~qpi_driver.simulation.SimulatedCoordinator` drops straight in. qblox
reaches it through a ``HardwareAgent`` that compiles *and* runs in one call, and
whose ``run`` goes down through the schedule's own experiment object rather than
asking a coordinator — so there is nothing to substitute at that seam.

The seam that does exist is the split inside the agent: ``compile`` is offline
and needs no instrument at all. So this keeps a real ``HardwareAgent`` for
everything structural — compilation, the quantum device, the hardware config —
and takes over only the execution step, handing the compiled schedule to the
simulator instead of to a cluster.

Two consequences worth being explicit about:

- **The compilation is the real one.** A schedule that would not assemble for a
  cluster does not assemble here either, so `is_simulated` under qblox still
  catches every compile-time error a lab node would.
- **Dummy connections exist only so compilation can happen.** The agent is
  constructed with them and its ``run`` is never called, so no acquisition ever
  comes from the dummy cluster that returns ``nan``.

quantify-scheduler is being deprecated, which makes this the path that has to
keep working rather than the fallback.
"""

import logging
from typing import Any

import xarray as xr

log = logging.getLogger(__name__)


class SimulatedAgent:
    """``HardwareAgent``'s surface, with the physics in place of the cluster.

    Only the members the driver and the tuners actually use are forwarded, so a
    call this does not model fails loudly on the attribute rather than quietly
    reaching a dummy instrument.
    """

    def __init__(self, agent: Any, coordinator: Any) -> None:
        self._agent = agent
        self._coordinator = coordinator

    # --- the parts that are genuinely the agent's ----------------------------

    def compile(self, schedule: Any) -> Any:
        """The real compiler. An invalid schedule fails here exactly as on a node."""
        return self._agent.compile(schedule)

    @property
    def quantum_device(self) -> Any:
        return self._agent.quantum_device

    @property
    def hardware_configuration(self) -> Any:
        return self._agent.hardware_configuration

    @property
    def latest_compiled_schedule(self) -> Any:
        return self._agent.latest_compiled_schedule

    def set_output_data_dir(self, *args: Any, **kwargs: Any) -> Any:
        return self._agent.set_output_data_dir(*args, **kwargs)

    # --- the part that is the chip's -----------------------------------------

    def run(self, schedule: Any, timeout: int = 10, **kwargs: Any) -> xr.Dataset:
        """Compile, then play through the simulator rather than a cluster.

        ``timeout`` is accepted and ignored: there is nothing to wait for, and a
        caller that passes it is right to.
        """
        compiled = self._agent.compile(schedule)
        self._coordinator.prepare(compiled)
        self._coordinator.start()
        self._coordinator.wait_done(timeout_sec=timeout)
        return self._coordinator.retrieve_acquisition()

    def connect_clusters(self) -> None:
        """Nothing to connect. Named because ``run`` on the real agent calls it."""

    def get_clusters(self) -> list:
        return []

    def close(self) -> None:
        for target in (self._coordinator, self._agent):
            close = getattr(target, "close", None)
            if close is None:
                continue
            try:
                close()
            except Exception:  # noqa: BLE001 - shutdown must not mask a failure
                log.debug("failed to close %r cleanly", target, exc_info=True)


def simulated_agent(
    *,
    hardware_configuration: Any,
    quantum_device_configuration: Any,
    output_dir: Any = None,
    simulator: Any = None,
    sideband_gaps: Any = None,
) -> SimulatedAgent:
    """A :class:`SimulatedAgent` over a real, dummy-connected ``HardwareAgent``.

    The dummy connections are for the compiler's benefit alone — see the module
    docstring. Nothing reads an acquisition from them.
    """
    from qblox_scheduler import HardwareAgent

    from qpi_driver.simulation import SimulatedCoordinator

    agent = HardwareAgent(
        hardware_configuration=hardware_configuration,
        quantum_device_configuration=quantum_device_configuration,
        create_dummy_connections=True,
        output_dir=output_dir,
    )
    return SimulatedAgent(
        agent, SimulatedCoordinator(simulator, sideband_gaps=sideband_gaps)
    )
