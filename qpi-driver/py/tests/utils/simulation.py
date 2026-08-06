"""Test doubles built on the transmon simulator (RFC 0004 §7, tier 3).

The simulator itself now lives in `qpi_driver.simulation`, because a thing you
can put where the hardware goes belongs in the package rather than beside the
tests. What stays here is the scaffolding only tests need:

- :class:`StubBackend` records what a routine composes and refuses to run it,
  for a test that supplies the acquisition itself.
- :class:`SimulatedBackend` answers ``run`` by asking the simulator for the
  experiment the schedule encodes — enough to drive a routine through
  ``build_schedule → run → analyse → apply`` with no scheduler installed.
- :class:`SimulatedTuner` puts that behind the real :class:`Tuner`.
- :class:`FakeDevice` stands in for a `QuantumDevice` where a real one would
  need quantify. It serialises, but what it writes names *this* module's class,
  so the file it produces is not one the executor can load — for that, see
  `tests/test_calibration_loop.py`, which uses a real device throughout.
"""

from pathlib import Path
from typing import Any

import numpy as np
import xarray as xr
from qpi_driver.simulation.coupled import CoupledTransmons
from qpi_driver.simulation.transmon import GHZ, NS, TransmonSimulator, _rxy_qobj
from qpi_driver.tuners.base import Tuner
from qpi_driver.tuners.base.config import DEFAULT_ROUTINE_TIMEOUT_S

__all__ = [
    "GHZ",
    "NS",
    "TransmonSimulator",
    "CoupledTransmons",
    "_rxy_qobj",
    "FakeDevice",
    "FakeElement",
    "device_for",
    "RecordingBackend",
    "StubBackend",
    "SimulatedBackend",
    "SimulatedTuner",
]


class FakeElement:
    """A device element that reads and writes like the real ones do.

    Enough of a `QuantumDevice` for a routine's `build_schedule` to read the
    current frequency it should scan around, and for `apply` to write back — so
    tier 3 exercises the whole routine, not only its fit.

    ``name``, ``submodules`` and ``parameters`` are what
    `qpi_driver.tuners.utils.persistence` walks, so a device built from these
    also serialises: an end-to-end calibration writes a real device YAML rather
    than skipping the step that every later job depends on.
    """

    def __init__(self, name: str = "q0", **submodules):
        self.name = name
        self.submodules = {
            attribute: _Submodule(**values) for attribute, values in submodules.items()
        }
        for attribute, submodule in self.submodules.items():
            setattr(self, attribute, submodule)

    @property
    def parameters(self) -> dict[str, Any]:
        """None of its own; every parameter here lives on a submodule."""
        return {}


class _Submodule:
    def __init__(self, **parameters):
        for name, value in parameters.items():
            setattr(self, name, value)

    @property
    def parameters(self) -> dict[str, Any]:
        """Its parameters as plain values, which is what pydantic elements give too."""
        return dict(vars(self))


class FakeDevice:
    """A minimal stand-in for a QuantumDevice: elements per qubit, edges between."""

    def __init__(
        self,
        elements: dict[str, FakeElement],
        edges: dict[str, FakeElement] | None = None,
    ):
        self._elements = elements
        self._edges = edges or {}

    def elements(self):
        return list(self._elements)

    def edges(self):
        return list(self._edges)

    def get_element(self, name: str) -> FakeElement:
        return self._elements[name]

    def get_edge(self, name: str) -> FakeElement:
        return self._edges[name]


def device_for(
    simulator: TransmonSimulator, *qubits: str, edges: tuple[str, ...] = ()
) -> FakeDevice:
    """A device whose current parameters are near, but not at, the true ones.

    Deliberately offset: a routine that scans around the current value has to
    actually find the true one, rather than being handed it.

    Every qubit is given the same starting parameters. They are the same
    simulated transmon — this is a model of one qubit, not of a chip — which is
    enough for what more than one target is here to test: that a routine runs
    once per target, and that a partial recalibration leaves the others alone.
    """
    return FakeDevice(
        {
            qubit: FakeElement(
                name=qubit,
                clock_freqs={
                    "f01": simulator.f01 * GHZ + 3e6,  # 3 MHz off
                    "f12": (simulator.f01 + simulator.anharmonicity) * GHZ,
                    "readout": simulator.readout_frequency_ghz * GHZ,
                },
                rxy={"amp180": 0.18, "motzoi": 0.0},
                measure={"pulse_amp": 0.25},
            )
            for qubit in (qubits or ("q0",))
        },
        {
            edge: FakeElement(
                name=edge,
                # Offset from the true operating point for the same reason the
                # qubits are: the routine has to find it, not be handed it.
                # The names a real CompositeSquareEdge has. A fake shaped to
                # the routine instead cannot contradict it, which is how
                # `cz.amp` survived: the routine wrote it, the fake accepted it,
                # and no real device was ever asked.
                cz={
                    "square_amp": 0.2,
                    "square_duration": 60e-9,
                    "q0_phase_correction": 0.0,
                    "q1_phase_correction": 0.0,
                },
            )
            for edge in edges
        },
    )


class _Operation:
    """One operation as recorded on a schedule.

    A scheduler's operation classes are opaque objects its compiler consumes.
    These stand in for them and keep their arguments legible, which is what lets
    :class:`SimulatedBackend` answer ``run`` from what a routine actually
    composed rather than from what it is assumed to have composed.
    """

    kind = "operation"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs

    def __repr__(self) -> str:
        return f"{self.kind}(*{self.args}, **{self.kwargs})"


def _operation(kind: str) -> type[_Operation]:
    return type(kind, (_Operation,), {"kind": kind})


class _Schedule:
    """A recorded schedule: its name, its repetitions, and its operations in order."""

    def __init__(self, name: str, repetitions: int = 1) -> None:
        self.name = name
        self.repetitions = repetitions
        self.operations: list[_Operation] = []

    def add(self, operation: Any, **kwargs: Any) -> None:
        self.operations.append(operation)

    def __repr__(self) -> str:
        return f"<_Schedule {self.name!r} with {len(self.operations)} operations>"


class RecordingBackend:
    """The scheduler vocabulary, recording rather than compiling.

    Deliberately not a :class:`~qpi_driver.tuners.base.SchedulerBackend`
    subclass: that ABC requires ``run``, and half the point of
    :class:`StubBackend` is that it has none. Routines only ever use the
    attributes, so duck typing is the honest relationship here.
    """

    name = "simulated"
    drag_parameter = "motzoi"

    Schedule = _Schedule
    Reset = _operation("Reset")
    Measure = _operation("Measure")
    Rxy = _operation("Rxy")
    X = _operation("X")
    Y = _operation("Y")
    Rz = _operation("Rz")
    CZ = _operation("CZ")
    IdlePulse = _operation("IdlePulse")
    SquarePulse = _operation("SquarePulse")
    SetClockFrequency = _operation("SetClockFrequency")

    class BinMode:
        AVERAGE = "average"
        APPEND = "append"

    def new_schedule(self, name: str, repetitions: int = 1) -> _Schedule:
        return _Schedule(name, repetitions)

    def idle(self, schedule: _Schedule, duration: float) -> None:
        schedule.add(self.IdlePulse(duration=duration))


class StubBackend(RecordingBackend):
    """Just enough backend for a routine to build a schedule.

    For a test that supplies the acquisition itself: the schedule is built —
    which is what sets the routine's setpoints — and then discarded. ``run``
    raises rather than returning something plausible, so a test that forgets to
    supply data fails loudly instead of fitting a fabrication.
    """

    def run(
        self, schedule: _Schedule, timeout_s: float = DEFAULT_ROUTINE_TIMEOUT_S
    ) -> xr.Dataset:
        raise NotImplementedError(
            "StubBackend does not run schedules; supply the acquisition yourself, "
            "or use SimulatedBackend"
        )


class SimulatedBackend(RecordingBackend):
    """A backend that answers ``run`` from the simulator.

    What it is *not* is a schedule-level simulator. It reads a schedule for the
    sweep the routine encoded there — the frequencies of its
    ``SetClockFrequency``\\ s, the durations of its idles, the amplitudes on its
    ``Rxy``\\ s — and then asks the simulator for that experiment by name. So a
    routine that swept the wrong axis, or built the wrong number of
    acquisitions, fails here; a routine that composed the right sweep out of the
    wrong gates does not. Closing that last gap is RFC 0004 §11's outstanding
    item, and this is not it.

    What it does make possible is an end-to-end run: with ``run`` answered, a
    :class:`~qpi_driver.tuners.base.Tuner` can walk its whole DAG, fit, apply
    and persist without a scheduler or an instrument.

    ``rb`` is the exception to the description above, and reads the gates
    literally — see :meth:`_acquire_rb`.
    """

    def __init__(
        self,
        simulator: TransmonSimulator,
        *,
        gate_error: float = 0.001,
        coupled: CoupledTransmons | None = None,
    ) -> None:
        self.simulator = simulator
        #: Depolarising strength applied per gate in an RB sequence. Raising it
        #: is how a test drives the measured fidelity below a drift threshold.
        self.gate_error = gate_error
        #: The two-qubit register, for the edge routines. Separate from
        #: ``simulator`` because entanglement needs a joint state that one
        #: transmon cannot hold — see :mod:`qpi_driver.simulation.coupled`.
        self.coupled = coupled or CoupledTransmons()

    def run(
        self, schedule: _Schedule, timeout_s: float = DEFAULT_ROUTINE_TIMEOUT_S
    ) -> xr.Dataset:
        acquire = self._ACQUISITIONS.get(schedule.name)
        if acquire is None:
            raise NotImplementedError(
                f"the simulator has no physics for {schedule.name!r}. Disable it in "
                f"the calibration config, or add it here — do not let it return "
                f"data that means nothing. Simulated: "
                f"{', '.join(sorted(self._ACQUISITIONS))}"
            )
        values = np.asarray(acquire(self, schedule), dtype=float)
        return xr.Dataset({"y0": ("acq_index", values)})

    @staticmethod
    def _of_kind(schedule: _Schedule, kind: str) -> list[_Operation]:
        return [op for op in schedule.operations if op.kind == kind]

    def _idle_durations(self, schedule: _Schedule) -> list[float]:
        return [
            float(op.kwargs["duration"]) for op in self._of_kind(schedule, "IdlePulse")
        ]

    def _acquire_qubit_spectroscopy(self, schedule: _Schedule) -> np.ndarray:
        frequencies = [
            float(op.kwargs["clock_freq_new"])
            for op in self._of_kind(schedule, "SetClockFrequency")
        ]
        return self.simulator.qubit_spectroscopy(frequencies)

    def _acquire_rabi(self, schedule: _Schedule) -> np.ndarray:
        amplitudes = [
            float(op.kwargs["amp180"])
            for op in self._of_kind(schedule, "Rxy")
            if "amp180" in op.kwargs
        ]
        return self.simulator.rabi(amplitudes)

    def _acquire_t1(self, schedule: _Schedule) -> np.ndarray:
        return self.simulator.t1(self._idle_durations(schedule))

    def _acquire_t2_echo(self, schedule: _Schedule) -> np.ndarray:
        # The routine splits each delay in two around the refocusing pulse, so
        # the idles come in pairs and the delay is their sum.
        halves = self._idle_durations(schedule)
        return self.simulator.t2_echo(
            [first + second for first, second in zip(halves[::2], halves[1::2])]
        )

    def _acquire_ramsey(self, schedule: _Schedule) -> np.ndarray:
        delays = self._idle_durations(schedule)
        second_pulses = self._of_kind(schedule, "Rxy")[1::2]
        phases = [float(op.kwargs["phi"]) for op in second_pulses]
        return self.simulator.ramsey(delays, _detuning_of(delays, phases))

    def _acquire_rb(self, schedule: _Schedule) -> np.ndarray:
        """Play the schedule's own Clifford gates, with a known error on each.

        This one reads the gates rather than the sweep, because an RB schedule
        carries its sequences literally: the survival probability is computed
        from the rotations the routine emitted, so its Clifford decomposition
        and its recovery gate are under test and not merely assumed.

        Survival is the ground-state population, which is what RB's decay model
        is written in — a readout used for RB is calibrated to report it.
        """
        import qutip

        ground = qutip.basis(2, 0) * qutip.basis(2, 0).dag()
        mixed = qutip.qeye(2) / 2

        state = ground
        survival: list[float] = []
        for op in schedule.operations:
            if op.kind == "Reset":
                state = ground
            elif op.kind in ("Rxy", "X", "Y"):
                gate = _rxy_qobj(*_rotation_of(op))
                state = gate * state * gate.dag()
                # A depolarising channel is what a gate error looks like once
                # averaged over the Clifford group, which is the assumption RB
                # itself rests on.
                state = (1 - self.gate_error) * state + self.gate_error * mixed
            elif op.kind == "Measure":
                survival.append(float(np.real((state * ground).tr())))
        return self.simulator._measure(
            np.array(survival), averages=schedule.repetitions
        )

    def _acquire_cz_chevron(self, schedule: _Schedule) -> np.ndarray:
        """The flux sweep, read back off the schedule's square pulses.

        The routine emits one ``SquarePulse`` per grid point in row-major order,
        so the distinct amplitudes and durations recover the axes it swept — a
        routine that built the grid the other way round produces a transposed
        surface and fails here.
        """
        pulses = self._of_kind(schedule, "SquarePulse")
        if not pulses:
            raise NotImplementedError(
                "a cz_chevron schedule with no flux pulse cannot be simulated"
            )
        amplitudes = _ordered({float(op.kwargs["amp"]) for op in pulses})
        durations = _ordered({float(op.kwargs["duration"]) for op in pulses})
        return self.coupled.chevron(
            amplitudes, durations, averages=schedule.repetitions
        )

    def _acquire_conditional_phase(self, schedule: _Schedule) -> np.ndarray:
        """The swept phase of each second π/2, target down then up.

        The routine emits the ground-target fringe first and the excited-target
        fringe second, so the phases of the second ``Rxy`` in each pair — read in
        emission order — are exactly the sweep, twice over.
        """
        phases = [
            float(op.kwargs["phi"])
            for op in self._of_kind(schedule, "Rxy")
            if float(op.kwargs.get("theta", 0)) == 90
        ]
        # Each point contributes two Rxy — the first at phi=0, the second swept
        # — and the routine emits four fringes over one sweep, so one quarter of
        # the pairs is the sweep itself.
        quarter = len(phases) // 4
        swept = phases[1:quarter:2] if quarter else []
        if not swept:
            raise NotImplementedError(
                "a conditional_phase schedule with no swept second pulse cannot "
                "be simulated"
            )
        return self.coupled.conditional_phase(swept, averages=schedule.repetitions)

    #: Routine name to acquisition. A routine's schedule is named after it, so
    #: this is a lookup rather than a guess. Absent means unsimulated, and
    #: :meth:`run` refuses rather than inventing something.
    _ACQUISITIONS = {
        "qubit_spectroscopy": _acquire_qubit_spectroscopy,
        "rabi": _acquire_rabi,
        "t1": _acquire_t1,
        "t2_echo": _acquire_t2_echo,
        "ramsey": _acquire_ramsey,
        "rb": _acquire_rb,
        "cz_chevron": _acquire_cz_chevron,
        "conditional_phase": _acquire_conditional_phase,
    }


def _ordered(values: set[float]) -> list[float]:
    """A swept axis recovered from a schedule, in the order it was swept."""
    return sorted(values)


def _rotation_of(operation: _Operation) -> tuple[float, float]:
    """The ``(theta, phi)`` an operation rotates by, in degrees."""
    if operation.kind == "X":
        return 180.0, 0.0
    if operation.kind == "Y":
        return 180.0, 90.0
    return float(operation.kwargs["theta"]), float(operation.kwargs["phi"])


def _detuning_of(delays: list[float], phases: list[float]) -> float:
    """Recover the artificial detuning a Ramsey schedule encoded as a phase advance.

    The routine does not detune the clock; it advances the second π/2 by
    ``360·δ·t`` modulo a turn. The shortest non-zero delay therefore recovers δ
    exactly, since at nanoseconds and megahertz that phase has not yet wrapped.

    Reading it back rather than being told it is the point: the simulator then
    generates the fringe the schedule asks for, and the routine's own fit has to
    reconcile that with the detuning it *configured*. A schedule that advanced
    the phase by the wrong amount shows up as a residual detuning the qubit does
    not have.
    """
    candidates = [
        (delay, phase)
        for delay, phase in zip(delays, phases)
        if delay > 0 and phase > 0
    ]
    if not candidates:
        raise ValueError(
            "no delay carries a phase advance; this schedule encodes no detuning"
        )
    delay, phase = min(candidates)
    return phase / (360.0 * delay)


class SimulatedTuner(Tuner):
    """The real :class:`Tuner`, over the simulator instead of a scheduler.

    ``Tuner`` supplies everything a calibration is — the DAG walk, the retry-free
    error accounting, the write-back, the report — over exactly two things a
    subclass provides. Providing those two from the simulator makes all of it
    testable end to end, which is the only way to find the faults that live
    between the pieces rather than inside them.
    """

    def __init__(
        self,
        simulator: TransmonSimulator | None = None,
        *,
        qubits: tuple[str, ...] = ("q0",),
        edges: tuple[str, ...] = (),
        device_config_path: Path | str | None = None,
        gate_error: float = 0.001,
        coupled: CoupledTransmons | None = None,
        name: str = "simulated",
    ) -> None:
        super().__init__(name=name)
        self.simulator = simulator or TransmonSimulator()
        self.coupled = coupled or CoupledTransmons()
        self._backend = SimulatedBackend(
            self.simulator, gate_error=gate_error, coupled=self.coupled
        )
        self._device = device_for(self.simulator, *qubits, edges=edges)
        self._device_config_path = (
            Path(device_config_path) if device_config_path is not None else None
        )

    @property
    def backend(self) -> SimulatedBackend:
        return self._backend

    @property
    def device(self) -> FakeDevice:
        return self._device
