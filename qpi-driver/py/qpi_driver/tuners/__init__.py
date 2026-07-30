import importlib
from typing import Any

from qpi_driver.tuners.base import CalibrationConfig as CalibrationConfig
from qpi_driver.tuners.base import CalibrationReport as CalibrationReport
from qpi_driver.tuners.base import Tuner

BUILTIN_TUNERS: dict[str, str] = {
    "quantify": "qpi_driver.tuners.quantify:QuantifyTuner",
    "qblox": "qpi_driver.tuners.qblox:QbloxTuner",
}


def resolve_tuner(
    tuner: str | type[Tuner] | Tuner,
    **kwargs: Any,
) -> Tuner:
    """
    Resolve the tuner argument to a Tuner instance.

    Args:
        tuner: Can be one of the following:
            - A string key corresponding to a built-in tuner
            - A subclass of Tuner, or an instance of one
        **kwargs: Additional keyword arguments to pass to the tuner constructor
            if instantiation is needed

    Returns:
        An instance of Tuner corresponding to the provided argument

    Raises:
        ValueError: If a string key is provided that does not match any registered tuner
        TypeError: If the provided tuner argument is of an invalid type
    """
    if isinstance(tuner, Tuner):
        return tuner

    if isinstance(tuner, type) and issubclass(tuner, Tuner):
        if "name" not in kwargs:
            kwargs["name"] = tuner.__name__.lower().replace("tuner", "") or "tuner"
        return tuner(**kwargs)

    if isinstance(tuner, str):
        registry = BUILTIN_TUNERS
        import_path = None
        try:
            import_path = registry[tuner]
        except KeyError as exp:
            raise ValueError(
                f"Unknown tuner name '{tuner}'. Registered tuners: {list(registry.keys())}"
            ) from exp

        if "name" not in kwargs:
            kwargs["name"] = tuner

        module_path, class_name = import_path.split(":")
        try:
            module = importlib.import_module(module_path)
            cls = getattr(module, class_name)
        except (ImportError, AttributeError) as e:
            raise ImportError(f"Failed to load tuner '{tuner}' from {import_path}: {e}")

        return cls(**kwargs)

    raise TypeError(
        f"Invalid tuner type: {type(tuner)}. "
        f"Must be a string, subclass of Tuner, or an instance of Tuner."
    )
