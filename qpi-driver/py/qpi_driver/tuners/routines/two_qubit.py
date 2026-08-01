"""Two-qubit gate calibration: the CZ chevron and its conditional phase (RFC 0004 §3).

These target edges rather than qubits. An edge is named ``<parent>_<child>``,
which is how the two qubits it acts on are recovered.
"""

from typing import Any

import numpy as np
import xarray as xr

from qpi_driver.tuners.base.backend import SchedulerBackend
from qpi_driver.tuners.base.config import RoutineConfig
from qpi_driver.tuners.base.device import (
    phase_correction_names,
    read_path,
    write_path,
)
from qpi_driver.tuners.base.routines import (
    CalibrationRoutine,
    RoutineError,
    grid_duration,
    linear_setpoints,
    setpoints_of,
)
from qpi_driver.tuners.fitting import (
    FitError,
    fit_chevron,
    fit_conditional_phase,
    fit_resonator_spectroscopy,
    signal_of,
)


def qubits_of(edge: str) -> tuple[str, str]:
    """The two element names an edge joins.

    Raises:
        RoutineError: if *edge* is not a ``<parent>_<child>`` pair, which is the
            only form the device config and ``CompositeSquareEdge`` accept.
    """
    parts = edge.split("_")
    if len(parts) != 2 or not all(parts):
        raise RoutineError(
            f"edge {edge!r} is not a '<parent>_<child>' pair, so its qubits "
            "cannot be determined"
        )
    return parts[0], parts[1]


def parametric_edge(device: Any, edge: str) -> Any | None:
    """The edge's ``cz`` clock, if it is driven parametrically rather than by DC flux.

    A `FluxTunableCoupler` plays its CZ as a microwave tone on the coupler and carries
    a ``clock_freqs.cz`` for it. A `CompositeSquareEdge` pushes one qubit onto the
    crossing with a baseband flux pulse and has no such clock — for that edge there is
    no drive frequency to find, and a routine asking for one should decline rather
    than invent it.
    """
    element = device.get_edge(edge)
    clocks = getattr(element, "clock_freqs", None)
    if clocks is None or not hasattr(clocks, "cz"):
        return None
    return element


class CouplerAnticrossing(CalibrationRoutine):
    """Find where the coupler crosses a qubit, and park it a stated distance away.

    The one routine in the graph that a single schedule cannot express. Everything
    else sweeps *pulse* parameters, which a schedule holds; a coupler's parking bias
    is a DC current held for as long as the fridge is cold, delivered out of band over
    qcodes through an SPI rack or a cluster output. So this sets instrument state, runs
    a schedule, reads it, and repeats — see `CalibrationRoutine.measure`, which exists
    for this node and is overridden by no other.

    **What it measures** is the coupler's flux arc, through the qubit. A parked coupler
    repels every qubit it touches by ``g^2/(f_q - f_c)``, so sweeping the current walks
    the coupler down and drags the qubit's frequency with it — gently far away, then
    steeply, then the other way once the coupler has passed through. Locating that
    crossing is what turns a bias current from a number in a file into a measurement.

    **What it writes** is a parking current derived from the crossing by a stated rule,
    rather than the crossing itself. A tunable coupler is parked *away* from its
    qubits, where the residual coupling is small and the modulated CZ still reaches;
    how far away is a choice about that trade, so it is a configurable fraction and
    the crossing is reported alongside it. Calling the fraction a measurement would be
    dressing up a decision.

    It declines when there is no way to hold a current — see `applies_to`. Against a
    real cluster the bias belongs to the executor, held across jobs rather than for the
    length of one calibration, so a live rack here is a separate decision.
    """

    name = "coupler_anticrossing"
    depends_on = ("rabi",)
    targets = "edges"
    updates = ("bias.parking_current",)

    #: Where to park, as a fraction of the crossing current. Well below it: the push
    #: at 60% of the crossing is a couple of megahertz where at 97% it is tens, and
    #: the point of parking is to be somewhere the coupler is *not* doing anything
    #: until a CZ tone asks it to.
    PARKING_FRACTION = 0.6

    def applies_to(self, device: Any, target: str) -> bool:
        """Only to an edge that carries a bias to park."""
        bias = getattr(device.get_edge(target), "bias", None)
        return bias is not None and hasattr(bias, "parking_current")

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        """There is no single schedule. See :meth:`measure`.

        Present because the base class requires it, and raising because a caller that
        reached it has taken the ordinary path for a routine that cannot use it — a
        silent empty schedule would be a measurement of nothing.
        """
        raise RoutineError(
            f"{self.name} sweeps a DC bias, which no single schedule holds — it is "
            f"run through `measure` instead"
        )

    def analyse(
        self, dataset: Any, target: str, device: Any, config: RoutineConfig
    ) -> dict[str, Any]:
        """Likewise: the fitting happens inside :meth:`measure`, per bias point."""
        raise RoutineError(
            f"{self.name} analyses each bias point as it goes — see `measure`"
        )

    def measure(
        self,
        target: str,
        device: Any,
        config: RoutineConfig,
        backend: SchedulerBackend,
        bias: Any = None,
    ) -> dict[str, Any]:
        if bias is None:
            raise RoutineError(
                f"{target} has no way to hold a parking current, so its coupler's "
                f"crossing cannot be swept — the bias is delivered out of band and "
                f"this routine needs to drive it"
            )
        from qpi_driver.executors.utils.coupler_bias import bias_settings

        element = device.get_edge(target)
        settings = bias_settings(element)
        parent, _child = qubits_of(target)
        currents = setpoints_of(config, "currents", linear_setpoints(0.0, 3.0e-3, 13))
        span = float(config.get("span", 200e6))
        points = int(config.get("points", 11))
        base = float(read_path(device.get_element(parent), "clock_freqs.f01"))
        frequencies = linear_setpoints(base - span / 2, base + span / 2, points)

        original = float(read_path(element, "bias.parking_current"))
        found: list[tuple[float, float]] = []
        try:
            for current in currents:
                bias.apply(target, float(current), settings)
                schedule = self._probe(parent, frequencies, config, backend)
                try:
                    fitted = fit_resonator_spectroscopy(
                        frequencies, signal_of(backend.run(schedule))
                    )
                except FitError:
                    # A bias that pushed the qubit clean out of the window is not a
                    # failure of the sweep: it is the sweep working, and the crossing
                    # is nearby. Skipped rather than fatal.
                    continue
                found.append((float(current), float(fitted["readout_frequency"])))
        finally:
            # Whatever happened, the coupler goes back where it was. Leaving a chip
            # parked at the last current a sweep happened to try is the one outcome
            # worse than not measuring: every later routine would run against it.
            bias.apply(target, original, settings)

        return self._locate(found, base, config)

    def _probe(
        self,
        qubit: str,
        frequencies: list[float],
        config: RoutineConfig,
        backend: SchedulerBackend,
    ) -> Any:
        """A short qubit spectroscopy — enough to say where the line moved to."""
        amplitude = float(config.get("drive_amp", 0.10))
        schedule = backend.new_schedule(
            self.name, repetitions=int(config.get("shots", 512))
        )
        for index, frequency in enumerate(frequencies):
            schedule.add(backend.Reset(qubit))
            schedule.add(
                backend.SetClockFrequency(clock=f"{qubit}.01", clock_freq_new=frequency)
            )
            schedule.add(backend.Rxy(theta=180, phi=0, qubit=qubit, amp180=amplitude))
            schedule.add(
                backend.Measure(
                    qubit, acq_index=index, bin_mode=backend.BinMode.AVERAGE
                )
            )
        return schedule

    def _locate(
        self, found: list[tuple[float, float]], base: float, config: RoutineConfig
    ) -> dict[str, Any]:
        """The crossing, from where the qubit moved fastest with current."""
        if len(found) < 3:
            raise RoutineError(
                f"only {len(found)} bias points produced a fittable line, which is "
                f"too few to locate a crossing — widen 'span' or narrow 'currents'"
            )
        currents = np.array([c for c, _f in found])
        shifts = np.array([f - base for _c, f in found])
        # The crossing is where the push runs away, so it is where the *step* between
        # neighbouring bias points is largest. Not where the shift itself is largest:
        # past the crossing the sign flips and the magnitude comes back down, so the
        # extreme value sits beside the crossing rather than on it.
        steps = np.abs(np.diff(shifts))
        index = int(np.argmax(steps))
        crossing = float((currents[index] + currents[index + 1]) / 2.0)
        fraction = float(config.get("parking_fraction", self.PARKING_FRACTION))
        return {
            "crossing_current": crossing,
            "parking_current": crossing * fraction,
            "max_shift": float(np.max(np.abs(shifts))),
            "points": len(found),
        }

    def apply(self, device: Any, target: str, params: dict[str, Any]) -> None:
        write_path(
            device.get_edge(target), "bias.parking_current", params["parking_current"]
        )


class CZSpectroscopy(CalibrationRoutine):
    """Find the frequency a parametric CZ has to be driven at.

    The coupler is modulated at microwave frequency and a sideband of that modulation
    bridges ``|11>``-``|02>``. Amplitude sets how fast the exchange runs; the
    *frequency* sets whether it runs at all — so a drive off the transition gives a
    gate that compiles, plays, and does nothing.

    Nothing measured it. ``clock_freqs.cz`` is hand-set on every edge that has one,
    and the device model deliberately keeps it separate from ``clock_freqs.sideband_gap``
    — the frequency it is *played at* against the transition it means to bridge — so
    that a mistuned drive is a detectable mistake rather than a definition. This is
    what detects it.

    Prepares ``|11>`` and watches the parent leave it. Off resonance the population
    stays put and the sweep is flat; on it the exchange runs and the parent drops
    towards ``|0>``, which is a peak to fit like any other spectroscopy.

    Declines on a `CompositeSquareEdge`: a DC-flux CZ has no drive frequency, and its
    resonance condition is the flux amplitude `cz_chevron` already sweeps.
    """

    name = "cz_spectroscopy"
    depends_on = ("rabi",)
    targets = "edges"
    updates = ("clock_freqs.cz",)

    def applies_to(self, device: Any, target: str) -> bool:
        """Only to an edge whose CZ is a drive rather than a flux pulse."""
        return parametric_edge(device, target) is not None

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        element = parametric_edge(device, target)
        if element is None:
            raise RoutineError(
                f"edge {target!r} drives its CZ with a baseband flux pulse, so it has "
                f"no drive frequency to find — see `cz_chevron` for its amplitude"
            )
        parent, child = qubits_of(target)
        centre = config.get("centre_frequency")
        if centre is None:
            configured = float(read_path(element, "clock_freqs.cz"))
            # An uncalibrated edge carries zero, which is not a frequency to scan
            # around. The default centre is a plausible coupler sideband rather than
            # a measurement, and a chip that knows better says so in its config.
            centre = configured or float(config.get("prior", 4.0e9))
        span = float(config.get("span", 400e6))
        points = int(config.get("points", 81))
        self._frequencies = setpoints_of(
            config,
            "frequencies",
            linear_setpoints(centre - span / 2, centre + span / 2, points),
        )
        amplitude = float(
            config.get("amplitude", read_path(element, "cz.square_amp") or 0.5)
        )
        # Long enough that a resonant drive moves most of the population, short
        # enough that it has not come back: a quarter of a round trip at the
        # nominal rate. Off resonance the length makes no difference, which is the
        # asymmetry the sweep reads.
        duration = grid_duration(float(config.get("duration", 100e-9)))

        schedule = backend.new_schedule(
            self.name, repetitions=int(config.get("shots", 512))
        )
        # The edge's CZ clock is declared inside the CZ gate's own subschedule, not
        # by the device, so a raw pulse on it has nothing to reference. Every other
        # spectroscopy sweeps a clock the element already owns; this one brings its
        # own, then retunes it point by point like the rest.
        clock = f"{target}.cz"
        schedule.add_resource(
            backend.ClockResource(name=clock, freq=self._frequencies[0])
        )
        for index, frequency in enumerate(self._frequencies):
            schedule.add(backend.Reset(parent))
            schedule.add(backend.Reset(child))
            schedule.add(backend.X(parent))
            schedule.add(backend.X(child))
            schedule.add(
                backend.SetClockFrequency(clock=clock, clock_freq_new=frequency)
            )
            schedule.add(
                backend.SquarePulse(
                    amp=amplitude,
                    duration=duration,
                    port=f"{target}:fl",
                    clock=clock,
                )
            )
            schedule.add(
                backend.Measure(
                    parent, acq_index=index, bin_mode=backend.BinMode.AVERAGE
                )
            )
        return schedule

    def analyse(
        self, dataset: xr.Dataset, target: str, device: Any, config: RoutineConfig
    ) -> dict[str, Any]:
        fitted = fit_resonator_spectroscopy(self._frequencies, signal_of(dataset))
        return {"clock_freq_cz": fitted["readout_frequency"], **fitted}

    def apply(self, device: Any, target: str, params: dict[str, Any]) -> None:
        element = parametric_edge(device, target)
        if element is not None:
            write_path(element, "clock_freqs.cz", params["clock_freq_cz"])


class CZChevron(CalibrationRoutine):
    """Sweep flux amplitude and duration to find the CZ (DiCarlo et al., Nature 460, 240)."""

    name = "cz_chevron"
    depends_on = ("rb", "flux_spectroscopy")
    targets = "edges"
    updates = ("cz.square_amp", "cz.square_duration")

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        control, _child = qubits_of(target)
        self._amplitudes = setpoints_of(
            config, "amplitudes", linear_setpoints(0.1, 0.6, 11)
        )
        self._durations = setpoints_of(
            config, "durations", linear_setpoints(20e-9, 200e-9, 11)
        )
        port = f"{control}:fl"
        schedule = backend.new_schedule(
            self.name, repetitions=int(config.get("shots", 512))
        )

        index = 0
        for amplitude in self._amplitudes:
            for duration in self._durations:
                parent, child = qubits_of(target)
                schedule.add(backend.Reset(parent))
                schedule.add(backend.Reset(child))
                # |11> is the state that exchanges with |02>, so both qubits are
                # excited before the flux pulse brings them into resonance.
                schedule.add(backend.X(parent))
                schedule.add(backend.X(child))
                schedule.add(
                    backend.SquarePulse(
                        amp=amplitude,
                        duration=duration,
                        port=port,
                        clock="cl0.baseband",
                    )
                )
                schedule.add(
                    backend.Measure(
                        parent, acq_index=index, bin_mode=backend.BinMode.AVERAGE
                    )
                )
                index += 1
        return schedule

    def analyse(
        self, dataset: xr.Dataset, target: str, device: Any, config: RoutineConfig
    ) -> dict[str, Any]:
        return fit_chevron(
            np.asarray(self._amplitudes),
            np.asarray(self._durations),
            signal_of(dataset),
        )

    def apply(self, device: Any, target: str, params: dict[str, Any]) -> None:
        """Write the operating point to the names the edge actually has.

        ``square_amp`` and ``square_duration``, not ``amp`` and ``duration``:
        both schedulers' CZ submodule spells them the first way, and writing the
        second raised on every real device — so a chevron that had measured the
        gate correctly failed at the last step, and `conditional_phase` never ran
        because it depends on this one.
        """
        edge = device.get_edge(target)
        write_path(edge, "cz.square_amp", params["cz_amplitude"])
        write_path(edge, "cz.square_duration", grid_duration(params["cz_duration"]))


class ConditionalPhase(CalibrationRoutine):
    """Tune the CZ's conditional phase to π (Sung et al., PRX 11, 021058)."""

    name = "conditional_phase"
    depends_on = ("cz_chevron",)
    targets = "edges"
    # Named by role; `apply` resolves them to whatever this edge actually calls
    # them, which differs between the two schedulers.
    updates = ("cz.parent_phase_correction", "cz.child_phase_correction")

    def build_schedule(
        self, target: str, device: Any, config: RoutineConfig, backend: SchedulerBackend
    ) -> Any:
        parent, child = qubits_of(target)
        self._phases = setpoints_of(config, "phases", linear_setpoints(0.0, 360.0, 25))
        schedule = backend.new_schedule(
            self.name, repetitions=int(config.get("shots", 512))
        )

        # Four fringes, not two. A Ramsey on one qubit with the other down and
        # then up gives the conditional phase from the offset between them, and
        # the *ground* fringe's own phase gives that qubit's single-qubit phase
        # over the CZ. Both qubits are needed because each carries its own, and
        # the edge has a separate correction for each — measuring one and
        # assuming the other is how a CZ ends up building the wrong Bell state
        # while every reported number looks right.
        index = 0
        for measured, spectator in ((parent, child), (child, parent)):
            for spectator_excited in (False, True):
                for phase in self._phases:
                    schedule.add(backend.Reset(measured))
                    schedule.add(backend.Reset(spectator))
                    if spectator_excited:
                        schedule.add(backend.X(spectator))
                    schedule.add(backend.Rxy(theta=90, phi=0, qubit=measured))
                    schedule.add(backend.CZ(parent, child))
                    schedule.add(backend.Rxy(theta=90, phi=phase, qubit=measured))
                    schedule.add(
                        backend.Measure(
                            measured,
                            acq_index=index,
                            bin_mode=backend.BinMode.AVERAGE,
                        )
                    )
                    index += 1
        return schedule

    def analyse(
        self, dataset: xr.Dataset, target: str, device: Any, config: RoutineConfig
    ) -> dict[str, Any]:
        signal = signal_of(dataset)
        count = len(self._phases)
        if signal.size < 4 * count:
            raise RoutineError(
                f"conditional phase expected {4 * count} acquisitions, got {signal.size}"
            )

        phases = np.asarray(self._phases)
        # Both fringes of a pair, not their difference: the measured qubit's
        # dynamical phase over the flux pulse cancels between them and does not
        # cancel within either.
        parent_fit = fit_conditional_phase(
            phases, signal[:count], signal[count : 2 * count]
        )
        child_fit = fit_conditional_phase(
            phases, signal[2 * count : 3 * count], signal[3 * count : 4 * count]
        )

        return {
            **parent_fit,
            "parent_phase_correction": parent_fit["reference_phase"],
            "child_phase_correction": child_fit["reference_phase"],
            # The same gate seen from either qubit, so the two conditional
            # phases are a consistency check rather than two measurements.
            "conditional_phase_from_child": child_fit["conditional_phase"],
        }

    def apply(self, device: Any, target: str, params: dict[str, Any]) -> None:
        """Write the two single-qubit corrections the CZ left behind.

        Not the conditional phase: that is fixed by the pulse's amplitude and
        duration, and no virtual Z can change it. What these two parameters
        cancel is the single-qubit phase each qubit accumulated during the flux
        pulse, which is what stops a conditional-phase-correct CZ from building
        the Bell state it should.

        The names are resolved from the edge rather than assumed, because the
        two schedulers spell them differently and the name this used to write —
        ``cz.phase_correction`` — is one neither has, so it silently applied
        nothing at all.
        """
        edge = device.get_edge(target)
        names = phase_correction_names(edge)
        if names is None:
            return  # an edge with no virtual-Z corrections to set

        parent_name, child_name = names
        write_path(edge, f"cz.{parent_name}", params["parent_phase_correction"])
        write_path(edge, f"cz.{child_name}", params["child_phase_correction"])
