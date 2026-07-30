"""Tuners: the calibration backends, and how ``--device`` resolves to one.

A tuner is to ``calibrate`` what an executor is to ``process`` — the one
swappable piece — and it is resolved the same three ways: by built-in name, by
class, or by instance. Resolution mirrors
:func:`~qpi_driver.executors.resolve_executor` deliberately; a tuner that had
its own conventions would be a second thing to learn for no gain.
"""

import importlib
from typing import Any

from qpi_driver.tuners.base import CalibrationConfig as CalibrationConfig
from qpi_driver.tuners.base import CalibrationReport as CalibrationReport
from qpi_driver.tuners.base import ConfigError as ConfigError
from qpi_driver.tuners.base import SchedulerBackend as SchedulerBackend
from qpi_driver.tuners.base import Tuner

#: Built-in tuners, imported lazily: each pulls in a scheduler that an install
#: without that extra does not have, so importing them eagerly would make this
#: module unimportable on a base install.
BUILTIN_TUNERS: dict[str, str] = {
    "quantify": "qpi_driver.tuners.quantify:QuantifyTuner",
    "qblox": "qpi_driver.tuners.qblox:QbloxTuner",
}

__all__ = [
    "Tuner",
    "SchedulerBackend",
    "CalibrationConfig",
    "CalibrationReport",
    "ConfigError",
    "BUILTIN_TUNERS",
    "resolve_tuner",
]


def resolve_tuner(tuner: str | type[Tuner] | Tuner, **kwargs: Any) -> Tuner:
    """Turn *tuner* into a :class:`Tuner` instance.

    Args:
        tuner: a built-in name, a :class:`Tuner` subclass, or an instance — the
            last being how a tuner the SDK does not ship gets used.
        **kwargs: passed to the constructor when one is needed.

    Raises:
        ValueError: if a name matches no built-in tuner.
        ImportError: if a built-in tuner's scheduler is not installed.
        TypeError: if *tuner* is none of the three accepted forms.
    """
    if isinstance(tuner, Tuner):
        return tuner

    if isinstance(tuner, type) and issubclass(tuner, Tuner):
        kwargs.setdefault(
            "name", tuner.__name__.lower().replace("tuner", "") or "tuner"
        )
        return tuner(**kwargs)

    if isinstance(tuner, str):
        try:
            import_path = BUILTIN_TUNERS[tuner]
        except KeyError as exc:
            raise ValueError(
                f"Unknown tuner name '{tuner}'. "
                f"Registered tuners: {sorted(BUILTIN_TUNERS)}"
            ) from exc

        kwargs.setdefault("name", tuner)
        module_path, class_name = import_path.split(":")
        try:
            module = importlib.import_module(module_path)
            cls = getattr(module, class_name)
        except (ImportError, AttributeError) as exc:
            raise ImportError(
                f"Failed to load tuner '{tuner}' from {import_path}: {exc}"
            ) from exc
        return cls(**kwargs)

    raise TypeError(
        f"Invalid tuner type: {type(tuner)}. "
        "Must be a string, subclass of Tuner, or an instance of Tuner."
    )
