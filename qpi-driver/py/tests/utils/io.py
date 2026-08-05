"""Loading fixture data files from tests/fixtures."""

import json
from pathlib import Path
from typing import Any

import yaml

_FIXTURES_PATH = Path(__file__).resolve().parent.parent / "fixtures"


def load_json_fixture(fixture_relative_path: str) -> Any:
    """Load a JSON fixture from the tests/fixtures directory."""
    full_path = _FIXTURES_PATH / fixture_relative_path
    with open(full_path, "r") as f:
        return json.load(f)


def load_yaml_fixture(fixture_relative_path: str) -> Any:
    """Load a YAML fixture from the tests/fixtures directory."""
    full_path = _FIXTURES_PATH / fixture_relative_path
    with open(full_path, "r") as f:
        return yaml.safe_load(f)
