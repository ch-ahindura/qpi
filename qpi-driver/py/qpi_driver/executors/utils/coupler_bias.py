"""Delivering a coupler's DC parking current (RFC 0004 §7).

A tunable coupler needs two different things from the electronics, on two
completely different timescales, and only one of them is a pulse:

- the **CZ**, a microwave tone of a few hundred nanoseconds, compiled into the
  schedule and played by the cluster;
- the **parking bias**, a DC current that holds the coupler at its operating
  point for as long as the fridge is cold.

quantify compiles the first and knows nothing about the second. Its hardware
config has no notion of an SPI rack at all — ``backends.types.qblox`` describes
clusters and nothing else — so the bias cannot be expressed as part of a
schedule even in principle. It is set out of band, over qcodes, before any job
runs.

Two mechanisms can deliver it, and which one a coupler uses is a property of the
rack rather than of the chip:

- :class:`SpiRackBias` drives an S4g current source in an SPI rack. This is what a
  working chip's rack uses.
- :class:`QcmBias` holds the same offset on a baseband module output inside the
  cluster, which removes an instrument from the rack.

Both are addressed the same way from the device config — an edge carries its
``bias.parking_current`` and a ``bias.source`` naming the mechanism — so moving
a chip from one to the other is a config change rather than a code change.
"""

import logging
from typing import Any, Protocol

log = logging.getLogger(__name__)

#: What `bias.source` may say.
SPI = "spi"
QCM = "qcm"


class BiasSource(Protocol):
    """Something that can hold a coupler at a DC current."""

    def apply(self, edge: str, current_a: float, settings: dict[str, Any]) -> None:
        """Park *edge*'s coupler at *current_a*."""

    def close(self) -> None:
        """Release whatever hardware was opened."""


class RecordingBias:
    """Records what would have been applied, and applies nothing.

    Used whenever the cluster itself is replaced — ``is_simulated`` or
    ``is_dummy`` — because there is no rack to talk to, and used by the tests
    because the interesting part is *which current goes where*, which is
    decided long before any instrument is touched.
    """

    def __init__(self) -> None:
        self.applied: dict[str, float] = {}

    def apply(self, edge: str, current_a: float, settings: dict[str, Any]) -> None:
        self.applied[edge] = current_a
        log.info("would park %s at %.6g A (%s)", edge, current_a, settings)

    def close(self) -> None:
        pass


class SpiRackBias:
    """An SPI rack's S4g modules, over qcodes.

    The rack is opened once and shared: an S4g is a physical module with four
    current outputs, and several couplers usually sit on one. Each edge says
    which module and which output it is wired to.
    """

    def __init__(self, address: str, name: str = "spi_rack") -> None:
        from qblox_instruments import SpiRack

        self._rack = SpiRack(name, address)
        self._modules: dict[int, Any] = {}

    def _dac(self, module: int, output: int):
        from qblox_instruments import S4gModule

        if module not in self._modules:
            self._rack.add_spi_module(module, S4gModule, f"module{module}")
            self._modules[module] = getattr(self._rack, f"module{module}")
        return getattr(self._modules[module], f"dac{output}")

    def apply(self, edge: str, current_a: float, settings: dict[str, Any]) -> None:
        module = int(settings.get("spi_module", 0))
        output = int(settings.get("spi_output", 0))
        dac = self._dac(module, output)
        # Ramped rather than stepped: an S4g can slew fast enough to induce a
        # flux transient in the coupler's loop, and the point of a parking
        # current is that nothing is moving when a gate runs.
        dac.ramping_enabled(True)
        dac.current(current_a)
        log.info(
            "parked %s at %.6g A on spi module %d dac %d",
            edge,
            current_a,
            module,
            output,
        )

    def close(self) -> None:
        try:
            self._rack.close()
        except Exception:  # noqa: BLE001 - closing must not mask a real failure
            log.warning("failed to close the SPI rack cleanly", exc_info=True)


class QcmBias:
    """A DC offset held on a baseband cluster module output.

    The same current through the same line, sourced from inside the cluster.
    The offset is a voltage into the flux line's own resistance, so the edge
    carries the conversion rather than this class inventing one.
    """

    def __init__(self, cluster: Any) -> None:
        self._cluster = cluster

    def apply(self, edge: str, current_a: float, settings: dict[str, Any]) -> None:
        module = settings.get("qcm_module")
        output = settings.get("qcm_output")
        if module is None or output is None:
            raise ValueError(
                f"edge {edge!r} is biased by a QCM but does not say which module "
                "and output it is wired to — set bias.qcm_module and "
                "bias.qcm_output"
            )
        ohms = float(settings.get("line_resistance_ohm", 0.0))
        if ohms <= 0:
            raise ValueError(
                f"edge {edge!r} needs bias.line_resistance_ohm to turn its "
                "parking current into the voltage a QCM output can hold"
            )
        target = getattr(self._cluster, f"module{int(module)}")
        getattr(target, f"out{int(output)}_offset")(current_a * ohms)
        log.info(
            "parked %s at %.6g A via qcm module %s out %s",
            edge,
            current_a,
            module,
            output,
        )

    def close(self) -> None:
        pass


def edge_names(device: Any) -> list[str]:
    """The device's edge names, however this scheduler exposes them.

    quantify's ``QuantumDevice.edges`` is a method returning names; qblox's is a
    dict of name to edge. Calling the wrong one raises
    ``'dict' object is not callable``, which is an unhelpful way to find out.
    """
    edges = getattr(device, "edges", None)
    if edges is None:
        return []
    if callable(edges):
        return list(edges())
    return list(edges)


#: The keys a bias submodule may carry, in either scheduler's spelling.
BIAS_KEYS = (
    "parking_current",
    "source",
    "spi_module",
    "spi_output",
    "qcm_module",
    "qcm_output",
    "line_resistance_ohm",
)


def bias_settings(edge: Any) -> dict[str, Any]:
    """Everything an edge's ``bias`` submodule says, as plain values.

    Both schedulers are read here, because they disagree about what a parameter
    *is*: quantify's qcodes submodule exposes callables you invoke to get the
    value, and qblox's pydantic one holds the value on the attribute directly.
    Asking by name and calling only what is callable covers both without either
    caring.
    """
    submodule = getattr(edge, "bias", None)
    if submodule is None:
        return {}

    settings: dict[str, Any] = {}
    names = getattr(submodule, "parameters", None) or BIAS_KEYS
    for name in names:
        value = getattr(submodule, name, None)
        if value is None:
            continue
        if callable(value):
            try:
                value = value()
            except Exception:  # noqa: BLE001 - an unreadable parameter is not a bias
                continue
        settings[name] = value
    return settings


def apply_coupler_bias(device: Any, source: BiasSource) -> dict[str, float]:
    """Park every coupler the device describes, and report what was applied.

    Skips an edge with no bias submodule — a stock `CompositeSquareEdge` has
    none, because a qubit-port CZ has no coupler to park — and skips a current
    of zero, which is the default and means "not biased" rather than "drive
    zero amps at it".
    """
    applied: dict[str, float] = {}
    for name in edge_names(device):
        settings = bias_settings(device.get_edge(name))
        current = settings.get("parking_current")
        if current is None or not current:
            continue
        source.apply(name, float(current), settings)
        applied[name] = float(current)
    return applied


def resolve_bias_source(
    device: Any, *, cluster: Any = None, spi_address: str | None = None
) -> BiasSource:
    """The mechanism this device's couplers are biased through.

    One source for the chip rather than one per edge: an SPI rack is a rack, and
    opening it once per coupler would be four connections to the same serial
    port. A chip whose couplers disagree about the mechanism is a
    misconfiguration rather than a setup to support.
    """
    sources = {
        str(bias_settings(device.get_edge(name)).get("source") or SPI)
        for name in edge_names(device)
        if bias_settings(device.get_edge(name)).get("parking_current")
    }
    if not sources:
        return RecordingBias()
    if len(sources) > 1:
        raise ValueError(
            f"the couplers disagree about how they are biased ({sorted(sources)}); "
            "one rack cannot be two things"
        )

    wanted = sources.pop()
    if wanted == QCM:
        if cluster is None:
            raise ValueError(
                "the couplers are biased by a QCM, but there is no cluster to "
                "hold the offset — this needs real hardware"
            )
        return QcmBias(cluster)
    if wanted != SPI:
        raise ValueError(
            f"unknown coupler bias source {wanted!r}; expected {SPI!r} or {QCM!r}"
        )
    if not spi_address:
        raise ValueError(
            "the couplers are biased by an SPI rack, but no address was given — "
            "pass -o spi_rack_address=<port>"
        )
    return SpiRackBias(spi_address)


def declared_sideband_gaps(device: Any) -> dict[str, float]:
    """Each coupler edge's ``clock_freqs.sideband_gap``, in Hz.

    Only edges that declare a non-zero one: zero means uncharacterised, and the
    simulator should fall back to its own constant rather than be told the gap
    is at DC.
    """
    from qpi_driver.executors.utils.coupler_bias import edge_names

    gaps: dict[str, float] = {}
    for name in edge_names(device):
        clocks = getattr(device.get_edge(name), "clock_freqs", None)
        value = getattr(clocks, "sideband_gap", None) if clocks else None
        if callable(value):
            try:
                value = value()
            except Exception:  # noqa: BLE001 - an unreadable parameter is not a gap
                continue
        if value:
            gaps[name] = float(value)
    return gaps
