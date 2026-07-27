from typing import Any

from qpi_driver.executors.base import CircuitPayload as CircuitPayload
from qpi_driver.executors.base import Executor
from qpi_driver.executors.base import JobPayload as JobPayload
from qpi_driver.executors.mock import MockExecutor
from qpi_driver.executors.presto import PrestoExecutor
from qpi_driver.executors.qblox import QbloxExecutor
from qpi_driver.executors.qiskit_aer import QiskitAerExecutor
from qpi_driver.executors.quantify import QuantifyExecutor

BUILTIN_EXECUTORS: dict[str, type[Executor]] = {
    "mock": MockExecutor,
    "qiskit_aer": QiskitAerExecutor,
    "quantify": QuantifyExecutor,
    "qblox": QbloxExecutor,
    "presto": PrestoExecutor,
}


def resolve_executor(
    executor: str | type[Executor] | Executor,
    **kwargs: Any,
) -> Executor:
    """
    Resolve the executor argument to an Executor instance.

    Args:
        executor: Can be one of the following:
            - A string key corresponding to a built-in executor
            - A subclass of Executor, or an instance of one — which is how an
              executor the SDK does not ship gets used
        **kwargs: Additional keyword arguments to pass to the executor constructor
            if instantiation is needed

    Returns:
        An instance of Executor corresponding to the provided argument

    Raises:
        ValueError: If a string key is provided that does not match any registered executor
        TypeError: If the provided executor argument is of an invalid type
    """
    if isinstance(executor, Executor):
        return executor

    if isinstance(executor, type) and issubclass(executor, Executor):
        if "name" not in kwargs:
            kwargs["name"] = (
                executor.__name__.lower().replace("executor", "") or "executor"
            )
        return executor(**kwargs)

    if isinstance(executor, str):
        registry = BUILTIN_EXECUTORS
        cls = None
        try:
            cls = registry[executor]
        except KeyError as exp:
            raise ValueError(
                f"Unknown executor name '{executor}'. Registered executors: {list(registry.keys())}"
            ) from exp

        if "name" not in kwargs:
            kwargs["name"] = executor
        return cls(**kwargs)

    raise TypeError(
        f"Invalid executor type: {type(executor)}. "
        f"Must be a string, subclass of Executor, or an instance of Executor."
    )
