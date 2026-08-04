"""The ``calibrate`` driver: options, event handling, worker and drift (RFC 0004 §6.5).

Everything here runs against a stub tuner in the base environment — no
scheduler, no instruments, no server. That is the point of a builder that
returns an unstarted driver (RFC 0003 §7): the whole event path is assertable
without a lab.
"""

from pathlib import Path

import pytest
from qpi_driver.builtins.calibrate import (
    CALIBRATE_DEVICES,
    DRIFT_CHECK_JOB_ID,
    CalibrateDriver,
    _drifted_targets,
    _execute_calibration,
    build_from_options,
    calibrate_worker,
    device_spec,
)
from qpi_driver.builtins.registry import Operation, devices, resolve
from qpi_driver.events import Event, EventType
from qpi_driver.options import Options
from qpi_driver.tuners import Tuner, resolve_tuner
from qpi_driver.tuners.base.config import CalibrationConfig
from qpi_driver.tuners.base.report import BenchmarkResult, CalibrationReport


class StubTuner(Tuner):
    """A tuner that records what it was asked to do and returns a fixed report."""

    def __init__(self, name: str = "stub", **kwargs):
        super().__init__(name)
        self.calls: list[tuple[str, object]] = []
        self.report = CalibrationReport(
            timestamp="2026-07-30T00:00:00.000Z",
            duration_s=1.0,
            mode="full",
            backend="stub",
        )
        self.closed = False

    @property
    def backend(self):  # pragma: no cover - never run in these tests
        raise NotImplementedError

    @property
    def device(self):  # pragma: no cover - never run in these tests
        raise NotImplementedError

    def calibrate(self, config):
        self.calls.append(("calibrate", None))
        return self.report

    def recalibrate(self, qubits, config):
        self.calls.append(("recalibrate", list(qubits)))
        return self.report

    def check_fidelity(self, config):
        self.calls.append(("check_fidelity", None))
        return self.report

    def close(self):
        self.closed = True


class RecordingQueue:
    """A stand-in for a multiprocessing queue that just remembers what it got."""

    def __init__(self):
        self.items: list = []

    def put(self, item):
        self.items.append(item)


def _transport() -> dict:
    return dict(
        qpi_addr="http://localhost:8090",
        token="t",
        ca_fingerprint="fp",
        ca_file_path="./bin/qpi.ca.pem",
        recv_timeout_ms=200,
    )


def _driver(**kwargs) -> CalibrateDriver:
    driver = CalibrateDriver(
        tuner=StubTuner(), calibration_config=Path("./calibration.yml"), **kwargs
    )
    driver._job_queue = RecordingQueue()
    driver.name = "cal-1"
    return driver


# --- options and registration -------------------------------------------------


def test_build_from_options_reads_every_key():
    driver = build_from_options(
        tuner="quantify",
        **_transport(),
        options=Options(
            {
                "calibration_config": "./cal.yml",
                "drift_check_interval": "1800",
                "fidelity_threshold": "0.9995",
                "fidelity_2q_threshold": "0.98",
                "is_dummy": "yes",
                "is_simulated": "no",
                "quantify_hardware_config": "./hw.json",
                "quantify_device_config": "./dev.yml",
            }
        ),
    )

    assert driver.calibration_config_path == Path("./cal.yml")
    assert driver.drift_check_interval == 1800
    assert driver.fidelity_threshold == 0.9995
    assert driver.fidelity_2q_threshold == 0.98
    assert driver.tuner_options["is_dummy"] is True
    assert driver.tuner_options["is_simulated"] is False
    assert driver.tuner_options["quantify_device_config"] == Path("./dev.yml")


def test_is_simulated_reaches_the_tuner():
    """``-o is_simulated=true`` is what runs a node with no hardware at all."""
    driver = build_from_options(
        tuner="quantify",
        **_transport(),
        options=Options({"is_simulated": "true"}),
    )
    assert driver.tuner_options["is_simulated"] is True
    assert driver.tuner_options["is_dummy"] is False


def test_build_from_options_needs_no_options_at_all():
    """Every option has a working default, so a tuner starts with no ``-o``."""
    driver = build_from_options(tuner="quantify", **_transport(), options=Options({}))

    assert driver.calibration_config_path == Path("./calibration.yml")
    assert driver.drift_check_interval == 0
    assert driver.tuner_options["quantify_device_config"] == Path(
        "./quantify.device.yml"
    )


def test_an_option_nothing_reads_is_visible_as_unread():
    """Reading an option is what declares it (RFC 0003 §13.6).

    The CLI turns an unread option into a startup error; what this asserts is
    that the builder leaves it unread rather than swallowing it.
    """
    options = Options({"calibration_config": "./cal.yml", "no_such_option": "1"})
    build_from_options(tuner="quantify", **_transport(), options=options)

    assert options.unread() == ("no_such_option",)


def test_both_tuner_devices_are_registered():
    registered = {spec.name for spec in devices(Operation.CALIBRATE)}
    assert registered == set(CALIBRATE_DEVICES)
    assert (
        resolve(Operation.CALIBRATE, "quantify_tuner").operation is Operation.CALIBRATE
    )


def test_device_name_drops_the_tuner_suffix_to_name_its_tuner():
    """``quantify_tuner`` is the device; ``quantify`` is the tuner it runs."""
    spec = device_spec("quantify_tuner")
    assert spec.build.keywords["tuner"] == "quantify"


# --- event handling -----------------------------------------------------------


def test_a_dispatch_is_queued_for_the_worker():
    driver = _driver()
    driver.handle_event(
        Event(
            type=EventType.CALIBRATE_DISPATCH,
            payload={"job_id": "j1", "mode": "full"},
        )
    )
    assert driver._job_queue.items == [{"job_id": "j1", "mode": "full"}]


def test_any_other_event_is_dropped():
    driver = _driver()
    driver.handle_event(Event(type=EventType.JOB_DISPATCH, payload={"job_id": "j1"}))
    assert driver._job_queue.items == []


def test_a_second_calibration_is_refused_rather_than_queued(monkeypatch):
    """One at a time: a report nobody can connect to a request is worse than a refusal."""
    driver = _driver()
    emitted: list[Event] = []
    monkeypatch.setattr(driver, "emit", emitted.append)

    driver.handle_event(
        Event(type=EventType.CALIBRATE_DISPATCH, payload={"job_id": "first"})
    )
    driver.handle_event(
        Event(type=EventType.CALIBRATE_DISPATCH, payload={"job_id": "second"})
    )

    assert [job["job_id"] for job in driver._job_queue.items] == ["first"]
    assert len(emitted) == 1
    assert "already running" in emitted[0].payload["error"]
    assert emitted[0].payload["job_id"] == "second"


# --- the drift timer ----------------------------------------------------------


def test_no_drift_timer_is_registered_when_the_interval_is_zero():
    assert _driver(drift_check_interval=0)._periodic == []


def test_the_drift_timer_is_registered_when_an_interval_is_given():
    driver = _driver(drift_check_interval=900)
    assert [interval for interval, _ in driver._periodic] == [900]


def test_a_drift_check_carries_the_thresholds_it_is_judged_against():
    driver = _driver(
        drift_check_interval=900, fidelity_threshold=0.99, fidelity_2q_threshold=0.9
    )
    driver._check_fidelity()

    queued = driver._job_queue.items[0]
    assert queued["mode"] == "fidelity_check"
    assert queued["job_id"] == DRIFT_CHECK_JOB_ID
    assert queued["fidelity_threshold"] == 0.99
    assert queued["fidelity_2q_threshold"] == 0.9


def test_a_drift_check_is_skipped_while_a_calibration_is_running():
    driver = _driver(drift_check_interval=900)
    driver._busy.set()
    driver._check_fidelity()
    assert driver._job_queue.items == []


# --- result pump --------------------------------------------------------------


def _pump_once(driver, item, monkeypatch):
    """Run the pump over one item, then stop it."""
    emitted: list[Event] = []
    monkeypatch.setattr(driver, "emit", emitted.append)
    driver._result_queue = _QueueOf([item, None])
    driver._pump_results()
    return emitted


class _QueueOf:
    def __init__(self, items):
        self._items = list(items)

    def get(self):
        return self._items.pop(0)


def test_a_report_is_emitted_flat_not_nested(monkeypatch):
    """QPI-UI unmarshals the payload directly, so the fields are top level.

    Nesting them under a ``results`` key parses without error and leaves every
    field at its zero value — a blank record saved for a real calibration.
    """
    driver = _driver()
    report = CalibrationReport(
        timestamp="2026-07-30T00:00:00.000Z",
        duration_s=3.0,
        mode="full",
        backend="stub",
    )
    emitted = _pump_once(
        driver, {"job_id": "j1", "report": report.to_event_payload()}, monkeypatch
    )

    payload = emitted[0].payload
    assert emitted[0].type is EventType.CALIBRATION_RESULT
    assert payload["job_id"] == "j1"
    assert payload["mode"] == "full"
    assert payload["status"] == "success"
    assert payload["duration_s"] == 3.0
    assert "results" not in payload


def test_a_worker_error_is_emitted_as_an_error(monkeypatch):
    driver = _driver()
    emitted = _pump_once(driver, {"job_id": "j1", "error": "boom"}, monkeypatch)
    assert emitted[0].payload["error"] == "boom"


def test_a_drift_follow_up_is_queued_as_a_partial_recalibration(monkeypatch):
    driver = _driver()
    report = CalibrationReport(
        timestamp="t", duration_s=1.0, mode="fidelity_check", backend="stub"
    )
    _pump_once(
        driver,
        {
            "job_id": DRIFT_CHECK_JOB_ID,
            "report": report.to_event_payload(),
            "follow_up": [{"mode": "partial", "job_id": "r1", "target_qubits": ["q0"]}],
        },
        monkeypatch,
    )
    assert driver._job_queue.items == [
        {"mode": "partial", "job_id": "r1", "target_qubits": ["q0"]}
    ]


# --- the worker ---------------------------------------------------------------


def test_each_mode_calls_its_tuner_method():
    tuner = StubTuner()
    results = RecordingQueue()
    config = CalibrationConfig(target_qubits=["q0"])

    _execute_calibration({"job_id": "a", "mode": "full"}, tuner, config, results)
    _execute_calibration(
        {"job_id": "b", "mode": "partial", "target_qubits": ["q1"]},
        tuner,
        config,
        results,
    )
    _execute_calibration(
        {"job_id": "c", "mode": "fidelity_check"}, tuner, config, results
    )

    assert tuner.calls == [
        ("calibrate", None),
        ("recalibrate", ["q1"]),
        ("check_fidelity", None),
    ]
    assert [item["job_id"] for item in results.items] == ["a", "b", "c"]


def test_a_partial_without_targets_is_refused_not_widened_to_a_full_run():
    """Silently running a full calibration instead would take the QPU out for hours."""
    results = RecordingQueue()
    _execute_calibration(
        {"job_id": "a", "mode": "partial"},
        StubTuner(),
        CalibrationConfig(target_qubits=["q0"]),
        results,
    )
    assert "target_qubits" in results.items[0]["error"]


def test_an_unknown_mode_is_reported():
    results = RecordingQueue()
    _execute_calibration(
        {"job_id": "a", "mode": "sideways"},
        StubTuner(),
        CalibrationConfig(),
        results,
    )
    assert "sideways" in results.items[0]["error"]


def test_a_tuner_that_raises_is_reported_not_fatal():
    class Exploding(StubTuner):
        def calibrate(self, config):
            raise ValueError("no cluster")

    results = RecordingQueue()
    _execute_calibration(
        {"job_id": "a", "mode": "full"}, Exploding(), CalibrationConfig(), results
    )
    assert "no cluster" in results.items[0]["error"]


def test_a_missing_calibration_config_is_an_init_error(tmp_path):
    """A tuner with no config would calibrate nothing and report success."""
    results = RecordingQueue()
    calibrate_worker(
        job_queue=_QueueOf([None]),
        result_queue=results,
        tuner=StubTuner(),
        calibration_config_path=tmp_path / "missing.yml",
    )
    assert results.items[0]["job_id"] == "init_error"
    assert "not found" in results.items[0]["error"]


def test_an_unresolvable_tuner_is_an_init_error(tmp_path):
    results = RecordingQueue()
    calibrate_worker(
        job_queue=_QueueOf([None]),
        result_queue=results,
        tuner="no_such_tuner",
        calibration_config_path=tmp_path / "cal.yml",
    )
    assert results.items[0]["job_id"] == "init_error"
    assert "Failed to resolve tuner" in results.items[0]["error"]


def test_an_unparseable_calibration_config_is_an_init_error(tmp_path):
    config = tmp_path / "cal.yml"
    config.write_text("routines: [not, a, mapping]\n")
    results = RecordingQueue()
    calibrate_worker(
        job_queue=_QueueOf([None]),
        result_queue=results,
        tuner=StubTuner(),
        calibration_config_path=config,
    )
    assert results.items[0]["job_id"] == "init_error"
    assert "Failed to load calibration config" in results.items[0]["error"]


def test_the_worker_runs_queued_jobs_until_the_poison_pill(tmp_path):
    config = tmp_path / "cal.yml"
    config.write_text("target_qubits: [q0]\n")
    tuner = StubTuner()
    results = RecordingQueue()

    calibrate_worker(
        job_queue=_QueueOf([{"job_id": "a", "mode": "full"}, None]),
        result_queue=results,
        tuner=tuner,
        calibration_config_path=config,
    )

    assert [item["job_id"] for item in results.items] == ["a"]
    assert tuner.closed is True


class _ConfigRecordingTuner(StubTuner):
    """StubTuner discards the config it is handed; these tests are about it."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.configs: list = []

    def calibrate(self, config):
        self.configs.append(config)
        return super().calibrate(config)


def test_an_edited_calibration_config_takes_effect_on_the_next_dispatch(tmp_path):
    config = tmp_path / "cal.yml"
    config.write_text("target_qubits: [q0]\n")
    tuner = _ConfigRecordingTuner()
    results = RecordingQueue()

    def rewrite_then_return(items):
        """Edit the file between the first job being taken and the second."""
        yielded = []
        for item in items:
            if len(yielded) == 1:
                config.write_text("target_qubits: [q0, q1]\n")
            yielded.append(item)
            yield item

    class _EditingQueue:
        def __init__(self, items):
            self._gen = rewrite_then_return(items)

        def get(self):
            return next(self._gen)

    calibrate_worker(
        job_queue=_EditingQueue(
            [{"job_id": "a", "mode": "full"}, {"job_id": "b", "mode": "full"}, None]
        ),
        result_queue=results,
        tuner=tuner,
        calibration_config_path=config,
    )

    assert tuner.configs[0].target_qubits == ["q0"]
    assert tuner.configs[1].target_qubits == ["q0", "q1"], (
        "the second calibration should have used the edited config"
    )


def test_a_broken_edit_fails_that_calibration_and_leaves_the_worker_alive(tmp_path):
    """Failing the job, not carrying on: this config decides which routines run."""
    config = tmp_path / "cal.yml"
    config.write_text("target_qubits: [q0]\n")
    tuner = StubTuner()
    results = RecordingQueue()

    class _BreakingQueue:
        def __init__(self, items):
            self._items = list(items)
            self._taken = 0

        def get(self):
            item = self._items.pop(0)
            self._taken += 1
            if self._taken == 1:
                config.write_text("routines: [not, a, mapping]\n")
            return item

    calibrate_worker(
        job_queue=_BreakingQueue(
            [{"job_id": "a", "mode": "full"}, {"job_id": "b", "mode": "full"}, None]
        ),
        result_queue=results,
        tuner=tuner,
        calibration_config_path=config,
    )

    assert results.items[0]["job_id"] == "a"
    assert "no longer loads" in results.items[0]["error"]
    # The worker survived to take the next job and shut down cleanly.
    assert tuner.closed is True


# --- lifecycle ----------------------------------------------------------------


def test_start_spawns_a_worker_and_a_pump_then_stop_joins_them(tmp_path):
    """The one test that really starts the subprocess, so the wiring is proved."""
    driver = CalibrateDriver(
        tuner=StubTuner(), calibration_config=tmp_path / "missing.yml"
    )

    driver._on_start()
    try:
        assert driver._worker is not None and driver._worker.is_alive()
        assert driver._result_pump is not None and driver._result_pump.is_alive()
        assert driver._worker.daemon and driver._result_pump.daemon
    finally:
        driver._on_stop()

    assert not driver._worker.is_alive()


def test_stop_is_safe_before_start(tmp_path):
    """SIGINT during the handshake means stopping something never started."""
    CalibrateDriver(
        tuner=StubTuner(), calibration_config=tmp_path / "cal.yml"
    )._on_stop()


def test_stop_terminates_a_worker_that_will_not_exit(tmp_path):
    """A calibration will not finish inside the join window, so terminate is the norm."""

    class Stubborn:
        def __init__(self):
            self.terminated = False
            self.joins = 0

        def join(self, timeout=None):
            self.joins += 1

        def is_alive(self):
            return not self.terminated

        def terminate(self):
            self.terminated = True

    driver = _driver()
    driver._result_queue = RecordingQueue()
    driver._worker = Stubborn()
    driver._on_stop()

    assert driver._worker.terminated is True
    assert driver._worker.joins == 2  # once with a timeout, once after terminate


# --- drift detection ----------------------------------------------------------


def _report_with(**fidelities) -> CalibrationReport:
    report = CalibrationReport(
        timestamp="t", duration_s=1.0, mode="fidelity_check", backend="stub"
    )
    for target, fidelity in fidelities.items():
        report.add_benchmark(
            BenchmarkResult(
                protocol="rb", target=target, fidelity=fidelity, error_per_gate=None
            )
        )
    return report


def test_nothing_drifts_when_every_fidelity_is_above_threshold():
    job = {"fidelity_threshold": 0.999, "fidelity_2q_threshold": 0.99}
    assert _drifted_targets(_report_with(q0=0.9995), job) == []


def test_a_qubit_below_its_threshold_drifts():
    job = {"fidelity_threshold": 0.999, "fidelity_2q_threshold": 0.99}
    assert _drifted_targets(_report_with(q0=0.99, q1=0.9999), job) == ["q0"]


def test_an_edge_is_judged_against_the_two_qubit_threshold():
    """A CZ an order of magnitude worse than a 1Q gate is normal, not drift."""
    job = {"fidelity_threshold": 0.999, "fidelity_2q_threshold": 0.99}
    assert _drifted_targets(_report_with(q0_q1=0.995), job) == []
    assert _drifted_targets(_report_with(q0_q1=0.95), job) == ["q0", "q1"]


def test_the_worst_protocol_wins_for_a_target():
    """A drift check should fire on the worst evidence it has, not the best."""
    report = _report_with(q0=0.9999)
    report.add_benchmark(
        BenchmarkResult(
            protocol="allxy_check", target="q0", fidelity=0.90, error_per_gate=None
        )
    )
    assert report.fidelities()["q0"] == 0.90


# --- tuner resolution ---------------------------------------------------------


def test_resolve_tuner_accepts_an_instance_a_class_and_a_name():
    instance = StubTuner()
    assert resolve_tuner(instance) is instance
    assert isinstance(resolve_tuner(StubTuner), StubTuner)
    with pytest.raises(ValueError, match="Unknown tuner name"):
        resolve_tuner("nope")
    with pytest.raises(TypeError, match="Invalid tuner type"):
        resolve_tuner(42)


def test_a_tuner_class_is_named_after_itself_when_unnamed():
    assert resolve_tuner(StubTuner).name == "stub"
