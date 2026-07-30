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


class ConfigError(ValueError):
    """``calibration.yml`` is not usable as written."""


@dataclass
class RoutineConfig:
    """One routine's switch and its sweep parameters.

    Parameters are read flat — ``rabi: {amp_range: ...}`` — because a nested
    ``params:`` key is a level of ceremony that buys nothing and that an
    operator writing the file by hand will forget.
    """

    enabled: bool = True
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
class CalibrationConfig:
    """The whole of ``calibration.yml``."""

    target_qubits: list[str] = field(default_factory=list)
    target_edges: list[str] = field(default_factory=list)
    routines: dict[str, RoutineConfig] = field(default_factory=dict)
    monitoring: MonitoringConfig = field(default_factory=MonitoringConfig)
    routine_timeout_s: float = DEFAULT_ROUTINE_TIMEOUT_S

    def is_enabled(self, routine_name: str) -> bool:
        """Whether *routine_name* should run. Absent means yes — see the module docstring."""
        routine = self.routines.get(routine_name)
        return routine.enabled if routine is not None else True

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
            params = {k: v for k, v in routine_data.items() if k != "enabled"}
            routines[name] = RoutineConfig(
                enabled=bool(routine_data.get("enabled", True)), params=params
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
        )

    @classmethod
    def from_yaml(cls, path: Path) -> "CalibrationConfig":
        with open(path, "r") as f:
            data = yaml.safe_load(f)
        if data is None:
            raise ConfigError(f"{path} is empty")
        return cls.from_dict(data)
