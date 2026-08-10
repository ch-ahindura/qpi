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
    _queue_progress,
    build_from_options,
    calibrate_worker,
    device_spec,
)
from qpi_driver.builtins.registry import Operation, devices, resolve
from qpi_driver.events import Event, EventType
from qpi_driver.options import Options
from qpi_driver.tuners import Tuner, resolve_tuner
from qpi_driver.tuners.base.config import (
    DEFAULT_ROUTINE_TIMEOUT_S,
    CalibrationConfig,
)
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


class TestOptionsAndRegistration:
    """Building a driver from `Options`, and the tuners it registers under `resolve_tuner`."""

    def test_build_from_options_reads_every_key(self):
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

    def test_the_universal_data_dir_reaches_the_tuner(self):
        driver = build_from_options(
            tuner="quantify",
            **_transport(),
            data_dir=Path("/tmp/qpi-tuner"),
            options=Options({}),
        )

        assert driver.tuner_options["data_dir"] == Path("/tmp/qpi-tuner")

    def test_an_o_data_dir_still_overrides_the_flag(self):
        """A unit file written before `--data-dir` existed keeps working."""
        driver = build_from_options(
            tuner="quantify",
            **_transport(),
            data_dir=Path("/tmp/qpi-tuner"),
            options=Options({"data_dir": "/tmp/from-the-unit-file"}),
        )

        assert driver.tuner_options["data_dir"] == Path("/tmp/from-the-unit-file")

    def test_an_unsafe_data_dir_is_refused_by_the_name_it_was_set_under(self):
        with pytest.raises(ValueError, match="bad value for --data-dir"):
            build_from_options(
                tuner="quantify",
                **_transport(),
                data_dir=Path("/var"),
                options=Options({}),
            )

        with pytest.raises(ValueError, match="bad value for -o data_dir"):
            build_from_options(
                tuner="quantify", **_transport(), options=Options({"data_dir": "/var"})
            )

    def test_saving_raw_data_is_off_unless_asked_for(self):
        driver = build_from_options(
            tuner="quantify", **_transport(), options=Options({})
        )
        assert driver.tuner_options["save_raw_data"] is False

        driver = build_from_options(
            tuner="quantify",
            **_transport(),
            options=Options({"save_raw_data": "true"}),
        )
        assert driver.tuner_options["save_raw_data"] is True

    def test_is_simulated_reaches_the_tuner(self):
        """``-o is_simulated=true`` is what runs a node with no hardware at all."""
        driver = build_from_options(
            tuner="quantify",
            **_transport(),
            options=Options({"is_simulated": "true"}),
        )
        assert driver.tuner_options["is_simulated"] is True
        assert driver.tuner_options["is_dummy"] is False

    def test_build_from_options_needs_no_options_at_all(self):
        """Every option has a working default, so a tuner starts with no ``-o``."""
        driver = build_from_options(
            tuner="quantify", **_transport(), options=Options({})
        )

        assert driver.calibration_config_path == Path("./calibration.yml")
        assert driver.drift_check_interval == 0
        assert driver.tuner_options["quantify_device_config"] == Path(
            "./quantify.device.yml"
        )

    def test_an_option_nothing_reads_is_visible_as_unread(self):
        """Reading an option is what declares it (RFC 0003 §13.6).

        The CLI turns an unread option into a startup error; what this asserts is
        that the builder leaves it unread rather than swallowing it.
        """
        options = Options({"calibration_config": "./cal.yml", "no_such_option": "1"})
        build_from_options(tuner="quantify", **_transport(), options=options)

        assert options.unread() == ("no_such_option",)

    def test_both_tuner_devices_are_registered(self):
        registered = {spec.name for spec in devices(Operation.CALIBRATE)}
        assert registered == set(CALIBRATE_DEVICES)
        assert (
            resolve(Operation.CALIBRATE, "quantify_tuner").operation
            is Operation.CALIBRATE
        )

    def test_device_name_drops_the_tuner_suffix_to_name_its_tuner(self):
        """``quantify_tuner`` is the device; ``quantify`` is the tuner it runs."""
        spec = device_spec("quantify_tuner")
        assert spec.build.keywords["tuner"] == "quantify"


class TestEventHandling:
    """Which events a dispatch queues, and which it drops."""

    def test_a_dispatch_is_queued_for_the_worker(self):
        driver = _driver()
        driver.handle_event(
            Event(
                type=EventType.CALIBRATE_DISPATCH,
                payload={"job_id": "j1", "mode": "full"},
            )
        )
        assert driver._job_queue.items == [{"job_id": "j1", "mode": "full"}]

    def test_any_other_event_is_dropped(self):
        driver = _driver()
        driver.handle_event(
            Event(type=EventType.JOB_DISPATCH, payload={"job_id": "j1"})
        )
        assert driver._job_queue.items == []

    def test_a_second_calibration_is_refused_rather_than_queued(self, monkeypatch):
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


class TestTheDriftTimer:
    """The periodic drift check: when it is registered, and what it is judged against."""

    def test_no_drift_timer_is_registered_when_the_interval_is_zero(self):
        assert _driver(drift_check_interval=0)._periodic == []

    def test_the_drift_timer_is_registered_when_an_interval_is_given(self):
        driver = _driver(drift_check_interval=900)
        assert [interval for interval, _ in driver._periodic] == [900]

    def test_a_drift_check_carries_the_thresholds_it_is_judged_against(self):
        driver = _driver(
            drift_check_interval=900, fidelity_threshold=0.99, fidelity_2q_threshold=0.9
        )
        driver._check_fidelity()

        queued = driver._job_queue.items[0]
        assert queued["mode"] == "fidelity_check"
        assert queued["job_id"] == DRIFT_CHECK_JOB_ID
        assert queued["fidelity_threshold"] == 0.99
        assert queued["fidelity_2q_threshold"] == 0.9

    def test_a_drift_check_is_skipped_while_a_calibration_is_running(self):
        driver = _driver(drift_check_interval=900)
        driver._busy.set()
        driver._check_fidelity()
        assert driver._job_queue.items == []

    def test_a_drift_check_announces_itself_before_queueing(self, monkeypatch):
        """Nobody dispatched it, so QPI-UI has no row for it unless the driver says so.

        Before the job is queued, so the row exists before progress reports on it.
        """
        driver = _driver(drift_check_interval=900)
        emitted: list[Event] = []
        monkeypatch.setattr(driver, "emit", emitted.append)

        driver._check_fidelity()

        assert emitted[0].type is EventType.CALIBRATION_QUEUED
        assert emitted[0].payload == {
            "job_id": DRIFT_CHECK_JOB_ID,
            "mode": "fidelity_check",
            "target_qubits": [],
            "reason": "the drift timer",
        }

    def test_a_skipped_drift_check_announces_nothing(self, monkeypatch):
        driver = _driver(drift_check_interval=900)
        driver._busy.set()
        emitted: list[Event] = []
        monkeypatch.setattr(driver, "emit", emitted.append)

        driver._check_fidelity()

        assert emitted == []


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


class TestResultPump:
    """What the pump emits for a report, a worker error, and a drift follow-up."""

    def test_a_report_is_emitted_flat_not_nested(self, monkeypatch):
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

    def test_progress_is_emitted_without_ending_the_calibration(self, monkeypatch):
        """The driver stays busy: progress means still running, not finished.

        Treating it as an outcome would let a second calibration in behind one that
        has hours to go.
        """
        driver = _driver()
        driver._busy.set()
        emitted = _pump_once(
            driver,
            {
                "job_id": "j1",
                "progress": {
                    "mode": "full",
                    "step": 7,
                    "total": 33,
                    "routine": "rabi",
                    "target": "q2",
                },
            },
            monkeypatch,
        )

        assert emitted[0].type is EventType.CALIBRATION_PROGRESS
        assert emitted[0].payload == {
            "job_id": "j1",
            "mode": "full",
            "step": 7,
            "total": 33,
            "routine": "rabi",
            "target": "q2",
        }
        assert driver._busy.is_set()

    def test_a_worker_error_is_emitted_as_an_error(self, monkeypatch):
        driver = _driver()
        emitted = _pump_once(driver, {"job_id": "j1", "error": "boom"}, monkeypatch)
        assert emitted[0].payload["error"] == "boom"

    def test_a_drift_follow_up_is_queued_as_a_partial_recalibration(self, monkeypatch):
        driver = _driver()
        report = CalibrationReport(
            timestamp="t", duration_s=1.0, mode="fidelity_check", backend="stub"
        )
        emitted = _pump_once(
            driver,
            {
                "job_id": DRIFT_CHECK_JOB_ID,
                "report": report.to_event_payload(),
                "follow_up": [
                    {"mode": "partial", "job_id": "r1", "target_qubits": ["q0"]}
                ],
            },
            monkeypatch,
        )
        assert driver._job_queue.items == [
            {"mode": "partial", "job_id": "r1", "target_qubits": ["q0"]}
        ]
        # Announced too, and after the drift check's own result: a recalibration
        # nobody asked for is otherwise a QPU busy for hours with nothing to show why.
        queued = [e for e in emitted if e.type is EventType.CALIBRATION_QUEUED]
        assert queued[0].payload == {
            "job_id": "r1",
            "mode": "partial",
            "target_qubits": ["q0"],
            "reason": f"drift measured by {DRIFT_CHECK_JOB_ID}",
        }

    def test_a_plan_is_announced_again_rather_than_as_an_event_of_its_own(
        self, monkeypatch
    ):
        """The row is created at queue time and the plan is resolved in the worker.

        Two moments, two processes, one event type: the server's handler is already
        idempotent (RFC 0006 §5.1).
        """
        driver = _driver()
        driver._busy.set()
        plan = {"nodes": [{"name": "rabi", "planned": True}]}
        emitted = _pump_once(
            driver,
            {
                "job_id": "j1",
                "mode": "full",
                "plan": plan,
                "target_qubits": ["q0", "q1"],
            },
            monkeypatch,
        )

        assert emitted[0].type is EventType.CALIBRATION_QUEUED
        assert emitted[0].payload == {
            "job_id": "j1",
            "mode": "full",
            "target_qubits": ["q0", "q1"],
            "reason": "the walk it is about to make",
            "plan": plan,
        }
        # A plan is not an outcome; the calibration it describes has hours to go.
        assert driver._busy.is_set()

    def test_an_announcement_without_a_plan_carries_no_plan_key(self, monkeypatch):
        """A field the server does not know is ignored, but an empty one is a lie."""
        driver = _driver(drift_check_interval=900)
        emitted: list[Event] = []
        monkeypatch.setattr(driver, "emit", emitted.append)

        driver._check_fidelity()

        assert "plan" not in emitted[0].payload


class TestQueueingTheWalksUpdates:
    """How the worker tags what the walk tells it, on its way to the main process."""

    def test_a_position_is_queued_as_progress(self):
        queue = RecordingQueue()
        _queue_progress(queue, "j1", "full", {"step": 7, "total": 33})
        assert queue.items == [
            {"job_id": "j1", "progress": {"mode": "full", "step": 7, "total": 33}}
        ]

    def test_a_plan_is_queued_alongside_the_mode_that_resolved_it(self):
        queue = RecordingQueue()
        plan = {"nodes": []}
        _queue_progress(queue, "j1", "partial", {"plan": plan, "target_qubits": ["q0"]})
        assert queue.items == [
            {
                "job_id": "j1",
                "mode": "partial",
                "plan": plan,
                "target_qubits": ["q0"],
            }
        ]

    def test_a_drift_checks_plan_is_dropped_here(self):
        """Four disconnected benchmark nodes is not a picture (RFC 0006 D6)."""
        queue = RecordingQueue()
        _queue_progress(
            queue, DRIFT_CHECK_JOB_ID, "fidelity_check", {"plan": {"nodes": []}}
        )
        assert queue.items == []

    def test_a_drift_check_still_reports_its_position(self):
        queue = RecordingQueue()
        _queue_progress(queue, DRIFT_CHECK_JOB_ID, "fidelity_check", {"step": 2})
        assert queue.items[0]["progress"] == {"mode": "fidelity_check", "step": 2}


class _ConfigRecordingTuner(StubTuner):
    """StubTuner discards the config it is handed; these tests are about it."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.configs: list = []

    def calibrate(self, config):
        self.configs.append(config)
        return super().calibrate(config)


class TestTheWorker:
    """The worker loop: each dispatch mode, an unresolvable tuner, and a config edited between dispatches."""

    def test_each_mode_calls_its_tuner_method(self):
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

    def test_a_partial_without_targets_is_refused_not_widened_to_a_full_run(self):
        """Silently running a full calibration instead would take the QPU out for hours."""
        results = RecordingQueue()
        _execute_calibration(
            {"job_id": "a", "mode": "partial"},
            StubTuner(),
            CalibrationConfig(target_qubits=["q0"]),
            results,
        )
        assert "target_qubits" in results.items[0]["error"]

    def test_an_unknown_mode_is_reported(self):
        results = RecordingQueue()
        _execute_calibration(
            {"job_id": "a", "mode": "sideways"},
            StubTuner(),
            CalibrationConfig(),
            results,
        )
        assert "sideways" in results.items[0]["error"]

    def test_a_tuner_that_raises_is_reported_not_fatal(self):
        class Exploding(StubTuner):
            def calibrate(self, config):
                raise ValueError("no cluster")

        results = RecordingQueue()
        _execute_calibration(
            {"job_id": "a", "mode": "full"}, Exploding(), CalibrationConfig(), results
        )
        assert "no cluster" in results.items[0]["error"]

    def test_a_missing_calibration_config_is_an_init_error(self, tmp_path):
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

    def test_an_unresolvable_tuner_is_an_init_error(self, tmp_path):
        results = RecordingQueue()
        calibrate_worker(
            job_queue=_QueueOf([None]),
            result_queue=results,
            tuner="no_such_tuner",
            calibration_config_path=tmp_path / "cal.yml",
        )
        assert results.items[0]["job_id"] == "init_error"
        assert "Failed to resolve tuner" in results.items[0]["error"]

    def test_an_unparseable_calibration_config_is_an_init_error(self, tmp_path):
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

    def test_the_worker_runs_queued_jobs_until_the_poison_pill(self, tmp_path):
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

    def test_an_edited_calibration_config_takes_effect_on_the_next_dispatch(
        self, tmp_path
    ):
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

    def test_a_broken_edit_fails_that_calibration_and_leaves_the_worker_alive(
        self, tmp_path
    ):
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


class TestTheQpusServiceState:
    """A dispatched or drift-checked calibration, judged against the QPU's own online/maintenance/disabled state."""

    def test_a_drift_check_is_skipped_unless_the_qpu_is_online(self):
        """The one path no server-side gate can reach: it runs on the driver's clock."""
        for state, expected in (("online", 1), ("maintenance", 0), ("disabled", 0)):
            driver = _driver()
            driver._job_queue = RecordingQueue()
            driver.handle_event(
                Event(type=EventType.QPU_STATE, payload={"state": state}, driver="d")
            )

            driver._check_fidelity()

            assert len(driver._job_queue.items) == expected, (
                f"state {state} should have queued {expected} drift check(s)"
            )

    def test_a_dispatched_calibration_still_runs_under_maintenance(self):
        """Maintenance is the state a chip is in while someone works on it."""
        driver = _driver()
        driver._job_queue = RecordingQueue()
        driver.handle_event(
            Event(
                type=EventType.QPU_STATE, payload={"state": "maintenance"}, driver="d"
            )
        )

        driver.handle_event(
            Event(
                type=EventType.CALIBRATE_DISPATCH,
                payload={"job_id": "j1", "mode": "full"},
                driver="d",
            )
        )

        assert driver._job_queue.items == [{"job_id": "j1", "mode": "full"}]

    def test_a_dispatched_calibration_is_refused_on_a_switched_off_qpu(
        self, monkeypatch
    ):
        driver = _driver()
        driver._job_queue = RecordingQueue()
        emitted: list[Event] = []
        monkeypatch.setattr(driver, "emit", emitted.append)
        driver.handle_event(
            Event(type=EventType.QPU_STATE, payload={"state": "disabled"}, driver="d")
        )

        driver.handle_event(
            Event(
                type=EventType.CALIBRATE_DISPATCH,
                payload={"job_id": "j1", "mode": "full"},
                driver="d",
            )
        )

        assert driver._job_queue.items == []
        assert "switched off" in emitted[0].payload["error"]

    def test_a_driver_calibrates_before_it_is_ever_told_a_state(self):
        """A server that predates the event says nothing; that is not "disabled"."""
        driver = _driver()
        driver._job_queue = RecordingQueue()

        driver.handle_event(
            Event(
                type=EventType.CALIBRATE_DISPATCH,
                payload={"job_id": "j1", "mode": "full"},
                driver="d",
            )
        )

        assert driver._job_queue.items == [{"job_id": "j1", "mode": "full"}]


class TestLifecycle:
    """Starting and stopping the worker and its pump."""

    def test_start_spawns_a_worker_and_a_pump_then_stop_joins_them(self, tmp_path):
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

    def test_stop_is_safe_before_start(self, tmp_path):
        """SIGINT during the handshake means stopping something never started."""
        CalibrateDriver(
            tuner=StubTuner(), calibration_config=tmp_path / "cal.yml"
        )._on_stop()

    def test_stop_terminates_a_worker_that_will_not_exit(self, tmp_path):
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


class TestDriftDetection:
    """Which fidelities drift, and which protocol's threshold decides a target's worst case."""

    def test_nothing_drifts_when_every_fidelity_is_above_threshold(self):
        job = {"fidelity_threshold": 0.999, "fidelity_2q_threshold": 0.99}
        assert _drifted_targets(_report_with(q0=0.9995), job) == []

    def test_a_qubit_below_its_threshold_drifts(self):
        job = {"fidelity_threshold": 0.999, "fidelity_2q_threshold": 0.99}
        assert _drifted_targets(_report_with(q0=0.99, q1=0.9999), job) == ["q0"]

    def test_an_edge_is_judged_against_the_two_qubit_threshold(self):
        """A CZ an order of magnitude worse than a 1Q gate is normal, not drift."""
        job = {"fidelity_threshold": 0.999, "fidelity_2q_threshold": 0.99}
        assert _drifted_targets(_report_with(q0_q1=0.995), job) == []
        assert _drifted_targets(_report_with(q0_q1=0.95), job) == ["q0", "q1"]

    def test_the_worst_protocol_wins_for_a_target(self):
        """A drift check should fire on the worst evidence it has, not the best."""
        report = _report_with(q0=0.9999)
        report.add_benchmark(
            BenchmarkResult(
                protocol="allxy_check", target="q0", fidelity=0.90, error_per_gate=None
            )
        )
        assert report.fidelities()["q0"] == 0.90


class TestTunerResolution:
    """`resolve_tuner` over an instance, a class, and a name."""

    def test_resolve_tuner_accepts_an_instance_a_class_and_a_name(self):
        instance = StubTuner()
        assert resolve_tuner(instance) is instance
        assert isinstance(resolve_tuner(StubTuner), StubTuner)
        with pytest.raises(ValueError, match="Unknown tuner name"):
            resolve_tuner("nope")
        with pytest.raises(TypeError, match="Invalid tuner type"):
            resolve_tuner(42)

    def test_a_tuner_class_is_named_after_itself_when_unnamed(self):
        assert resolve_tuner(StubTuner).name == "stub"


class _RecordingAgent:
    """Stands in for a qblox `HardwareAgent`, recording how `run` was called."""

    def __init__(self):
        self.kwargs: dict = {}

    def run(self, schedule, **kwargs):
        self.kwargs = kwargs
        return schedule


class TestSavingRawData:
    """`-o save_raw_data=` decides whether the qblox agent keeps an acquisition.

    It has to be said explicitly, because the agent's own default is to save a
    dataset and an instrument snapshot per schedule.
    """

    def test_the_qblox_backend_saves_nothing_by_default(self):
        from qpi_driver.tuners.qblox import QbloxBackend

        agent = _RecordingAgent()
        QbloxBackend(agent).run("schedule")

        assert agent.kwargs["save_to_experiment"] is False
        assert agent.kwargs["save_snapshot"] is False

    def test_the_qblox_backend_saves_both_when_asked(self):
        from qpi_driver.tuners.qblox import QbloxBackend

        agent = _RecordingAgent()
        QbloxBackend(agent, should_save_raw_data=True).run("schedule")

        assert agent.kwargs["save_to_experiment"] is True
        assert agent.kwargs["save_snapshot"] is True


class _RecordingCoordinator:
    def __init__(self, hangs=False):
        self._hangs = hangs
        self.stopped = 0

    def prepare(self, compiled):
        pass

    def start(self):
        pass

    def wait_done(self, timeout_sec):
        self.timeout_sec = timeout_sec
        if self._hangs:
            raise TimeoutError(
                "Sequencer 0 did not stop in timeout period of 5 minutes"
            )

    def retrieve_acquisition(self):
        return "dataset"

    def stop(self):
        self.stopped += 1

    def components(self):
        return []


def _passthrough_compiler():
    return type("Compiler", (), {"compile": staticmethod(lambda schedule: schedule)})()


class _TimedSchedule:
    """A compiled schedule that knows how long its pulses take."""

    name = "sweep"

    def __init__(self, duration_s):
        self._duration_s = duration_s

    def get_schedule_duration(self):
        return self._duration_s


def _compiler_of(duration_s):
    return type(
        "Compiler",
        (),
        {"compile": staticmethod(lambda schedule: _TimedSchedule(duration_s))},
    )()


class TestRoutineTimeoutBoundsTheWait:
    """`routine_timeout_s` has to reach the instruments to mean anything.

    Its whole purpose is that a routine hanging on an instrument must not hang the
    worker, and the elapsed-time check in `_run_one` cannot do that on its own: it
    runs after `wait_done` has returned, so it reports a hang rather than ending
    one. The number is only a ceiling if the wait itself is given it.
    """

    def test_the_quantify_backend_waits_as_long_as_it_is_told(self):
        from qpi_driver.tuners.quantify import QuantifyBackend

        coordinator = _RecordingCoordinator()
        QuantifyBackend(_passthrough_compiler(), coordinator).run(
            "schedule", timeout_s=3600
        )

        assert coordinator.timeout_sec == 3600

    def test_the_quantify_backend_falls_back_when_told_nothing(self):
        from qpi_driver.tuners.quantify import QuantifyBackend

        coordinator = _RecordingCoordinator()
        QuantifyBackend(_passthrough_compiler(), coordinator).run("schedule")

        assert coordinator.timeout_sec == DEFAULT_ROUTINE_TIMEOUT_S

    def test_the_qblox_backend_passes_it_to_the_agent(self):
        from qpi_driver.tuners.qblox import QbloxBackend

        agent = _RecordingAgent()
        QbloxBackend(agent).run("schedule", timeout_s=3600)

        assert agent.kwargs["timeout"] == 3600

    def test_the_qblox_backend_falls_back_when_told_nothing(self):
        from qpi_driver.tuners.qblox import QbloxBackend

        agent = _RecordingAgent()
        QbloxBackend(agent).run("schedule")

        assert agent.kwargs["timeout"] == DEFAULT_ROUTINE_TIMEOUT_S


class TestEveryRunReleasesTheSyncNetwork:
    """Only `stop` clears `sync_en` on the modules a schedule did not use.

    `prepare` reaches the modules in the program and no others, so a module left in
    the cluster's sync network by an earlier node never arrives at `wait_sync` and
    every sequencer that does waits on it until the timeout — at any
    `routine_timeout_s`. The run that most needs stopping is the one that failed.
    """

    def test_the_quantify_backend_stops_after_a_good_run(self):
        from qpi_driver.tuners.quantify import QuantifyBackend

        coordinator = _RecordingCoordinator()
        QuantifyBackend(_passthrough_compiler(), coordinator).run("schedule")

        assert coordinator.stopped == 1

    def test_the_quantify_backend_stops_after_a_timeout(self):
        from qpi_driver.tuners.quantify import QuantifyBackend

        coordinator = _RecordingCoordinator(hangs=True)
        with pytest.raises(TimeoutError):
            QuantifyBackend(_passthrough_compiler(), coordinator).run("schedule")

        assert coordinator.stopped == 1

    def test_a_timeout_says_what_the_sequencers_were_doing(self):
        from qpi_driver.tuners.quantify import QuantifyBackend

        coordinator = _RecordingCoordinator(hangs=True)
        with pytest.raises(TimeoutError, match="did not stop.*sequencer"):
            QuantifyBackend(_passthrough_compiler(), coordinator).run("schedule")


class TestALongScheduleRaisesItsOwnCeiling:
    """`routine_timeout_s` bounds being *stuck*, not how large an experiment may be.

    A sweep whose pulses genuinely outlast the ceiling would otherwise fail for being
    big, and no ceiling an operator picks can be right for every routine — a punchout
    is 250x a time-of-flight on the same chip. The schedule's own duration therefore
    raises the wait, and never lowers it.
    """

    def test_a_schedule_longer_than_the_ceiling_gets_the_time_it_needs(self):
        from qpi_driver.tuners.quantify import QuantifyBackend

        coordinator = _RecordingCoordinator()
        QuantifyBackend(_compiler_of(1200.0), coordinator).run(
            "schedule", timeout_s=300
        )

        # Twenty whole minutes of pulses, plus one for upload and arming.
        assert coordinator.timeout_sec == 1260

    def test_the_allowance_survives_the_instrument_flooring_it_to_minutes(self):
        from qpi_driver.tuners.quantify import QuantifyBackend

        coordinator = _RecordingCoordinator()
        QuantifyBackend(_compiler_of(130.0), coordinator).run("schedule", timeout_s=60)

        # Passing 130 as-is would floor to two minutes and stop 10s short of the
        # schedule, which is the same failure with a subtler cause.
        assert coordinator.timeout_sec // 60 * 60 >= 130

    def test_a_short_schedule_never_lowers_the_ceiling(self):
        from qpi_driver.tuners.quantify import QuantifyBackend

        coordinator = _RecordingCoordinator()
        QuantifyBackend(_compiler_of(10.6), coordinator).run("schedule", timeout_s=300)

        assert coordinator.timeout_sec == 300

    def test_the_dag_judges_the_routine_by_what_the_backend_allowed(self):
        """Otherwise the long wait happens and the data is discarded anyway."""
        from qpi_driver.tuners.quantify import QuantifyBackend

        backend = QuantifyBackend(_compiler_of(1200.0), _RecordingCoordinator())
        backend.run("schedule", timeout_s=300)

        assert backend.last_allowance_s == 1260

    def test_a_backend_that_cannot_measure_its_schedule_leaves_the_ceiling_alone(self):
        from qpi_driver.tuners.quantify import QuantifyBackend

        coordinator = _RecordingCoordinator()
        backend = QuantifyBackend(_passthrough_compiler(), coordinator)
        backend.run("schedule", timeout_s=300)

        assert coordinator.timeout_sec == 300
        assert backend.last_allowance_s == 300
