"""Two-qubit gate calibration: the CZ chevron and its conditional phase (RFC 0004 §3).

These target edges rather than qubits. An edge is named ``<parent>_<child>``,
which is how the two qubits it acts on are recovered.
"""

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
import xarray as xr

from qpi_driver.tuners.base.backend import SchedulerBackend
from qpi_driver.tuners.base.config import DEFAULT_ROUTINE_TIMEOUT_S, RoutineConfig
from qpi_driver.tuners.base.fusion import (
    add_after,
    add_together,
    channels_of,
    grouped_by_grid,
    grouped_by_size,
)
from qpi_driver.tuners.base.device import (
    has_flux_port,
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
from qpi_driver.tuners.base.sweep import Sweep
from qpi_driver.tuners.fitting import (
    FitError,
    fit_chevron,
    fit_conditional_phase,
    fit_rabi,
    fit_resonator_spectroscopy,
    signal_of,
)


def prepare_11(schedule: Any, backend: SchedulerBackend, edges: Sequence[str]) -> Any:
    """Reset and excite both qubits of every edge, returning the anchor.

    ``|11>`` is the state that exchanges with ``|02>``, so both qubits are excited before
    the pulse brings them into resonance. Shared because all four CZ sweeps open this way,
    and a fused group needs two edges' four resets and four X pulses to coincide rather
    than to queue.
    """
    pairs = [qubits_of(edge) for edge in edges]
    add_together(schedule, [backend.Reset(q) for pair in pairs for q in pair])
    return add_together(schedule, [backend.X(q) for pair in pairs for q in pair])


def measure_parents(
    backend: SchedulerBackend, edges: Sequence[str], index: int
) -> list[Any]:
    """One measurement per edge, on its parent and on the edge's own channel.

    The parent is the qubit the exchange leaves population in, so it is the one every CZ
    sweep reads. The channel is the edge's position in the group, which is what
    `channels_of` slices the result apart by.
    """
    return [
        backend.Measure(
            qubits_of(edge)[0],
            acq_channel=channel,
            acq_index=index,
            bin_mode=backend.BinMode.AVERAGE,
        )
        for channel, edge in enumerate(edges)
    ]


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
    #: Its own edge's only. `measure` also reads the *parent qubit's*
    #: ``clock_freqs.f01`` to centre each probe sweep, which a path on this routine's
    #: own target cannot name — `depends_on = ("rabi",)` is what orders that, and this
    #: is the one read in the graph that the notation does not reach. Declared by hand
    #: because this routine builds no schedule for the derivation test to instrument.
    reads = ("bias.parking_current",)

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
        self,
        target: str,
        device: Any,
        config: RoutineConfig,
        backend: SchedulerBackend,
        sweep: Sweep,
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
        self,
        dataset: Any,
        target: str,
        device: Any,
        config: RoutineConfig,
        sweep: Sweep,
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
        sweep: Sweep,
        bias: Any = None,
        timeout_s: float = DEFAULT_ROUTINE_TIMEOUT_S,
    ) -> dict[str, Any]:
        if bias is None or not getattr(bias, "holds_current", False):
            # A recorder counts as nothing here, and that distinction is the point.
            # Against one, every bias point returns the same qubit frequency, the
            # sweep is flat, and the fit reports a crossing with total confidence —
            # a number written to the device that no instrument ever produced. It is
            # the exact failure this whole node exists to make impossible.
            raise RoutineError(
                f"{target} has no source that can actually hold a parking current, so "
                f"its coupler's crossing cannot be swept — the bias is delivered out "
                f"of band, and a recorder would make this measure nothing at all. On "
                f"hardware: check `bias.source` on the edge, and pass "
                f"-o spi_rack_address=<port> if it says `spi`"
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
                        frequencies,
                        signal_of(backend.run(schedule, timeout_s=timeout_s)),
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

    def compatible_groups(
        self, targets: Sequence[str], device: Any, config: RoutineConfig
    ) -> list[list[str]]:
        """By probe size: each band is centred on that edge's own parent's f01."""
        return grouped_by_size(targets, lambda t: self._probe_band(device, t, config))

    def _probe_band(
        self, device: Any, target: str, config: RoutineConfig
    ) -> list[float]:
        """Where to look for this edge's parent, around where it currently sits."""
        parent, _child = qubits_of(target)
        base = float(read_path(device.get_element(parent), "clock_freqs.f01"))
        span = float(config.get("span", 200e6))
        points = int(config.get("points", 11))
        return list(linear_setpoints(base - span / 2, base + span / 2, points))

    def measure_group(
        self,
        targets: Sequence[str],
        device: Any,
        config: RoutineConfig,
        backend: SchedulerBackend,
        sweeps: Mapping[str, Sweep],
        bias: Any = None,
        timeout_s: float = DEFAULT_ROUTINE_TIMEOUT_S,
    ) -> dict[str, dict[str, Any] | Exception]:
        """Sweep every coupler's parking current at once, one probe per setpoint.

        A rack is shared; its *channels* are not. Each edge names its own S4g output or its
        own baseband output — `bias.spi_module`/`bias.spi_output`, or the `qcm` pair — so
        setting a group's currents is one quick write per edge over the same serial port and
        then a *single* acquisition, not one per edge. What is sequential here is within one
        coupler, across its own current setpoints, exactly as everywhere else in this graph.

        The parents are distinct within a group because `edge_spacing` will not put two
        edges sharing a qubit in one, and that is what lets a single probe read all of them.
        """
        if bias is None or not getattr(bias, "holds_current", False):
            return {target: self._no_bias_source(target) for target in targets}
        from qpi_driver.executors.utils.coupler_bias import bias_settings

        settings = {t: bias_settings(device.get_edge(t)) for t in targets}
        originals = {
            t: float(read_path(device.get_edge(t), "bias.parking_current"))
            for t in targets
        }
        bands = {t: self._probe_band(device, t, config) for t in targets}
        parents = {t: qubits_of(t)[0] for t in targets}
        bases = {
            t: float(read_path(device.get_element(parents[t]), "clock_freqs.f01"))
            for t in targets
        }
        currents = setpoints_of(config, "currents", linear_setpoints(0.0, 3.0e-3, 13))
        for target in targets:
            sweeps[target]["currents"] = currents

        found: dict[str, list[tuple[float, float]]] = {t: [] for t in targets}
        try:
            for current in currents:
                for target in targets:
                    bias.apply(target, float(current), settings[target])
                schedule = self._probe_group(targets, parents, bands, config, backend)
                sliced = channels_of(
                    backend.run(schedule, timeout_s=timeout_s), list(targets)
                )
                for target in targets:
                    acquisition = sliced.get(target)
                    if acquisition is None:
                        continue
                    try:
                        fitted = fit_resonator_spectroscopy(
                            bands[target], signal_of(acquisition)
                        )
                    except FitError:
                        continue
                    found[target].append(
                        (float(current), float(fitted["readout_frequency"]))
                    )
        finally:
            # Every coupler back where it was, whatever happened — an abandoned sweep
            # must not leave the chip parked at the last current it happened to try.
            for target in targets:
                bias.apply(target, originals[target], settings[target])

        results: dict[str, dict[str, Any] | Exception] = {}
        for target in targets:
            try:
                results[target] = self._locate(found[target], bases[target], config)
            except RoutineError as exc:
                results[target] = exc
        return results

    def _probe_group(
        self,
        targets: Sequence[str],
        parents: Mapping[str, str],
        bands: Mapping[str, list[float]],
        config: RoutineConfig,
        backend: SchedulerBackend,
    ) -> Any:
        """One short spectroscopy per edge, on its parent and its own channel."""
        amplitude = float(config.get("drive_amp", 0.10))
        schedule = backend.new_schedule(
            self.name, repetitions=int(config.get("shots", 512))
        )
        for index in range(len(bands[targets[0]])):
            anchor = add_together(
                schedule, [backend.Reset(parents[t]) for t in targets]
            )
            add_together(
                schedule,
                [
                    backend.SetClockFrequency(
                        clock=f"{parents[t]}.01", clock_freq_new=bands[t][index]
                    )
                    for t in targets
                ],
            )
            anchor = add_after(
                schedule,
                [
                    backend.Rxy(theta=180, phi=0, qubit=parents[t], amp180=amplitude)
                    for t in targets
                ],
                anchor,
            )
            add_after(
                schedule,
                [
                    backend.Measure(
                        parents[target],
                        acq_channel=channel,
                        acq_index=index,
                        bin_mode=backend.BinMode.AVERAGE,
                    )
                    for channel, target in enumerate(targets)
                ],
                anchor,
            )
        return schedule

    def _no_bias_source(self, target: str) -> RoutineError:
        """The refusal both paths give, worded once.

        A recorder counts as nothing here, and that distinction is the point. Against one,
        every bias point returns the same qubit frequency, the sweep is flat, and the fit
        reports a crossing with total confidence — a number written to the device that no
        instrument ever produced. It is the exact failure this node exists to prevent.
        """
        return RoutineError(
            f"{target} has no source that can actually hold a parking current, so "
            f"its coupler's crossing cannot be swept — the bias is delivered out "
            f"of band, and a recorder would make this measure nothing at all. On "
            f"hardware: check `bias.source` on the edge, and pass "
            f"-o spi_rack_address=<port> if it says `spi`"
        )

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
    reads = ("clock_freqs.cz", "cz.square_amp", "clock_freqs.f01", "rxy.amp180")

    def applies_to(self, device: Any, target: str) -> bool:
        """Only to an edge whose CZ is a drive rather than a flux pulse."""
        return parametric_edge(device, target) is not None

    def build_schedule(
        self,
        target: str,
        device: Any,
        config: RoutineConfig,
        backend: SchedulerBackend,
        sweep: Sweep,
    ) -> Any:
        return self.build_group_schedule(
            [target], device, config, backend, {target: sweep}
        )

    def compatible_groups(
        self, targets: Sequence[str], device: Any, config: RoutineConfig
    ) -> list[list[str]]:
        """Edges whose sweeps have the same *number* of points, not the same points.

        Each edge drives its own clock on its own port, so at setpoint *i* every edge can
        sit at its own frequency — which it must, since the band is centred on that edge's
        own CZ clock. Only the point count has to agree, because the acquisition index is
        shared. See `grouped_by_size`.
        """
        return grouped_by_size(targets, lambda t: self._band(device, t, config))

    def _band(self, device: Any, target: str, config: RoutineConfig) -> list[float]:
        """This edge's drive-frequency sweep, centred on its own CZ clock."""
        element = parametric_edge(device, target)
        if element is None:
            raise RoutineError(
                f"edge {target!r} drives its CZ with a baseband flux pulse, so it has "
                f"no drive frequency to find — see `cz_chevron` for its amplitude"
            )
        centre = config.get("centre_frequency")
        if centre is None:
            configured = float(read_path(element, "clock_freqs.cz"))
            # An uncalibrated edge carries zero, which is not a frequency to scan around.
            # The default centre is a plausible coupler sideband rather than a measurement,
            # and a chip that knows better says so in its config.
            centre = configured or float(config.get("prior", 4.0e9))
        span = float(config.get("span", 400e6))
        points = int(config.get("points", 81))
        return setpoints_of(
            config,
            "frequencies",
            linear_setpoints(centre - span / 2, centre + span / 2, points),
        )

    def build_group_schedule(
        self,
        targets: Sequence[str],
        device: Any,
        config: RoutineConfig,
        backend: SchedulerBackend,
        sweeps: Mapping[str, Sweep],
    ) -> Any:
        """Every edge's drive swept at once, each over its own band on its own clock."""
        bands = {}
        amplitudes = {}
        for target in targets:
            element = parametric_edge(device, target)
            bands[target] = self._band(device, target, config)
            sweeps[target]["frequencies"] = bands[target]
            amplitudes[target] = float(
                config.get("amplitude", read_path(element, "cz.square_amp") or 0.5)
            )
        # Long enough that a resonant drive moves most of the population, short enough that
        # it has not come back: a quarter of a round trip at the nominal rate. Off resonance
        # the length makes no difference, which is the asymmetry the sweep reads.
        duration = grid_duration(float(config.get("duration", 100e-9)))

        schedule = backend.new_schedule(
            self.name, repetitions=int(config.get("shots", 512))
        )
        # An edge's CZ clock is declared inside the CZ gate's own subschedule, not by the
        # device, so a raw pulse on it has nothing to reference. Every other spectroscopy
        # sweeps a clock the element already owns; these bring their own, one per edge, then
        # retune them point by point like the rest.
        clocks = {target: f"{target}.cz" for target in targets}
        for target in targets:
            schedule.add_resource(
                backend.ClockResource(name=clocks[target], freq=bands[target][0])
            )
        for index in range(len(bands[targets[0]])):
            anchor = prepare_11(schedule, backend, targets)
            add_together(
                schedule,
                [
                    backend.SetClockFrequency(
                        clock=clocks[target], clock_freq_new=bands[target][index]
                    )
                    for target in targets
                ],
            )
            anchor = add_after(
                schedule,
                [
                    backend.SquarePulse(
                        amp=amplitudes[target],
                        duration=duration,
                        port=f"{target}:fl",
                        clock=clocks[target],
                    )
                    for target in targets
                ],
                anchor,
            )
            add_after(schedule, measure_parents(backend, targets, index), anchor)
        return schedule

    def analyse(
        self,
        dataset: xr.Dataset,
        target: str,
        device: Any,
        config: RoutineConfig,
        sweep: Sweep,
    ) -> dict[str, Any]:
        fitted = fit_resonator_spectroscopy(sweep["frequencies"], signal_of(dataset))
        return {"clock_freq_cz": fitted["readout_frequency"], **fitted}

    def apply(self, device: Any, target: str, params: dict[str, Any]) -> None:
        element = parametric_edge(device, target)
        if element is not None:
            write_path(element, "clock_freqs.cz", params["clock_freq_cz"])


class CZParametrization(CalibrationRoutine):
    """How fast a parametric CZ runs, and the pulse that makes it one round trip.

    `cz_chevron`'s counterpart for a coupler-driven edge, and needed because the two
    gates are resonant in different variables. A DC-flux CZ is brought onto the
    ``|11>``-``|02>`` crossing by *amplitude*, so its calibration is a 2D chevron over
    amplitude and duration. A parametric CZ is brought onto it by *frequency* — which
    `cz_spectroscopy` has already found — and amplitude only sets how fast the exchange
    then runs. So there is no chevron here: at the right frequency the population
    simply oscillates in duration, linearly faster with drive.

    That linearity is the parametrization, and it is what makes the gate predictable:
    measure the rate once and any (amplitude, duration) pair with the right product is
    a CZ. `PARAMETRIC_RATE_MHZ` in the simulator is that rate as a *chosen* constant,
    averaged from four calibrated edges off a real chip — this is the routine that
    would measure it instead.

    Writes a full ``|11> -> |02> -> |11>`` round trip, not half of one. Half is
    complete population transfer into ``|02>``: a perfectly good gate, measured just as
    confidently, and not a CZ. That mistake cost `cz_chevron` a 55 ns duration against
    a 110 ns round trip for as long as nothing checked the number itself.
    """

    name = "cz_parametrization"
    depends_on = ("cz_spectroscopy",)
    targets = "edges"
    updates = ("cz.square_amp", "cz.square_duration")
    reads = ("clock_freqs.cz", "cz.square_amp", "clock_freqs.f01", "rxy.amp180")

    def applies_to(self, device: Any, target: str) -> bool:
        """Only to an edge whose CZ is a drive — the same test `cz_spectroscopy` makes."""
        return parametric_edge(device, target) is not None

    def build_schedule(
        self,
        target: str,
        device: Any,
        config: RoutineConfig,
        backend: SchedulerBackend,
        sweep: Sweep,
    ) -> Any:
        return self.build_group_schedule(
            [target], device, config, backend, {target: sweep}
        )

    def compatible_groups(
        self, targets: Sequence[str], device: Any, config: RoutineConfig
    ) -> list[list[str]]:
        """Edges whose durations agree exactly, since a pulse length is shared time.

        Unlike `cz_spectroscopy`'s frequency axis, this one is the timeline itself: a 300 ns
        pulse occupies 300 ns of the schedule for every edge in it. `grouped_by_grid`, not
        `grouped_by_size`. In practice they always agree — the durations come from the
        config, not from the chip.
        """
        return grouped_by_grid(targets, lambda _target: self._durations(config))

    def _durations(self, config: RoutineConfig) -> list[float]:
        """The pulse lengths swept, on the instrument's nanosecond grid."""
        return [
            grid_duration(duration)
            for duration in setpoints_of(
                config, "durations", linear_setpoints(20e-9, 400e-9, 39)
            )
        ]

    def build_group_schedule(
        self,
        targets: Sequence[str],
        device: Any,
        config: RoutineConfig,
        backend: SchedulerBackend,
        sweeps: Mapping[str, Sweep],
    ) -> Any:
        """Every edge's coupler driven for the same lengths, each at its own operating point.

        The durations are one axis for the group; the drive frequency and amplitude are read
        per edge, since each has its own calibrated point and its own clock.
        """
        durations = self._durations(config)
        clocks = {}
        amplitudes = {}
        for target in targets:
            element = parametric_edge(device, target)
            if element is None:
                raise RoutineError(
                    f"edge {target!r} drives its CZ with a baseband flux pulse — see "
                    f"`cz_chevron`, which sweeps the amplitude its resonance lives in"
                )
            sweeps[target]["durations"] = durations
            sweeps[target]["amplitude"] = amplitudes[target] = float(
                config.get("amplitude", read_path(element, "cz.square_amp") or 0.5)
            )
            clocks[target] = f"{target}.cz"

        schedule = backend.new_schedule(
            self.name, repetitions=int(config.get("shots", 512))
        )
        for target in targets:
            schedule.add_resource(
                backend.ClockResource(
                    name=clocks[target],
                    freq=float(
                        read_path(parametric_edge(device, target), "clock_freqs.cz")
                    ),
                )
            )
        for index, duration in enumerate(durations):
            anchor = prepare_11(schedule, backend, targets)
            anchor = add_after(
                schedule,
                [
                    backend.SquarePulse(
                        amp=amplitudes[target],
                        duration=duration,
                        port=f"{target}:fl",
                        clock=clocks[target],
                    )
                    for target in targets
                ],
                anchor,
            )
            add_after(schedule, measure_parents(backend, targets, index), anchor)
        return schedule

    def analyse(
        self,
        dataset: xr.Dataset,
        target: str,
        device: Any,
        config: RoutineConfig,
        sweep: Sweep,
    ) -> dict[str, Any]:
        durations = np.asarray(sweep["durations"], dtype=float)
        # `fit_rabi` fits a cosine and reports where the *half* period falls. Its axis
        # is normally a drive amplitude and here it is a duration, which changes
        # nothing about the arithmetic: a cosine is a cosine.
        fitted = fit_rabi(durations, signal_of(dataset))
        half = float(fitted["amp180"])
        if half <= 0:
            raise RoutineError(
                f"the parametric exchange did not oscillate over {durations[-1] * 1e9:.0f} "
                f"ns at amplitude {sweep['amplitude']:.4g} — the drive may be off the "
                f"transition, which is `cz_spectroscopy`'s job to place"
            )
        round_trip = 2.0 * half
        return {
            "cz_amplitude": sweep["amplitude"],
            "cz_duration": grid_duration(round_trip),
            # Per unit amplitude, which is the parametrization. The factor is four
            # rather than two and that is a convention rather than an accident: the
            # exchange term carries the rate *undivided*, so the population oscillates
            # at twice it and a half period is ``1/(4 x rate x amplitude)``. Reported
            # in the same convention as the simulator's `PARAMETRIC_RATE_MHZ`, which
            # is the constant this routine exists to replace — a chip calibrated in
            # some other convention would differ by exactly this factor, which is why
            # it is written down rather than folded in.
            "exchange_rate_hz_per_unit": 1.0 / (4.0 * half * sweep["amplitude"]),
            "half_period": half,
        }

    def apply(self, device: Any, target: str, params: dict[str, Any]) -> None:
        element = parametric_edge(device, target)
        if element is None:
            return
        write_path(element, "cz.square_amp", params["cz_amplitude"])
        write_path(element, "cz.square_duration", params["cz_duration"])


class CZChevron(CalibrationRoutine):
    """Sweep flux amplitude and duration to find the CZ (DiCarlo et al., Nature 460, 240)."""

    name = "cz_chevron"
    depends_on = ("rb", "flux_spectroscopy")
    targets = "edges"
    updates = ("cz.square_amp", "cz.square_duration")
    reads = ("clock_freqs.f01", "rxy.amp180")

    def applies_to(self, device: Any, target: str) -> bool:
        """Only to an edge whose CZ *is* a flux pulse — the inverse of the test
        `cz_spectroscopy` and `cz_parametrization` make, and it was missing.

        This pushes the control qubit onto the crossing with a baseband pulse on that
        qubit's own ``q<n>:fl``. A flux-tunable coupler has no such line — the flux
        reaches the coupler instead — so on that chip the sweep cannot be built at
        all, and `cz_parametrization` is the counterpart that calibrates its gate.
        """
        control, _child = qubits_of(target)
        return parametric_edge(device, target) is None and has_flux_port(
            device, control
        )

    def build_schedule(
        self,
        target: str,
        device: Any,
        config: RoutineConfig,
        backend: SchedulerBackend,
        sweep: Sweep,
    ) -> Any:
        return self.build_group_schedule(
            [target], device, config, backend, {target: sweep}
        )

    def compatible_groups(
        self, targets: Sequence[str], device: Any, config: RoutineConfig
    ) -> list[list[str]]:
        """Both axes come from the config, so every edge's grid is the same one."""
        return grouped_by_grid(targets, lambda _target: self._grid(config)[0])

    def _grid(self, config: RoutineConfig) -> tuple[list[float], list[float]]:
        """The chevron's amplitude and duration axes."""
        return (
            setpoints_of(config, "amplitudes", linear_setpoints(0.1, 0.6, 11)),
            setpoints_of(config, "durations", linear_setpoints(20e-9, 200e-9, 11)),
        )

    def build_group_schedule(
        self,
        targets: Sequence[str],
        device: Any,
        config: RoutineConfig,
        backend: SchedulerBackend,
        sweeps: Mapping[str, Sweep],
    ) -> Any:
        """One chevron per edge, each pulse on its own control's flux port.

        The amplitude axis is per-edge hardware — a baseband pulse on ``q<n>:fl`` — so the
        edges can share a grid without their pulses interfering. That holds only because
        `edge_spacing` will not put two edges sharing a qubit in one group.
        """
        amplitudes, durations = self._grid(config)
        for target in targets:
            sweeps[target]["amplitudes"] = amplitudes
            sweeps[target]["durations"] = durations
        schedule = backend.new_schedule(
            self.name, repetitions=int(config.get("shots", 512))
        )

        index = 0
        for amplitude in amplitudes:
            for duration in durations:
                anchor = prepare_11(schedule, backend, targets)
                anchor = add_after(
                    schedule,
                    [
                        backend.SquarePulse(
                            amp=amplitude,
                            duration=duration,
                            port=f"{qubits_of(edge)[0]}:fl",
                            clock="cl0.baseband",
                        )
                        for edge in targets
                    ],
                    anchor,
                )
                add_after(schedule, measure_parents(backend, targets, index), anchor)
                index += 1
        return schedule

    def analyse(
        self,
        dataset: xr.Dataset,
        target: str,
        device: Any,
        config: RoutineConfig,
        sweep: Sweep,
    ) -> dict[str, Any]:
        return fit_chevron(
            np.asarray(sweep["amplitudes"]),
            np.asarray(sweep["durations"]),
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
    reads = ("clock_freqs.f01", "rxy.amp180")

    def build_schedule(
        self,
        target: str,
        device: Any,
        config: RoutineConfig,
        backend: SchedulerBackend,
        sweep: Sweep,
    ) -> Any:
        return self.build_group_schedule(
            [target], device, config, backend, {target: sweep}
        )

    def build_group_schedule(
        self,
        targets: Sequence[str],
        device: Any,
        config: RoutineConfig,
        backend: SchedulerBackend,
        sweeps: Mapping[str, Sweep],
    ) -> Any:
        """The four fringes on every edge at once, each read on the edge's own channel.

        The phase axis is a virtual-Z on the qubit being measured, so it is per-edge
        hardware and the grid can be shared. The group never splits: the phases come from
        the config or from a full turn in 25 steps.
        """
        phases = setpoints_of(config, "phases", linear_setpoints(0.0, 360.0, 25))
        for target in targets:
            sweeps[target]["phases"] = phases
        schedule = backend.new_schedule(
            self.name, repetitions=int(config.get("shots", 512))
        )

        # Four fringes, not two. A Ramsey on one qubit with the other down and then up
        # gives the conditional phase from the offset between them, and the *ground*
        # fringe's own phase gives that qubit's single-qubit phase over the CZ. Both qubits
        # are needed because each carries its own, and the edge has a separate correction
        # for each — measuring one and assuming the other is how a CZ ends up building the
        # wrong Bell state while every reported number looks right.
        index = 0
        for role in (0, 1):
            # Which qubit of each edge this pass reads, and which merely sits excited.
            roles = {
                edge: (qubits_of(edge)[role], qubits_of(edge)[1 - role])
                for edge in targets
            }
            for spectator_excited in (False, True):
                for phase in phases:
                    add_together(
                        schedule,
                        [backend.Reset(q) for edge in targets for q in qubits_of(edge)],
                    )
                    if spectator_excited:
                        add_together(
                            schedule,
                            [backend.X(roles[edge][1]) for edge in targets],
                        )
                    add_together(
                        schedule,
                        [
                            backend.Rxy(theta=90, phi=0, qubit=roles[edge][0])
                            for edge in targets
                        ],
                    )
                    add_together(
                        schedule,
                        [backend.CZ(*qubits_of(edge)) for edge in targets],
                    )
                    anchor = add_together(
                        schedule,
                        [
                            backend.Rxy(theta=90, phi=phase, qubit=roles[edge][0])
                            for edge in targets
                        ],
                    )
                    add_after(
                        schedule,
                        [
                            backend.Measure(
                                roles[edge][0],
                                acq_channel=channel,
                                acq_index=index,
                                bin_mode=backend.BinMode.AVERAGE,
                            )
                            for channel, edge in enumerate(targets)
                        ],
                        anchor,
                    )
                    index += 1
        return schedule

    def analyse(
        self,
        dataset: xr.Dataset,
        target: str,
        device: Any,
        config: RoutineConfig,
        sweep: Sweep,
    ) -> dict[str, Any]:
        signal = signal_of(dataset)
        count = len(sweep["phases"])
        if signal.size < 4 * count:
            raise RoutineError(
                f"conditional phase expected {4 * count} acquisitions, got {signal.size}"
            )

        phases = np.asarray(sweep["phases"])
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
            # The *cancelling* virtual Z, not the fringe phase itself — see
            # `_cancelling`, which is where both conventions are set out.
            "parent_phase_correction": parent_fit["reference_correction"],
            "child_phase_correction": child_fit["reference_correction"],
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
