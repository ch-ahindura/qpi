import copy
import json
from pathlib import Path
from typing import Any

import yaml

from qpi_driver.compat.quantify import (
    BaseModel,
    Cluster,
    ClusterComponent,
    ClusterDescription,
    ClusterType,
    DeviceElement,
    Edge,
    ImportString,
    InstrumentCoordinator,
    InstrumentModule,
    InstrumentType,
    ParameterBase,
    QbloxHardwareCompilationConfig,
    QuantumDevice,
    field_validator,
)

_DEVICE_ELEMENT_TYPE_PROP = "element_type"


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
    def ensure_quantify(cls, value: Any):
        """Ensures that the import paths are for quantify-scheduler not qblox-scheduler"""
        if isinstance(value, str):
            value = value.replace("qblox_scheduler.", "quantify_scheduler.")
            value = value.replace(
                "qpi_driver.executors.qblox.", "qpi_driver.executors.quantify."
            )
        return value

    def instantiate(self) -> Any:
        """Instantiates the class"""
        return self.path(*self.args, **self.kwargs)


def load_quantify_hardware_config(
    data: QbloxHardwareCompilationConfig | Path | dict,
) -> QbloxHardwareCompilationConfig:
    """Load quantify hardware-layer config from the given data

    Args:
        data: the data in form of ``QbloxHardwareCompilationConfig`` or the path to the config file or the dict
            version from which ``QbloxHardwareCompilationConfig`` can be constructed.

    Returns:
        the parsed QbloxHardwareCompilationConfig

    Raises:
        ValidationError: if data or the file at 'data' is invalid QbloxHardwareCompilationConfig
    """
    if isinstance(data, Path):
        with open(data, "r") as file:
            if data.suffix == ".json":
                data = json.load(file)
            else:
                data = yaml.safe_load(file)

    return QbloxHardwareCompilationConfig.model_validate(data)


def load_quantum_device(name: str, config: Path | dict) -> QuantumDevice:
    """Load quantify device-layer config from the given data and returns a QuantumDevice

    Args:
        name: the name of the device
        config: the data in form of the path to the config file or the dict
            from which ``QuantumDevice`` can be constructed.

    Returns:
        the parsed QuantumDevice

    Raises:
        TypeError: if a device element's attribute is not callable
            yet it is configured like it is a parameter
        AttributeError: if a device element does not have the attribute
            yet it is being configured as if it has
    """
    if isinstance(config, Path):
        with open(config, "r") as file:
            config: dict = yaml.safe_load(file)
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
        path = (
            (element_data.get(_DEVICE_ELEMENT_TYPE_PROP) or {}).get("path") or ""
        ).lower()
        return "edge" in path or "coupler" in path

    ordered = sorted(config.items(), key=lambda item: _is_edge(item[1] or {}))

    for element_name, element_data in ordered:  # type: str, dict
        element_type = element_data.pop(_DEVICE_ELEMENT_TYPE_PROP, None)
        if not element_type:
            raise ValueError(
                f"Element '{element_name}' is missing a '{_DEVICE_ELEMENT_TYPE_PROP}' specification."
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
    stays the same one, so the compiler, the instrument coordinator and — under
    qblox — the hardware agent holding it need not be rebuilt, and no qcodes
    instrument name is taken twice.

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
            key: value
            for key, value in (data or {}).items()
            if key != _DEVICE_ELEMENT_TYPE_PROP
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


def _apply_parameters(obj: InstrumentModule | ParameterBase, data: dict | Any):
    """Helper to recursively map dictionaries onto QCoDeS submodules/parameters

    Args:
        obj: the instrument module or parameter
        data: the data to apply to this module or parameter

    Raises:
        TypeError: if obj is not callable yet data is not a dict
        AttributeError: if obj does not have the attribute set on it in the data
    """
    if not isinstance(data, dict):
        data = _to_num(data)
        try:
            obj(data)
        except TypeError as exp:
            raise TypeError(
                f"{obj} is not a Parameter yet value {data} passed is not a dict"
            ) from exp

    else:
        for key, value in data.items():
            try:
                attribute = getattr(obj, key)
            except AttributeError as exp:
                raise AttributeError(f"{obj} has no attribute '{key}'") from exp

            _apply_parameters(attribute, value)


def load_instrument_coordinator(
    name: str, hardware_config: QbloxHardwareCompilationConfig, is_dummy: bool = False
) -> InstrumentCoordinator:
    """Loads the instrument coordinator from the given hardware configuration

    Args:
        name: the name of the instrument coordinator
        hardware_config: the QbloxHardwareCompilationConfig of the setup.
        is_dummy: whether this setup is dummy or not.

    Returns:
        the parsed InstrumentCoordinator
    """
    coordinator = InstrumentCoordinator(name=name)
    hardware_description = hardware_config.hardware_description

    for instrument_name, cfg in hardware_description.items():
        if isinstance(cfg, ClusterDescription):
            cluster_ip = cfg.ip
            dummy_cfg = None
            if is_dummy:
                dummy_cfg = {
                    k: _to_cluster_type(v.instrument_type)
                    for k, v in cfg.modules.items()
                }

            cluster = Cluster(
                name=instrument_name, identifier=cluster_ip, dummy_cfg=dummy_cfg
            )
            cluster_component = ClusterComponent(cluster)
            coordinator.add_component(cluster_component)

    return coordinator


def _to_cluster_type(value: InstrumentType) -> ClusterType:
    """Converts the given InstrumentType to ClusterType
    Args:
        value: the InstrumentType to convert
    Returns:
        the corresponding ClusterType
    """
    return getattr(ClusterType, f"CLUSTER_{value}")
