"""Building a QPU (``process``) driver from CLI options (RFC 0003 §5, §7).

The point of a builder returning an unstarted driver is that every option can be
asserted here — with no QPI-UI server, no NNG socket and no worker subprocess.
The transport is covered by the SDK's own tests and the e2e suite.
"""

from pathlib import Path

import pytest
from qpi_driver.builtins.qpu import PROCESS_DEVICES, QpuDriver, build_from_options
from qpi_driver.builtins.registry import Operation, devices, resolve
from qpi_driver.options import Options


def _transport() -> dict:
    return dict(
        qpi_addr="http://localhost:8090",
        token="t",
        ca_fingerprint="fp",
        ca_file_path="./bin/qpi.ca.pem",
        recv_timeout_ms=200,
    )


def _options(**raw: str) -> Options:
    """The raw ``-o`` strings a device is handed, as the CLI hands them over."""
    return Options(raw)


def test_build_from_options_reads_all_keys():
    driver = build_from_options(
        executor="mock",
        **_transport(),
        options=_options(
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
    assert driver.recv_timeout_ms == 200
    # A driver does not name itself; the label arrives in the connect response.
    assert driver.name == ""


def test_build_from_options_defaults_every_key():
    """A QPU needs no -o options at all; each one has a working default."""
    driver = build_from_options(executor="mock", **_transport(), options=_options())

    assert driver.data_dir == Path("./bin/data")
    assert driver.executor_options["job_timeout"] == 10
    assert driver.executor_options["is_dummy"] is False
    assert driver.executor_options["quantify_hardware_config"] == Path(
        "./quantify.hardware.json"
    )
    assert driver.executor_options["quantify_device_config"] == Path(
        "./quantify.device.yml"
    )


def test_saving_raw_data_is_off_unless_asked_for():
    driver = build_from_options(executor="mock", **_transport(), options=_options())
    assert driver.executor_options["save_raw_data"] is False

    driver = build_from_options(
        executor="mock", **_transport(), options=_options(save_raw_data="yes")
    )
    assert driver.executor_options["save_raw_data"] is True


def test_build_from_options_returns_an_unstarted_driver():
    """Building connects to nothing and starts no worker."""
    driver = build_from_options(executor="mock", **_transport(), options=_options())

    assert driver._out_sock is None
    assert driver._worker is None
    assert driver._result_pump is None


def test_data_dir_must_be_in_a_safe_location():
    """An unsafe data_dir is refused as it is read (RFC 0003 §10)."""
    with pytest.raises(ValueError, match="safe location"):
        build_from_options(
            executor="mock", **_transport(), options=_options(data_dir="/var")
        )


def test_the_universal_data_dir_is_what_a_qpu_writes_under():
    driver = build_from_options(
        executor="mock",
        **_transport(),
        data_dir=Path("/tmp/qpi-qpu"),
        options=_options(),
    )

    assert driver.data_dir == Path("/tmp/qpi-qpu")


def test_an_o_data_dir_overrides_the_universal_one():
    """Unit files predate the flag, so the option they set has to keep winning."""
    driver = build_from_options(
        executor="mock",
        **_transport(),
        data_dir=Path("/tmp/qpi-qpu"),
        options=_options(data_dir="/tmp/from-the-unit-file"),
    )

    assert driver.data_dir == Path("/tmp/from-the-unit-file")


def test_an_unsafe_data_dir_is_named_by_the_spelling_that_set_it():
    with pytest.raises(ValueError, match="bad value for --data-dir"):
        build_from_options(
            executor="mock", **_transport(), data_dir=Path("/var"), options=_options()
        )


def test_a_builtin_executor_does_not_take_an_option_it_never_reads():
    """A typo left unread is what the CLI reports; executors swallow **kwargs.

    A built-in executor's constructor accepts anything, so nothing downstream
    would notice `-o data_dr=…` — the unread key is the only trace of it.
    """
    options = _options(data_dr="./bin/data")
    build_from_options(executor="mock", **_transport(), options=options)

    assert options.unread() == ("data_dr",)


def test_an_imported_executor_gets_the_options_this_module_never_heard_of():
    """Its constructor is the only thing that knows its keys, so they pass through."""
    options = _options(qubit_count="5", data_dir="./bin/data")
    driver = build_from_options(
        executor="mock", **_transport(), options=options, pass_through=True
    )

    assert driver.executor_options["qubit_count"] == "5"
    assert options.unread() == ()


def test_every_process_spec_binds_its_own_executor():
    """The specs share one builder and differ only in the executor bound to it.

    This is what replaced the device-name-to-run-function table: the device name
    reaches the driver because the spec closed over it, not because the builder
    looked it up.
    """
    assert {spec.name for spec in devices(Operation.PROCESS)} == set(PROCESS_DEVICES)

    for name in PROCESS_DEVICES:
        driver = resolve(Operation.PROCESS, name).build(
            **_transport(), options=_options()
        )
        assert driver.executor == name
