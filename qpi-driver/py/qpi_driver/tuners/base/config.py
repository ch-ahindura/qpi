"""What ``calibration.yml`` says: which routines run, over what, and how far (RFC 0004 §6.6).

Two rules govern the defaults here, both learned the hard way:

A routine absent from the file is **enabled**, not disabled. The opposite makes
an empty or missing file mean "calibrate nothing", and since a run that does
nothing raises nothing, it reports success — the worst answer a calibration can
give. Turning a routine off is now something the file has to say out loud.

An unknown routine name is an **error**. A typo like ``rabi_oscillations`` for
``rabi`` would otherwise enable a routine that does not exist while leaving the
real one at its default, and nothing in the run would mention it.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

#: Wall-clock ceiling for a single routine on a single target. The `process`
#: operation's `job_timeout` has no equivalent here, and a routine that hangs on
#: an instrument would otherwise hang the worker for the life of the driver.
DEFAULT_ROUTINE_TIMEOUT_S = 900.0

#: Hops that must separate two qubits measured at once. Two excludes adjacent pairs,
#: which leaves an idle qubit between every pair in a group — conservative against what
#: the simultaneous-benchmarking literature does routinely, and the right default until
#: a chip has measured its own penalty (RFC 0009 §5.6).
DEFAULT_QUBIT_SPACING = 2

#: The same for couplers, where one asks only that two of them share no qubit.
DEFAULT_EDGE_SPACING = 1

#: The most targets in one group. A ceiling on the sequencers one schedule can ask
#: for, before the per-output checks in `grouping.readout_misfit` narrow it further.
DEFAULT_MAX_GROUP = 8


class ConfigError(ValueError):
    """``calibration.yml`` is not usable as written."""


@dataclass
class RoutineConfig:
    """One routine's switch and its sweep parameters.

    Parameters are read flat — ``rabi: {amp_range: ...}`` — because a nested
    ``params:`` key is a level of ceremony that buys nothing and that an
    operator writing the file by hand will forget.

    ``timeout_s`` is a field rather than one of those parameters so that a typo in it is a
    startup error instead of a silently ignored key, and so it cannot be mistaken for a
    sweep axis by anything that widens one.
    """

    enabled: bool = True
    #: Wall-clock ceiling for this routine alone, overriding the walk's
    #: `CalibrationConfig.routine_timeout_s`. ``None`` inherits it.
    #:
    #: One global number has to be set for the slowest node, which makes it no ceiling at all
    #: for the fast ones: a spectroscopy that legitimately sweeps for minutes and a Rabi that
    #: should take seconds cannot share a limit that catches a hang in either.
    timeout_s: float | None = None
    params: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.params.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.params[key]

    def __contains__(self, key: str) -> bool:
        return key in self.params


@dataclass
class MonitoringConfig:
    """How the periodic drift check benchmarks, when one is scheduled."""

    rb_depths: list[int] = field(default_factory=lambda: [1, 10, 50])
    n_circuits_per_depth: int = 10
    shots: int = 512
    allxy_as_smoke_test: bool = True


@dataclass
class ParallelConfig:
    """Which targets a walk may measure at once (RFC 0009 §5.3).

    Off unless the file says otherwise, which is the opposite of `RoutineConfig`'s
    default and deliberately so: a missing `routines` entry cannot make a run measure
    nothing, whereas defaulting this to on would silently change how every existing
    chip calibrates.
    """

    enabled: bool = False
    qubit_spacing: int = DEFAULT_QUBIT_SPACING
    edge_spacing: int = DEFAULT_EDGE_SPACING
    max_group: int = DEFAULT_MAX_GROUP
    #: Pairs never grouped, whatever the spacing allows.
    exclude: list[list[str]] = field(default_factory=list)
    #: Explicit classes per kind, which skip the colouring entirely. For a chip whose
    #: measured crosstalk does not follow its topology.
    groups: dict[str, list[list[str]]] = field(default_factory=dict)
    #: Benchmark each target alone as well as in company, and report the difference
    #: (RFC 0009 §5.6). Off by default because it doubles what the benchmarks cost: it is
    #: the measurement that licenses a tighter `qubit_spacing`, not something every run
    #: needs. Without it the spacing is a guess, which is why the default spacing is the
    #: conservative one.
    measure_penalty: bool = False

    def spacing_for(self, kind: str) -> int:
        """The radius that applies to *kind* — ``"qubits"`` or ``"edges"``."""
        return self.edge_spacing if kind == "edges" else self.qubit_spacing

    @classmethod
    def from_dict(cls, data: Any) -> "ParallelConfig":
        if not isinstance(data, dict):
            raise ConfigError(f"'parallel' must be a mapping, got {type(data)}")

        # Checked before any falsy coalescing, or `groups: []` reads as "none given"
        # rather than as the mistake it is.
        exclude = data.get("exclude")
        if exclude is not None and not isinstance(exclude, (list, tuple)):
            raise ConfigError("parallel.exclude must be a list of target pairs")
        for pair in exclude or []:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise ConfigError(
                    f"parallel.exclude takes pairs of target names, got {pair!r}"
                )
        groups = data.get("groups")
        if groups is not None and not isinstance(groups, dict):
            raise ConfigError("parallel.groups must be a mapping of kind to groups")
        groups = groups or {}
        unknown = sorted(set(groups) - {"qubits", "edges"})
        if unknown:
            raise ConfigError(
                f"parallel.groups knows 'qubits' and 'edges', not {', '.join(unknown)}"
            )

        return cls(
            enabled=bool(data.get("enabled", False)),
            qubit_spacing=_positive(data, "qubit_spacing", DEFAULT_QUBIT_SPACING),
            edge_spacing=_positive(data, "edge_spacing", DEFAULT_EDGE_SPACING),
            max_group=_positive(data, "max_group", DEFAULT_MAX_GROUP),
            exclude=[list(pair) for pair in exclude or []],
            groups={kind: [list(g) for g in gs] for kind, gs in groups.items()},
            measure_penalty=bool(data.get("measure_penalty", False)),
        )


@dataclass
class CalibrationConfig:
    """The whole of ``calibration.yml``."""

    target_qubits: list[str] = field(default_factory=list)
    target_edges: list[str] = field(default_factory=list)
    routines: dict[str, RoutineConfig] = field(default_factory=dict)
    monitoring: MonitoringConfig = field(default_factory=MonitoringConfig)
    routine_timeout_s: float = DEFAULT_ROUTINE_TIMEOUT_S
    parallel: ParallelConfig = field(default_factory=ParallelConfig)

    def is_enabled(self, routine_name: str) -> bool:
        """Whether *routine_name* should run. Absent means yes — see the module docstring."""
        routine = self.routines.get(routine_name)
        return routine.enabled if routine is not None else True

    def timeout_for(self, name: str) -> float:
        """The wall-clock ceiling routine *name* runs under.

        Its own ``timeout_s`` if it names one, else the walk's. Read through here rather
        than off `routine_timeout_s` directly so that every place enforcing a ceiling — the
        acquisition, the check schedules, and the over-budget refusal that reports it —
        agrees about which number applied.
        """
        routine = self.routines.get(name)
        if routine is not None and routine.timeout_s is not None:
            return routine.timeout_s
        return self.routine_timeout_s

    def get_routine(self, name: str) -> RoutineConfig:
        """*name*'s configuration, or an enabled one with default parameters."""
        return self.routines.get(name, RoutineConfig())

    def validate_against(self, known_routines: set[str]) -> None:
        """Reject routine names no routine answers to.

        Raises:
            ConfigError: naming the unknown entries and what was available, so a
                typo is a startup error rather than a routine that never runs.
        """
        unknown = sorted(set(self.routines) - known_routines)
        if unknown:
            raise ConfigError(
                f"calibration.yml configures unknown routine(s): {', '.join(unknown)}. "
                f"Known routines: {', '.join(sorted(known_routines))}."
            )

    def validate_targets(self) -> None:
        """Reject an edge whose qubits are not themselves being calibrated.

        A two-qubit gate is measured *through* its qubits: `cz_chevron` prepares
        ``|11>`` with a pi pulse on each and reads one of them back. Neither is
        meaningful against a qubit whose own frequency and pi pulse are whatever the
        config file happened to say — the chevron would fit a real curve to a pair
        that was never brought up, write a plausible amplitude and duration, and the
        gate would not work.

        Failing at startup rather than at that point is the whole value. The wrong
        answer here is not an exception, it is a calibrated-looking edge, and the only
        moment it is cheap to catch is before anything runs.

        Raises:
            ConfigError: naming the edge and the qubits it needs.
        """
        targeted = set(self.target_qubits)
        missing: list[str] = []
        for edge in self.target_edges:
            parts = edge.split("_")
            if len(parts) != 2 or not all(parts):
                raise ConfigError(
                    f"target edge {edge!r} is not a '<parent>_<child>' pair, so the "
                    f"qubits it acts on cannot be determined"
                )
            absent = [qubit for qubit in parts if qubit not in targeted]
            if absent:
                missing.append(f"{edge} needs {', '.join(absent)}")
        if missing:
            raise ConfigError(
                "calibration.yml targets edges whose qubits it does not calibrate: "
                + "; ".join(missing)
                + ". A two-qubit gate is measured through its qubits, so an edge over "
                "an uncalibrated one produces a confident, wrong answer rather than "
                "an error."
            )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CalibrationConfig":
        if not isinstance(data, dict):
            raise ConfigError(f"calibration config must be a mapping, got {type(data)}")

        routines: dict[str, RoutineConfig] = {}
        raw_routines = data.get("routines") or {}
        if not isinstance(raw_routines, dict):
            raise ConfigError("'routines' must be a mapping of name to settings")

        for name, routine_data in raw_routines.items():
            if routine_data is None:
                routines[name] = RoutineConfig()
                continue
            if not isinstance(routine_data, dict):
                raise ConfigError(
                    f"routine {name!r} must be a mapping of settings, got {type(routine_data)}"
                )
            params = {
                k: v
                for k, v in routine_data.items()
                if k not in ("enabled", "timeout_s")
            }
            routines[name] = RoutineConfig(
                enabled=bool(routine_data.get("enabled", True)),
                timeout_s=_routine_timeout(name, routine_data),
                params=params,
            )

        monitoring_data = data.get("monitoring") or {}
        monitoring = MonitoringConfig(
            rb_depths=monitoring_data.get("rb_depths", [1, 10, 50]),
            n_circuits_per_depth=monitoring_data.get("n_circuits_per_depth", 10),
            shots=monitoring_data.get("shots", 512),
            allxy_as_smoke_test=monitoring_data.get("allxy_as_smoke_test", True),
        )

        return cls(
            target_qubits=list(data.get("target_qubits") or []),
            target_edges=list(data.get("target_edges") or []),
            routines=routines,
            monitoring=monitoring,
            routine_timeout_s=float(
                data.get("routine_timeout_s", DEFAULT_ROUTINE_TIMEOUT_S)
            ),
            parallel=ParallelConfig.from_dict(data.get("parallel") or {}),
        )

    @classmethod
    def from_yaml(cls, path: Path) -> "CalibrationConfig":
        with open(path, "r") as f:
            data = yaml.safe_load(f)
        if data is None:
            raise ConfigError(f"{path} is empty")
        return cls.from_dict(data)


def _positive(data: dict[str, Any], key: str, default: int) -> int:
    """*key* as a whole number of at least one, so a typo is a startup error."""
    if key not in data:
        return default
    try:
        value = int(data[key])
    except (TypeError, ValueError):
        raise ConfigError(
            f"parallel.{key} must be a whole number, got {data[key]!r}"
        ) from None
    if value < 1:
        raise ConfigError(f"parallel.{key} must be at least 1, got {value}")
    return value


def _routine_timeout(name: str, routine_data: dict[str, Any]) -> float | None:
    """A routine's own ``timeout_s``, validated, or ``None`` to inherit the walk's."""
    if "timeout_s" not in routine_data:
        return None
    value = routine_data["timeout_s"]
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise ConfigError(
            f"routine {name!r}: timeout_s must be a number of seconds, got {value!r}"
        ) from None
    if seconds <= 0 or seconds != seconds:
        raise ConfigError(
            f"routine {name!r}: timeout_s must be positive, got {seconds}"
        )
    return seconds
