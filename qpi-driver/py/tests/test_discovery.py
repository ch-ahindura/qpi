"""Devices that arrive from outside the SDK (RFC 0003 §6, §10).

Two routes, both of which run code the operator chose: a distribution advertising
devices through an entry point, and a device named by import path. What is asserted
here is as much about the failure modes as the happy paths — these routes are where
a third party's mistake could otherwise take down the CLI, and where a typo could
otherwise arrive as a traceback.
"""

from unittest.mock import patch

import pytest
from qpi_driver.builtins import registry
from qpi_driver.builtins.discovery import (
    ENTRY_POINT_GROUP,
    import_object,
    load_installed_devices,
    resolve_device,
)
from qpi_driver.builtins.registry import DeviceSpec, Operation
from qpi_driver.executors import Executor

# Something importable to point an import path at. `import_object` is given
# `tests.test_discovery:FakeExecutor` and has to find these.
FAKE_SUMMARY = "a fake"


class FakeExecutor(Executor):
    """An executor defined in a test module, i.e. one the SDK knows nothing about."""

    def __init__(self, name: str = "fake", **options):
        super().__init__(name=name)
        self.options = options

    def execute(self, payload):
        return {}

    def process_result(self, dataset, job_id):
        return {"job_id": job_id}


FAKE_MONITOR_SPEC = DeviceSpec(
    name="fake_monitor",
    operation=Operation.MONITOR,
    build=lambda **_: "a driver",
)

FAKE_PROCESS_SPEC = DeviceSpec(
    name="fake_process",
    operation=Operation.PROCESS,
    build=lambda **_: "a driver",
)


def fake_builder(**kwargs):
    """A monitor device builder: it returns a driver rather than running one."""
    return kwargs


NOT_A_DEVICE = "just a string"


@pytest.fixture
def empty_registry():
    """An empty device table, so a test's fakes cannot leak into another's."""
    with patch.dict(registry._DEVICES, {op: {} for op in Operation}, clear=True):
        yield


def _entry_point(value: str, name: str = "fake"):
    """A stand-in for an installed distribution's entry point.

    Building a real distribution per case would be slow and would test setuptools;
    what matters is what `load_installed_devices` does with what it loads.
    """

    class FakeEntryPoint:
        def __init__(self):
            self.name = name

        def load(self):
            return import_object(value)

    return FakeEntryPoint()


def _broken_entry_point(name: str = "broken", exc: Exception | None = None):
    class BrokenEntryPoint:
        def __init__(self):
            self.name = name

        def load(self):
            raise exc or ImportError("no module named 'vendor_sdk'")

    return BrokenEntryPoint()


def test_import_object_accepts_both_spellings():
    """`module:attr` and `module.attr` both resolve, per Pydantic's ImportString."""
    assert import_object("tests.test_discovery:FakeExecutor") is FakeExecutor
    assert import_object("tests.test_discovery.FakeExecutor") is FakeExecutor


def test_import_object_reports_a_missing_module_in_one_line():
    """A typo is an operator's mistake, so it gets a sentence, not a traceback.

    A traceback here would also carry filesystem paths into the journal
    (RFC 0003 §10).
    """
    with pytest.raises(ValueError) as excinfo:
        import_object("no_such_module:Thing")

    message = str(excinfo.value)
    assert message.startswith("could not import device 'no_such_module:Thing'")
    assert "\n" not in message


def test_import_object_keeps_filesystem_paths_out_of_the_message():
    """A half-installed device must not disclose where it lives (RFC 0003 §10).

    Python's ImportError names the file it was reading — useful in a traceback,
    but this message goes to an operator's journal, where the machine's layout is
    readable by anyone who can read logs.
    """
    with pytest.raises(ValueError) as excinfo:
        import_object("tests.fixtures.half_imported_device:Anything")

    message = str(excinfo.value)
    assert "ThisNameDoesNotExist" in message  # the useful half is kept
    assert ".py" not in message
    assert "/" not in message.replace(
        "tests.fixtures.half_imported_device:Anything", ""
    )


def test_import_object_reports_a_missing_attribute():
    with pytest.raises(ValueError, match="has no 'Nope'"):
        import_object("tests.test_discovery:Nope")


def test_import_object_rejects_something_that_is_not_a_path():
    with pytest.raises(ValueError, match="is not an import path"):
        import_object("mock")


def test_resolve_device_prefers_the_registry_for_a_plain_name():
    """A name without a dot or colon is looked up, never imported."""
    spec = resolve_device(Operation.PROCESS, "mock")

    assert spec is registry.resolve(Operation.PROCESS, "mock")


def test_resolve_device_still_reports_an_unknown_plain_name():
    """`mockk` is a typo, not an import path, and must not become an ImportError."""
    with pytest.raises(ValueError, match="unknown process device 'mockk'"):
        resolve_device(Operation.PROCESS, "mockk")


def test_resolve_device_wraps_an_imported_executor():
    """For `process`, being an Executor is being a device — no registration needed."""
    spec = resolve_device(Operation.PROCESS, "tests.test_discovery:FakeExecutor")

    assert spec.operation is Operation.PROCESS
    driver = spec.build(
        options=spec.parse_options({"probe_count": "4"}),
        qpi_addr="http://localhost:8090",
        token="t",
        ca_fingerprint="fp",
        ca_file_path="./bin/qpi.ca.pem",
        recv_timeout_ms=200,
    )

    assert driver.executor is FakeExecutor
    # An undeclared option reaches the executor as typed; nothing knows its type.
    assert driver.executor_options["probe_count"] == "4"


def test_an_imported_process_device_still_checks_its_declared_options():
    """The import route does not open a hole in the safe-path check (RFC 0003 §10)."""
    spec = resolve_device(Operation.PROCESS, "tests.test_discovery:FakeExecutor")

    with pytest.raises(ValueError, match="safe location"):
        spec.parse_options({"data_dir": "/var"})


def test_resolve_device_uses_an_imported_spec_directly():
    """A DeviceSpec is accepted by either operation — the route to a real schema."""
    spec = resolve_device(Operation.MONITOR, "tests.test_discovery:FAKE_MONITOR_SPEC")

    assert spec is FAKE_MONITOR_SPEC


def test_resolve_device_refuses_a_spec_of_another_operation():
    """A process device named to `monitor` is a mistake worth naming as one."""
    with pytest.raises(ValueError, match="is a process device, not a monitor one"):
        resolve_device(Operation.MONITOR, "tests.test_discovery:FAKE_PROCESS_SPEC")


def test_resolve_device_accepts_an_imported_monitor_builder():
    """For `monitor`, a device is the driver, so a builder is enough."""
    spec = resolve_device(Operation.MONITOR, "tests.test_discovery:fake_builder")

    assert spec.build is fake_builder
    assert spec.accepts_any_option is True
    # With no schema, every option passes through as the string it was typed as.
    assert spec.parse_options({"anything": "1"}) == {"anything": "1"}


def test_resolve_device_refuses_an_executor_for_monitor():
    """An executor cannot report readings; the operations are not interchangeable."""
    with pytest.raises(ValueError, match="not usable as a monitor device"):
        resolve_device(Operation.MONITOR, "tests.test_discovery:NOT_A_DEVICE")


def test_resolve_device_refuses_a_non_executor_for_process():
    with pytest.raises(ValueError, match="not usable as a process device"):
        resolve_device(Operation.PROCESS, "tests.test_discovery:NOT_A_DEVICE")


def test_installed_devices_join_the_registry(empty_registry):
    """An entry-point device is resolvable by `--device`, like any built-in."""
    with patch(
        "importlib.metadata.entry_points",
        return_value=[_entry_point("tests.test_discovery:FAKE_MONITOR_SPEC")],
    ) as entry_points:
        assert load_installed_devices() == ("fake_monitor",)

    entry_points.assert_called_once_with(group=ENTRY_POINT_GROUP)
    assert registry.resolve(Operation.MONITOR, "fake_monitor") is FAKE_MONITOR_SPEC


def test_installed_devices_may_ship_several(empty_registry):
    """An entry point resolving to an iterable registers each spec in it."""
    with patch(
        "importlib.metadata.entry_points",
        return_value=[_entry_point("tests.test_discovery:BOTH_SPECS")],
    ):
        assert set(load_installed_devices()) == {"fake_monitor", "fake_process"}


def test_a_broken_entry_point_is_skipped_not_fatal(empty_registry):
    """A third party's mistake must not stop the CLI or --help.

    The whole point of the entry-point route is that the SDK never learns about
    the device; the flip side is that it cannot vouch for it either.
    """
    with patch(
        "importlib.metadata.entry_points",
        return_value=[
            _broken_entry_point(),
            _entry_point("tests.test_discovery:FAKE_MONITOR_SPEC"),
        ],
    ):
        assert load_installed_devices() == ("fake_monitor",)

    assert registry.resolve(Operation.MONITOR, "fake_monitor") is FAKE_MONITOR_SPEC


def test_an_entry_point_resolving_to_a_non_spec_is_skipped(empty_registry):
    with patch(
        "importlib.metadata.entry_points",
        return_value=[_entry_point("tests.test_discovery:NOT_A_DEVICE")],
    ):
        assert load_installed_devices() == ()


def test_an_entry_point_reusing_a_name_is_skipped(empty_registry):
    """Two devices of one name would make the winner depend on install order."""
    registry.register(FAKE_MONITOR_SPEC)

    with patch(
        "importlib.metadata.entry_points",
        return_value=[_entry_point("tests.test_discovery:FAKE_MONITOR_SPEC")],
    ):
        assert load_installed_devices() == ()

    assert registry.resolve(Operation.MONITOR, "fake_monitor") is FAKE_MONITOR_SPEC


BOTH_SPECS = (FAKE_MONITOR_SPEC, FAKE_PROCESS_SPEC)
