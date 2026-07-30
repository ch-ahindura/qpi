"""Device configuration persistence utilities."""

import os
import tempfile
from pathlib import Path
from typing import Any

import yaml


def save_device_config(device: Any, path: Path) -> None:
    """Serialise QuantumDevice parameters to YAML atomically.

    Writes to a .tmp file first, then uses os.replace() for atomicity.
    The YAML format matches the quantify.device.example.yml schema.
    """
    config_dict = {}
    if hasattr(device, "generate_compilation_config"):
        # Generate quantify compilation config as a base if possible
        config_dict = device.generate_compilation_config().model_dump()
    else:
        # Fallback to manual serialization
        config_dict["name"] = device.name
        config_dict["elements"] = {}
        if hasattr(device, "elements"):
            for name, element in device.elements().items():
                config_dict["elements"][name] = serialize_device_element(element)
        config_dict["edges"] = {}
        if hasattr(device, "edges"):
            for name, edge in device.edges().items():
                config_dict["edges"][name] = serialize_edge(edge)

    # Write atomically
    dir_path = path.parent
    dir_path.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        "w", dir=dir_path, delete=False, suffix=".tmp"
    ) as tmp_file:
        yaml.dump(config_dict, tmp_file, default_flow_style=False)
        tmp_name = tmp_file.name

    os.replace(tmp_name, path)


def serialize_device_element(element: Any) -> dict:
    """Extract calibration parameters from a DeviceElement into a dict."""
    data = {}
    if hasattr(element, "parameters"):
        for param_name, param in element.parameters.items():
            try:
                val = param.get()
                if isinstance(val, (int, float, str, bool, list, dict)):
                    data[param_name] = val
                else:
                    data[param_name] = str(val)
            except Exception:
                pass
    return data


def serialize_edge(edge: Any) -> dict:
    """Extract calibration parameters from an Edge into a dict."""
    data = {}
    if hasattr(edge, "parameters"):
        for param_name, param in edge.parameters.items():
            try:
                val = param.get()
                if isinstance(val, (int, float, str, bool, list, dict)):
                    data[param_name] = val
                else:
                    data[param_name] = str(val)
            except Exception:
                pass
    return data
