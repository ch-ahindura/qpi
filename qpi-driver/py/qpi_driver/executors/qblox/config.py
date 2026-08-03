import copy
import json
from pathlib import Path
from typing import Any

import yaml

from qpi_driver.compat.qblox import (
    BaseModel,
    DeviceElement,
    Edge,
    ImportString,
    QbloxHardwareCompilationConfig,
    QuantumDevice,
    field_validator,
)


class _ElementType(BaseModel):
    """The config for each element defined in the device-layer config"""

    path: ImportString
    args: tuple = ()
    kwargs: dict = {}

    @field_validator("args", mode="before")
    @classmethod
    def conv_none_to_empty_tuple(cls, value: Any):
        """Ensures None values become empty tuple"""
        if value is None:
            return ()
        return value

    @field_validator("kwargs", mode="before")
    @classmethod
    def conv_none_to_empty_dict(cls, value: Any):
        """Ensures None values become empty tuple"""
        if value is None:
            return {}
        return value

    @field_validator("path", mode="before")
    @classmethod
    def ensure_qblox(cls, value: Any):
        """Ensures that the import paths are for qblox-scheduler not quantify-scheduler"""
        if isinstance(value, str):
            value = value.replace("quantify_scheduler.", "qblox_scheduler.")
            value = value.replace(
                "qpi_driver.executors.quantify.", "qpi_driver.executors.qblox."
            )
        return value

    def instantiate(self) -> Any:
        """Instantiates the class"""
        return self.path(*self.args, **self.kwargs)


def load_quantify_hardware_config(
    data: QbloxHardwareCompilationConfig | Path | dict,
) -> QbloxHardwareCompilationConfig:
    """Load quantify hardware-layer config from the given data and convert it for qblox-scheduler."""
    if isinstance(data, Path):
        with open(data, "r") as file:
            if data.suffix == ".json":
                data = json.load(file)
            else:
                data = yaml.safe_load(file)
    else:
        data = copy.deepcopy(data)

    if isinstance(data, dict):
        if "quantify_scheduler" in data.get("config_type", ""):
            data["config_type"] = "QbloxHardwareCompilationConfig"
        return QbloxHardwareCompilationConfig.model_validate(data)

    return data


def load_quantum_device(name: str, config: Path | dict) -> QuantumDevice:
    """Load quantify device-layer config from the given data and returns a QuantumDevice"""
    if isinstance(config, Path):
        with open(config, "r") as file:
            config = yaml.safe_load(file)
    else:
        config = copy.deepcopy(config)

    quantum_device = QuantumDevice(name=name)

    # Elements before edges, regardless of the order the file lists them in. An
    # edge names the two qubits it joins and the device rejects it if either is
    # not there yet, so a single pass in file order means a config is only
    # loadable if its author happened to put every coupler after both its
    # qubits — and anything that rewrites the YAML alphabetically (which is
    # `yaml.safe_dump`'s default) silently makes it unloadable.
    def _is_edge(element_data: dict) -> bool:
        path = ((element_data.get("element_type") or {}).get("path") or "").lower()
        return "edge" in path or "coupler" in path

    ordered = sorted(config.items(), key=lambda item: _is_edge(item[1] or {}))

    for element_name, element_data in ordered:  # type: str, dict
        element_type = element_data.pop("element_type", None)
        if not element_type:
            raise ValueError(
                f"Element '{element_name}' is missing a 'element_type' specification."
            )

        element_type_conf = _ElementType.model_validate(element_type)

        try:
            element_instance = element_type_conf.instantiate()
            _apply_parameters(element_instance, element_data)
            if isinstance(element_instance, DeviceElement):
                quantum_device.add_element(element_instance)
            elif isinstance(element_instance, Edge):
                quantum_device.add_edge(element_instance)
            else:
                raise TypeError(
                    f"Element '{element_name}' is has an unsupported type {type(element_instance)}."
                )
        except Exception as exp:
            raise ValueError(
                f"Failed to add element '{element_name}' from <{element_type_conf}> to quantum device, {exp}"
            ) from exp

    return quantum_device


def apply_device_config(device: QuantumDevice, config: Path | dict) -> list[str]:
    """Write *config*'s parameters onto a device that is already built.

    Reloading a calibration in place rather than rebuilding: the device object
    stays the same one, so the `HardwareAgent` holding it need not be rebuilt —
    which would mean re-establishing the cluster connection — and `quantum_device`
    is a read-only property, so there is no way to hand it a new one.

    Only parameters are applied. A config naming an element the device does not
    have is structural, which calibration never writes (RFC 0004 §10), and the
    names are returned rather than raised so the caller can log and carry on with
    what it did apply.
    """
    if isinstance(config, Path):
        with open(config, "r") as file:
            config = yaml.safe_load(file)
    else:
        config = copy.deepcopy(config)

    unknown: list[str] = []
    for name, data in (config or {}).items():
        component = _component_by_name(device, name)
        if component is None:
            unknown.append(name)
            continue
        parameters = {
            key: value for key, value in (data or {}).items() if key != "element_type"
        }
        _apply_parameters(component, parameters)
    return unknown


def _component_by_name(device: QuantumDevice, name: str) -> Any:
    for accessor in (device.get_element, device.get_edge):
        try:
            return accessor(name)
        except Exception:  # noqa: BLE001 - not this kind of component
            continue
    return None


def _to_num(value: Any) -> Any:
    """Convert a string value to float or int if it represents a number."""
    if isinstance(value, str):
        try:
            val = float(value)
            return int(val) if val.is_integer() else val
        except ValueError:
            pass
    return value


def _apply_parameters(obj: Any, data: dict):
    """Helper to recursively map dictionaries onto submodules/parameters/attributes"""
    for key, value in data.items():
        lookup_key = (
            "beta"
            if key == "motzoi" and not hasattr(obj, "motzoi") and hasattr(obj, "beta")
            else key
        )
        try:
            attribute = getattr(obj, lookup_key)
        except AttributeError as exp:
            raise AttributeError(f"{obj} has no attribute '{key}'") from exp

        if isinstance(value, dict):
            _apply_parameters(attribute, value)
        else:
            value = _to_num(value)
            if hasattr(attribute, "__call__") and not isinstance(
                attribute, (int, float, str, bool)
            ):
                try:
                    attribute(value)
                except TypeError:
                    setattr(obj, lookup_key, value)
            else:
                setattr(obj, lookup_key, value)
