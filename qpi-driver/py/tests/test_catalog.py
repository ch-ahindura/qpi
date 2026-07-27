"""Rendering the device catalog for help, humans and programs (RFC 0003 §5, §9).

``catalog --json`` is a contract Go, JS and QPI-UI all read, so its shape is
asserted here rather than left to whatever the code happens to emit.
"""

from qpi_driver.builtins import Operation, devices
from qpi_driver.builtins.catalog import (
    SCHEMA_VERSION,
    catalog_dict,
    device_lines,
    missing_for_extras,
    missing_requirements,
    render_catalog,
)

# The shape `pyproject.toml`'s extras take once packaged, including an extra
# declared as an extra of this same distribution — how `qiskit_aer` is written.
# Deliberately not the real extra names: which of those are installed depends on
# which `make test-py-*` target is running, and this resolution is the same
# either way.
REQUIREMENTS = [
    'numpy>=2.0.0; extra == "installed"',  # a base dependency, present everywhere
    'no-such-distribution; extra == "vendor"',
    'also-absent<2; extra == "vendor"',
    'qpi-driver[vendor]; extra == "wrapper"',
    "numpy>=2.0.0",  # not tied to an extra, so no extra pulls it in
]


def test_catalog_shape_is_the_documented_one():
    """Every key `catalog_dict`'s docstring promises is present, and no other.

    Consumers in three languages read this document, so the shape is the thing
    under test — a renamed key is a break, not a detail.
    """
    document = catalog_dict()

    assert set(document) == {"schema_version", "operations"}
    assert document["schema_version"] == SCHEMA_VERSION

    for operation in document["operations"]:
        assert set(operation) == {
            "name",
            "summary",
            "default_device",
            "events",
            "devices",
        }
        for device in operation["devices"]:
            assert set(device) == {"name", "summary", "extra", "options"}
            for option in device["options"]:
                assert set(option) == {
                    "key",
                    "help",
                    "type",
                    "default",
                    "required",
                    "example",
                }


def test_catalog_covers_every_registered_device():
    """The document is generated from the registry, so it cannot fall behind it."""
    document = catalog_dict()
    listed = {
        entry["name"]: {device["name"] for device in entry["devices"]}
        for entry in document["operations"]
    }

    for operation in Operation:
        assert listed[operation.value] == {spec.name for spec in devices(operation)}


def test_catalog_is_json_serialisable_and_stable():
    """Two runs produce byte-identical output, so a drift check can diff it."""
    import json

    first = json.dumps(catalog_dict(), indent=2)
    second = json.dumps(catalog_dict(), indent=2)

    assert first == second
    # Devices in a stable, name-sorted order rather than import order.
    process = next(
        entry for entry in catalog_dict()["operations"] if entry["name"] == "process"
    )
    names = [device["name"] for device in process["devices"]]
    assert names == sorted(names)


def test_catalog_reports_option_types_and_defaults():
    """An option's type, default and requiredness travel with it."""
    process = next(
        entry for entry in catalog_dict()["operations"] if entry["name"] == "process"
    )
    options = {
        option["key"]: option
        for option in next(
            device for device in process["devices"] if device["name"] == "mock"
        )["options"]
    }

    assert options["data_dir"]["type"] == "safe_dir"
    assert options["data_dir"]["default"] == "./bin/data"
    assert options["job_timeout"]["type"] == "int"
    assert options["is_dummy"]["type"] == "bool"
    assert options["quantify_device_config"]["type"] == "Path"
    assert all(option["required"] is False for option in options.values())

    monitor = next(
        entry for entry in catalog_dict()["operations"] if entry["name"] == "monitor"
    )
    channels = next(
        option
        for option in monitor["devices"][0]["options"]
        if option["key"] == "channels"
    )
    assert channels["required"] is True
    assert channels["type"] == "channels"
    assert channels["default"] is None


def test_device_lines_share_one_options_block():
    """Devices with the same schema are not made to repeat it.

    Every process device is the same driver over a different executor, so five
    copies of five options would be noise in `--help`, not information.
    """
    lines = device_lines(Operation.PROCESS)

    headers = [line for line in lines if line.startswith("Options")]
    assert headers == ["Options (-o key=value) for every process device:"]
    assert sum(1 for line in lines if line.startswith("• data_dir=")) == 1


def test_device_lines_name_the_device_when_a_schema_is_its_own():
    """A single device's options block names it, rather than claiming "every"."""
    lines = device_lines(Operation.MONITOR)

    assert "Options (-o key=value) for bluefors_gen1:" in lines


def test_device_lines_state_the_default_device():
    """--device is optional, so help says what it falls back to."""
    assert "default mock" in device_lines(Operation.PROCESS)[0]
    assert "default bluefors_gen1" in device_lines(Operation.MONITOR)[0]


def test_render_catalog_narrows_to_one_operation():
    everything = render_catalog()
    monitor_only = render_catalog(Operation.MONITOR)

    assert "process" in everything and "monitor" in everything
    assert "bluefors_gen1" in monitor_only
    assert "qblox" not in monitor_only


def test_missing_for_extras_finds_an_uninstalled_requirement():
    """An extra whose distributions are absent is reported, by distribution name."""
    missing = missing_for_extras(("vendor",), REQUIREMENTS)

    assert set(missing) == {"no-such-distribution", "also-absent"}


def test_missing_for_extras_follows_an_extra_of_this_distribution():
    """`qiskit_aer` is declared as `qpi-driver[aer]`, so resolution has to recurse.

    Without following it, such a device would look installed whatever the truth —
    the failure mode is a confident wrong answer, so it gets its own test.
    """
    assert set(missing_for_extras(("wrapper",), REQUIREMENTS)) == {
        "no-such-distribution",
        "also-absent",
    }


def test_missing_for_extras_ignores_an_installed_requirement():
    """Nothing is reported for an extra whose requirements are all present."""
    assert missing_for_extras(("installed",), REQUIREMENTS) == ()


def test_missing_for_extras_ignores_requirements_of_no_extra():
    """A plain dependency belongs to no extra, so no extra may claim it."""
    assert missing_for_extras(("nonexistent",), REQUIREMENTS) == ()


def test_missing_for_extras_terminates_on_a_cycle():
    """A self-referential extra must not loop; each extra is resolved once."""
    cyclic = [
        'qpi-driver[b]; extra == "a"',
        'qpi-driver[a]; extra == "b"',
        'nowhere-at-all; extra == "b"',
    ]

    assert missing_for_extras(("a",), cyclic) == ("nowhere-at-all",)


def test_missing_requirements_is_silent_for_a_device_needing_nothing():
    """mock and presto ship with the base extra, so they are never "unavailable"."""
    assert missing_requirements("") == ()


def test_missing_requirements_reads_real_metadata():
    """Against the installed distribution: an answer, and never a crash.

    Which devices are installed depends on the extras the test run synced, so the
    assertion is on the kind of answer, not its contents.
    """
    result = missing_requirements("qpi-driver[cli,qblox]")

    assert isinstance(result, tuple)
    assert all(isinstance(name, str) for name in result)
