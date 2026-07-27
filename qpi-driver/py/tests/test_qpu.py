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


def test_build_from_options_reads_all_keys():
    driver = build_from_options(
        executor="mock",
        **_transport(),
        options={
            "data_dir": "./bin/data/run-1",
            "job_timeout": "42",
            "is_dummy": "yes",
            "quantify_hardware_config": "./hw.json",
            "quantify_device_config": "./dev.yml",
        },
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
    driver = build_from_options(executor="mock", **_transport(), options={})

    assert driver.data_dir == Path("./bin/data")
    assert driver.executor_options["job_timeout"] == 10
    assert driver.executor_options["is_dummy"] is False


def test_build_from_options_returns_an_unstarted_driver():
    """Building connects to nothing and starts no worker."""
    driver = build_from_options(executor="mock", **_transport(), options={})

    assert driver._out_sock is None
    assert driver._worker is None
    assert driver._result_pump is None


def test_build_from_options_rejects_an_unsafe_data_dir():
    """A path outside the allowed roots is refused before anything is built."""
    with pytest.raises(ValueError, match="safe location"):
        build_from_options(
            executor="mock", **_transport(), options={"data_dir": "/var"}
        )


def test_every_process_spec_binds_its_own_executor():
    """The specs share one builder and differ only in the executor bound to it.

    This is what replaced the device-name-to-run-function table: the device name
    reaches the driver because the spec closed over it, not because the builder
    looked it up.
    """
    assert {spec.name for spec in devices(Operation.PROCESS)} == set(PROCESS_DEVICES)

    for name in PROCESS_DEVICES:
        driver = resolve(Operation.PROCESS, name).build(**_transport(), options={})
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
