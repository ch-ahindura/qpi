"""Building a QPU (``process``) driver from CLI options (RFC 0003 §5, §7).

The point of a builder returning an unstarted driver is that every option can be
asserted here — with no QPI-UI server, no NNG socket and no worker subprocess.
The transport is covered by the SDK's own tests and the e2e suite.
"""

from pathlib import Path

import pytest
from qpi_driver.builtins.qpu import PROCESS_DEVICES, QpuDriver, build_from_options
from qpi_driver.builtins.registry import Operation, devices, resolve


def _transport() -> dict:
    return dict(
        qpi_addr="http://localhost:8090",
        token="t",
        name="qpu-sim-01",
        ca_fingerprint="fp",
        ca_file_path="./bin/qpi.ca.pem",
        recv_timeout_ms=200,
    )


def _parsed(**raw: str) -> dict:
    """Run *raw* ``-o`` strings through a process device's schema, as the CLI does.

    Every process device shares one schema, so which one is asked is immaterial.
    """
    return resolve(Operation.PROCESS, "mock").parse_options(raw)


def test_build_from_options_reads_all_keys():
    driver = build_from_options(
        executor="mock",
        **_transport(),
        options=_parsed(
            data_dir="./bin/data/run-1",
            job_timeout="42",
            is_dummy="yes",
            quantify_hardware_config="./hw.json",
            quantify_device_config="./dev.yml",
        ),
    )

    assert isinstance(driver, QpuDriver)
    assert driver.executor == "mock"
    assert driver.data_dir == Path("./bin/data/run-1")
    assert driver.executor_options["job_timeout"] == 42
    assert driver.executor_options["is_dummy"] is True
    assert driver.executor_options["quantify_hardware_config"] == Path("./hw.json")
    assert driver.executor_options["quantify_device_config"] == Path("./dev.yml")

    # The transport arguments land too, normalized as the SDK expects.
    assert driver.qpi_addr == "http://localhost:8090"
    assert driver.name == "qpu_sim_01"
    assert driver.recv_timeout_ms == 200


def test_build_from_options_defaults_every_key():
    """A QPU needs no -o options at all; each one has a working default."""
    driver = build_from_options(executor="mock", **_transport(), options=_parsed())

    assert driver.data_dir == Path("./bin/data")
    assert driver.executor_options["job_timeout"] == 10
    assert driver.executor_options["is_dummy"] is False
    assert driver.executor_options["quantify_hardware_config"] == Path(
        "./quantify.hardware.json"
    )
    assert driver.executor_options["quantify_device_config"] == Path(
        "./quantify.device.yml"
    )


def test_build_from_options_forwards_every_option_but_data_dir():
    """The executor gets whatever the schema parsed, without the builder listing it.

    That is what lets an option be added to :data:`OPTIONS` — or declared by a
    device the SDK does not ship — and reach the executor with no edit here.
    """
    options = _parsed()
    driver = build_from_options(executor="mock", **_transport(), options=options)

    assert set(driver.executor_options) == set(options) - {"data_dir"}


def test_build_from_options_returns_an_unstarted_driver():
    """Building connects to nothing and starts no worker."""
    driver = build_from_options(executor="mock", **_transport(), options=_parsed())

    assert driver._out_sock is None
    assert driver._worker is None
    assert driver._result_pump is None


def test_data_dir_must_be_in_a_safe_location():
    """An unsafe data_dir is refused by the option's own parser (RFC 0003 §10).

    The check moved out of the builder into the schema, so it now happens before
    anything is constructed — and it still happens, which is the point.
    """
    with pytest.raises(ValueError, match="safe location"):
        _parsed(data_dir="/var")


def test_process_options_are_coerced_by_their_parsers():
    """Each -o string becomes the type the executor expects, in one place only."""
    parsed = _parsed(
        data_dir="./bin/data",
        job_timeout="42",
        is_dummy="ON",
        quantify_device_config="./dev.yml",
    )

    assert parsed["data_dir"] == Path("./bin/data")
    assert parsed["job_timeout"] == 42
    assert parsed["is_dummy"] is True
    assert parsed["quantify_device_config"] == Path("./dev.yml")
    # A value none of the truthy spellings matches is false, not an error.
    assert _parsed(is_dummy="maybe")["is_dummy"] is False


def test_every_process_spec_binds_its_own_executor():
    """The specs share one builder and differ only in the executor bound to it.

    This is what replaced the device-name-to-run-function table: the device name
    reaches the driver because the spec closed over it, not because the builder
    looked it up.
    """
    assert {spec.name for spec in devices(Operation.PROCESS)} == set(PROCESS_DEVICES)

    for name in PROCESS_DEVICES:
        driver = resolve(Operation.PROCESS, name).build(
            **_transport(), options=_parsed()
        )
        assert driver.executor == name


def test_every_process_spec_declares_its_install_extra():
    """A device that needs an optional dependency says which extra ships it."""
    extras = {spec.name: spec.extra for spec in devices(Operation.PROCESS)}

    assert extras["qiskit_aer"] == "qpi-driver[cli,aer]"
    assert extras["quantify"] == "qpi-driver[cli,quantify]"
    assert extras["qblox"] == "qpi-driver[cli,qblox]"
    # mock and presto need nothing beyond the base [cli] extra.
    assert extras["mock"] == ""
    assert extras["presto"] == ""
