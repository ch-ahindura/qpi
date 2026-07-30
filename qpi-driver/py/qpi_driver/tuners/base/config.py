from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class RoutineConfig:
    enabled: bool = True
    params: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.params.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.params[key]


@dataclass
class MonitoringConfig:
    rb_depths: list[int] = field(default_factory=lambda: [1, 10, 50])
    n_circuits_per_depth: int = 10
    shots: int = 512
    allxy_as_smoke_test: bool = True


@dataclass
class CalibrationConfig:
    target_qubits: list[str] = field(default_factory=list)
    target_edges: list[str] = field(default_factory=list)
    routines: dict[str, RoutineConfig] = field(default_factory=dict)
    monitoring: MonitoringConfig = field(default_factory=MonitoringConfig)

    def is_enabled(self, routine_name: str) -> bool:
        routine = self.routines.get(routine_name)
        return routine.enabled if routine else False

    def get_routine(self, name: str) -> RoutineConfig:
        return self.routines.get(name, RoutineConfig(enabled=False))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CalibrationConfig":
        routines = {}
        for name, routine_data in data.get("routines", {}).items():
            routines[name] = RoutineConfig(
                enabled=routine_data.get("enabled", True),
                params=routine_data.get("params", {}),
            )

        monitoring_data = data.get("monitoring", {})
        monitoring = MonitoringConfig(
            rb_depths=monitoring_data.get("rb_depths", [1, 10, 50]),
            n_circuits_per_depth=monitoring_data.get("n_circuits_per_depth", 10),
            shots=monitoring_data.get("shots", 512),
            allxy_as_smoke_test=monitoring_data.get("allxy_as_smoke_test", True),
        )

        return cls(
            target_qubits=data.get("target_qubits", []),
            target_edges=data.get("target_edges", []),
            routines=routines,
            monitoring=monitoring,
        )

    @classmethod
    def from_yaml(cls, path: Path) -> "CalibrationConfig":
        with open(path, "r") as f:
            data = yaml.safe_load(f)
        return cls.from_dict(data)
