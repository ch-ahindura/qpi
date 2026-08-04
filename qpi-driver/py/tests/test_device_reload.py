"""An executor picks up a device config rewritten under it (RFC 0004 §8, §11).

Parametrised over whichever scheduler is installed rather than duplicated, as
`test_calibration_loop.py` is: the reload is one mechanism and a claim proved for
one scheduler and not the other is how several two-scheduler faults got in.
"""

import importlib.util

import pytest
import yaml

from qpi_driver.executors.base import CircuitPayload, JobPayload

from .utils import load_json_fixture, load_yaml_fixture

_HARDWARE = load_json_fixture("quantify.hardware.json")
_DEVICE = load_yaml_fixture("quantify.device.yml")

SCHEDULERS = [
    name
    for name, module in (
        ("quantify", "quantify_scheduler"),
        ("qblox", "qblox_scheduler"),
    )
    if importlib.util.find_spec(module) is not None
]

pytestmark = pytest.mark.skipif(
    not SCHEDULERS, reason="needs quantify-scheduler or qblox-scheduler"
)

QASM = (
    'OPENQASM 3.0;\ninclude "stdgates.inc";\nqubit[1] q;\nbit[1] c;\n'
    "x q[0];\nc[0] = measure q[0];\n"
)


@pytest.fixture(params=SCHEDULERS)
def scheduler(request) -> str:
    return request.param


def _executor(scheduler: str, device_path, data_dir, hardware=None):
    if scheduler == "quantify":
        from qpi_driver.executors.quantify import QuantifyExecutor as Executor
    else:
        from qpi_driver.executors.qblox import QbloxExecutor as Executor

    return Executor(
        name=f"reload_{scheduler}",
        quantify_hardware_config=hardware if hardware is not None else _HARDWARE,
        quantify_device_config=device_path,
        is_dummy=True,
        data_dir=data_dir,
    )


@pytest.fixture
def device_file(tmp_path):
    path = tmp_path / "quantify.device.yml"
    path.write_text(yaml.safe_dump(_DEVICE))
    return path


def _f01(executor) -> float:
    from qpi_driver.tuners.base.device import read_path

    return read_path(executor._device.get_element("q0"), "clock_freqs.f01")


def _rewrite_f01(path, value: float) -> None:
    config = yaml.safe_load(path.read_text())
    config["q0"]["clock_freqs"]["f01"] = value
    path.write_text(yaml.safe_dump(config))


def _run(executor) -> None:
    executor.execute(JobPayload(circuits=[CircuitPayload(circuit=QASM)], shots=1))


def test_a_rewritten_frequency_reaches_a_running_executor(
    scheduler, device_file, tmp_path
):
    executor = _executor(scheduler, device_file, tmp_path)
    try:
        before = _f01(executor)
        _run(executor)

        _rewrite_f01(device_file, before + 12e6)
        _run(executor)

        assert _f01(executor) == pytest.approx(before + 12e6)
    finally:
        executor.close()


# The case an operator hits without any tuner involved: parameters restored or
# measured by hand, dropped in place, no restart.
def test_a_hand_written_config_needs_no_restart(scheduler, device_file, tmp_path):
    executor = _executor(scheduler, device_file, tmp_path)
    try:
        _run(executor)
        _rewrite_f01(device_file, 5.432e9)
        _run(executor)

        assert _f01(executor) == pytest.approx(5.432e9)
    finally:
        executor.close()


def test_an_unreadable_config_leaves_the_device_alone(scheduler, device_file, tmp_path):
    executor = _executor(scheduler, device_file, tmp_path)
    try:
        before = _f01(executor)
        _run(executor)

        device_file.write_text("q0: {clock_freqs: {f01: [unclosed\n")
        _run(executor)

        assert _f01(executor) == pytest.approx(before)
    finally:
        executor.close()


def test_an_untouched_config_is_not_reapplied(scheduler, device_file, tmp_path):
    """A device the operator changed in memory is not reverted by a no-op reload."""
    executor = _executor(scheduler, device_file, tmp_path)
    try:
        from qpi_driver.tuners.base.device import write_path

        _run(executor)
        write_path(executor._device.get_element("q0"), "clock_freqs.f01", 4.9e9)
        _run(executor)

        assert _f01(executor) == pytest.approx(4.9e9)
    finally:
        executor.close()


def test_a_changed_hardware_config_warns_rather_than_reconnecting(
    scheduler, device_file, tmp_path, caplog
):
    """It builds the Cluster, so applying a new one means redialling the rack.

    A reconnect that fails would leave the driver with no coordinator and no way
    back, the old one already closed — the device config has a fallback, this has
    none. So it warns and keeps running, and a restart is the answer.
    """
    import json

    hardware = tmp_path / "quantify.hardware.json"
    hardware.write_text(json.dumps(_HARDWARE))

    executor = _executor(scheduler, device_file, tmp_path, hardware=hardware)
    try:
        before = executor.hardware_config
        _run(executor)

        hardware.write_text(json.dumps(_HARDWARE) + "\n")
        with caplog.at_level("WARNING"):
            _run(executor)

        assert "Restart it" in caplog.text
        assert executor.hardware_config is before, "it must not have been reloaded"
    finally:
        executor.close()
