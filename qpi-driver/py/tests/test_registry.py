"""The operation/device registry (RFC 0003 §6, §9)."""

from unittest.mock import patch

import pytest
from qpi_driver.builtins import registry
from qpi_driver.builtins.registry import (
    DeviceSpec,
    Operation,
    devices,
    register,
    resolve,
)


@pytest.fixture
def empty_registry():
    """Swap in an empty device table so a test's fakes cannot leak.

    The registry is process-global and the built-in devices register themselves
    at import, so a test that registered into the real table would change what
    every later test sees.
    """
    with patch.dict(registry._DEVICES, {op: {} for op in Operation}, clear=True):
        yield


def _spec(name: str, operation: Operation = Operation.PROCESS) -> DeviceSpec:
    """A minimal spec; the builder is never called in these tests."""
    return DeviceSpec(name=name, operation=operation, build=lambda **_: None)


def test_operations_are_a_closed_set():
    """The operations are exactly the ones QPI-UI has handlers for.

    Devices are the extensible half; operations are not, so asserting the whole
    set is the point here rather than a brittleness (RFC 0003 §13.1). Adding one
    is meant to fail here, and in the Go and TypeScript SDKs' equivalents, until
    every side of the contract has been updated together (RFC 0004 §6.1).
    """
    assert [op.value for op in Operation] == ["process", "monitor", "calibrate"]


def test_a_device_is_a_name_an_operation_and_a_builder():
    """The whole of a spec, so a field creeping back in fails here.

    A device that also described itself — a summary, a declared option schema, an
    install target — would be a second catalog beside QPI-UI's, kept in step by
    hand (RFC 0003 §9).
    """
    assert {field for field in DeviceSpec.__dataclass_fields__} == {
        "name",
        "operation",
        "build",
    }


def test_builtin_devices_are_registered():
    """Importing the package makes every built-in device resolvable."""
    process = {spec.name for spec in devices(Operation.PROCESS)}
    assert process == {"mock", "qiskit_aer", "quantify", "qblox", "presto"}
    assert {spec.name for spec in devices(Operation.MONITOR)} == {"bluefors_gen1"}

    for operation in Operation:
        for spec in devices(operation):
            assert spec.operation is operation
            assert resolve(operation, spec.name) is spec


def test_devices_are_sorted_by_name(empty_registry):
    """Order is by name, not by import, so `qpi-driver devices` is stable."""
    for name in ("zeta", "alpha", "mu"):
        register(_spec(name))

    assert [spec.name for spec in devices(Operation.PROCESS)] == [
        "alpha",
        "mu",
        "zeta",
    ]


def test_register_rejects_a_duplicate_name(empty_registry):
    """Two devices of one name would make the winner depend on import order."""
    register(_spec("mine"))

    with pytest.raises(ValueError, match="already registered"):
        register(_spec("mine"))


def test_register_scopes_names_per_operation(empty_registry):
    """A name is unique within its operation, not globally.

    ``--device`` is always read in the context of an operation, so nothing needs
    to stop a process and a monitor device from sharing a name.
    """
    register(_spec("shared", Operation.PROCESS))
    register(_spec("shared", Operation.MONITOR))

    assert resolve(Operation.PROCESS, "shared").operation is Operation.PROCESS
    assert resolve(Operation.MONITOR, "shared").operation is Operation.MONITOR


def test_resolve_reports_the_known_devices(empty_registry):
    """An unknown device names the alternatives, sorted, so the CLI can print it."""
    register(_spec("mu"))
    register(_spec("alpha"))

    with pytest.raises(ValueError) as excinfo:
        resolve(Operation.PROCESS, "mockk")

    message = str(excinfo.value)
    assert "unknown process device 'mockk'" in message
    assert "Known devices: alpha, mu." in message


def test_resolve_says_none_when_the_sdk_ships_no_such_device(empty_registry):
    """An SDK with no device for an operation must say so, not print an empty list."""
    with pytest.raises(ValueError, match="Known devices: none."):
        resolve(Operation.PROCESS, "mock")


def test_resolve_looks_only_within_its_operation(empty_registry):
    """A device of another operation is not found, and is not offered as an option."""
    register(_spec("bluefors_gen1", Operation.MONITOR))

    with pytest.raises(ValueError, match="unknown process device"):
        resolve(Operation.PROCESS, "bluefors_gen1")


def test_specs_are_immutable():
    """Specs are data; a caller cannot retune a device by mutating what it read."""
    spec = resolve(Operation.MONITOR, "bluefors_gen1")

    with pytest.raises(AttributeError):
        spec.name = "other"
