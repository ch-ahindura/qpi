"""A whole calibration, end to end, against the simulator (RFC 0004 §7).

The other tests take a calibration apart. `test_calibration_dag.py` walks the
graph with stub routines, `test_tuner_routines.py` builds schedules against a
dummy cluster, `test_physics_simulation.py` checks each fit against physics —
and every one of them replaces the pieces on either side of the one it is
testing.

These put it back together. `_execute_calibration` is the calibrate worker's
entry point, so a job dict handed to it here goes through exactly what it goes
through in the driver's subprocess: the tuner, the DAG walk, every routine's
build/run/fit/apply, the write-back to the device YAML, and the report that
comes out the other end. What that covers is the joins — the drift check's
follow-up job being a job the worker can actually run, a fitted frequency
reaching the file the `process` driver reads — which is where the faults a unit
test cannot see actually live.

Needs the `sim` extra and no scheduler extra:

    make test-py-sim
"""

from pathlib import Path

import numpy as np
import pytest
import yaml
from qpi_driver.builtins.calibrate import _execute_calibration
from qpi_driver.tuners.base.config import CalibrationConfig
from qpi_driver.tuners.base.device import read_path
from qpi_driver.tuners.routines import routine_names

pytest.importorskip("scqubits", reason="needs the [sim] extra")
pytest.importorskip("qutip", reason="needs the [sim] extra")

from tests.utils.simulation import GHZ, SimulatedTuner  # noqa: E402

pytestmark = pytest.mark.scqubits

#: The routines the simulator has physics for. Everything else is disabled in
#: these configs — a routine whose acquisition the backend cannot produce would
#: fail, and a calibration that half-fails tests the error path rather than the
#: path these tests are about.
SIMULATED = ("qubit_spectroscopy", "rabi", "ramsey", "t1", "t2_echo", "rb")


class RecordingQueue:
    """Stands in for the worker's result queue; only ``put`` is ever called."""

    def __init__(self) -> None:
        self.items: list[dict] = []

    def put(self, item: dict) -> None:
        self.items.append(item)


def _floats(values) -> list[float]:
    """Plain floats — ``yaml.safe_dump`` will not emit numpy scalars."""
    return [float(v) for v in values]


def write_calibration_config(
    tmp_path: Path, qubits=("q0",), enabled=SIMULATED
) -> CalibrationConfig:
    """A ``calibration.yml`` on disk, loaded the way the worker loads it.

    The sweeps are narrower than a real calibration's. Each is still wide enough
    that its fit has to find something — the frequency window brackets a line
    the device is 3 MHz off from, the Rabi sweep covers more than one period —
    which is the property that makes the run meaningful, rather than the number
    of points.
    """
    sweeps = {
        "qubit_spectroscopy": {"span": 30e6, "points": 41},
        "rabi": {"amplitudes": _floats(np.linspace(0.0, 0.5, 41))},
        "ramsey": {
            "delays": _floats(np.linspace(4e-9, 6e-6, 41)),
            "artificial_detuning": 1e6,
        },
        "t1": {"delays": _floats(np.linspace(0.0, 80e-6, 21))},
        "t2_echo": {"delays": _floats(np.linspace(0.0, 60e-6, 21))},
        # Depths that reach far enough for the decay to be visible. RB cannot
        # resolve an error much smaller than 1/max(depth), which is a property
        # of the protocol rather than of this simulator.
        "rb": {"depths": [1, 2, 4, 8, 16, 32, 64], "circuits_per_depth": 8},
    }
    data = {
        "target_qubits": list(qubits),
        "routines": {
            name: dict(sweeps.get(name, {})) if name in enabled else {"enabled": False}
            for name in sorted(routine_names())
        },
    }
    path = tmp_path / "calibration.yml"
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return CalibrationConfig.from_yaml(path)


def run_job(job: dict, tuner: SimulatedTuner, config: CalibrationConfig) -> dict:
    """One calibration through the worker's own entry point.

    The queue also carries the plan and a progress update per routine and target;
    the outcome is the one item that is neither, and there is still exactly one of
    those.
    """
    queue = RecordingQueue()
    _execute_calibration(job, tuner, config, queue)
    outcomes = [
        item for item in queue.items if "progress" not in item and "plan" not in item
    ]
    assert len(outcomes) == 1, queue.items
    return outcomes[0]


class TestAFullCalibration:
    """The whole path from a job dict to a device file, and coherence measured but not tuned."""

    def test_a_full_calibration_walks_the_graph_and_writes_the_device_back(
        self, tmp_path
    ):
        """The whole path, from a job dict to a device file the QPU driver can read."""
        device_path = tmp_path / "quantify.device.yml"
        tuner = SimulatedTuner(device_config_path=device_path)
        config = write_calibration_config(tmp_path)

        result = run_job({"mode": "full", "job_id": "job-1"}, tuner, config)

        assert "error" not in result, result
        report = result["report"]
        assert report["status"] == "success", report["errors"]
        assert report["mode"] == "full"
        assert {r["routine_name"] for r in report["routine_results"]} == set(SIMULATED)

        # The device started 3 MHz off the true line, and the calibration moved it on.
        true_f01 = tuner.simulator.f01 * GHZ
        element = tuner.device.get_element("q0")
        assert read_path(element, "clock_freqs.f01") == pytest.approx(true_f01, abs=2e5)
        assert read_path(element, "rxy.amp180") == pytest.approx(0.2, rel=0.05)

        # And it reached the file every later job is compiled against, which is the
        # only reason any of the above matters.
        written = yaml.safe_load(device_path.read_text())
        assert written["q0"]["clock_freqs"]["f01"] == pytest.approx(true_f01, abs=2e5)
        assert written["q0"]["rxy"]["amp180"] == pytest.approx(0.2, rel=0.05)

    def test_progress_reaches_the_queue_as_the_walk_proceeds(self, tmp_path):
        """The worker wires a real tuner's walk to the queue the driver emits from.

        Asserted here rather than against a stub because what could break is the
        wiring — `_execute_calibration` setting `on_progress` on the tuner it was
        handed, and the base `calibrate` passing it into the DAG.
        """
        tuner = SimulatedTuner()
        config = write_calibration_config(tmp_path)

        queue = RecordingQueue()
        _execute_calibration({"mode": "full", "job_id": "job-1"}, tuner, config, queue)

        reported = [item for item in queue.items if "progress" in item]
        updates = [item["progress"] for item in reported]
        assert [u["routine"] for u in updates] == [
            r["routine_name"] for r in queue.items[-1]["report"]["routine_results"]
        ]
        assert all(item["job_id"] == "job-1" for item in reported)
        assert all(u["mode"] == "full" and u["target"] == "q0" for u in updates)
        # Monotonic, and ending on the last step of the walk.
        assert [u["step"] for u in updates] == sorted(u["step"] for u in updates)
        assert updates[-1]["step"] == updates[-1]["total"]
        assert updates[-1]["succeeded"] == len(updates)

    def test_the_plan_describes_the_walk_the_report_then_records(self, tmp_path):
        """A real walk, so the plan's nodes and the report's results cannot disagree."""
        tuner = SimulatedTuner()
        config = write_calibration_config(tmp_path)

        queue = RecordingQueue()
        _execute_calibration({"mode": "full", "job_id": "job-1"}, tuner, config, queue)

        plans = [item for item in queue.items if "plan" in item]
        assert len(plans) == 1
        assert plans[0]["job_id"] == "job-1"
        assert plans[0]["target_qubits"] == ["q0"]

        nodes = plans[0]["plan"]["nodes"]
        planned = [n["name"] for n in nodes if n["planned"] and n["targets"]]
        report = queue.items[-1]["report"]
        assert planned == [r["routine_name"] for r in report["routine_results"]], (
            "the plan promised a walk the report did not make"
        )
        # Every routine is described, walked or not — the drawing shows both.
        assert set(routine_names()) == {n["name"] for n in nodes}

    def test_a_drift_check_publishes_no_plan(self, tmp_path):
        """Four disconnected benchmark nodes is not a picture (RFC 0006 D6)."""
        tuner = SimulatedTuner()
        config = write_calibration_config(tmp_path)

        queue = RecordingQueue()
        _execute_calibration(
            {"mode": "fidelity_check", "job_id": "drift_check"}, tuner, config, queue
        )

        assert not any("plan" in item for item in queue.items)

    def test_a_full_calibration_measures_coherence_it_does_not_tune(self, tmp_path):
        """T1 and T2 write nothing, so their value is only in the report."""
        tuner = SimulatedTuner()
        config = write_calibration_config(tmp_path)

        report = run_job({"mode": "full", "job_id": "job-1"}, tuner, config)["report"]
        measured = {
            r["routine_name"]: r["parameters"]
            for r in report["routine_results"]
            if r["routine_name"] in ("t1", "t2_echo")
        }

        assert measured["t1"]["t1"] == pytest.approx(
            tuner.simulator.t1_ns * 1e-9, rel=0.05
        )
        assert measured["t2_echo"]["t2"] == pytest.approx(
            tuner.simulator.t2_ns * 1e-9, rel=0.1
        )


class TestTheDriftCheckAndWhatItQueues:
    """A fidelity check that benchmarks without moving the calibration, and what it queues for the worker to run."""

    def test_a_fidelity_check_benchmarks_without_moving_the_calibration(self, tmp_path):
        tuner = SimulatedTuner()
        config = write_calibration_config(tmp_path)
        element = tuner.device.get_element("q0")
        before = (
            read_path(element, "clock_freqs.f01"),
            read_path(element, "rxy.amp180"),
        )

        report = run_job(
            {"mode": "fidelity_check", "job_id": "drift_check"}, tuner, config
        )["report"]

        assert report["mode"] == "fidelity_check"
        assert [r["routine_name"] for r in report["routine_results"]] == ["rb"]
        assert [b["protocol"] for b in report["benchmarks"]] == ["rb"]
        assert 0.9 < report["benchmarks"][0]["fidelity"] <= 1.0

        after = (
            read_path(element, "clock_freqs.f01"),
            read_path(element, "rxy.amp180"),
        )
        assert after == before, "a fidelity check measures; it does not calibrate"

    def test_drift_queues_a_follow_up_the_worker_can_actually_run(self, tmp_path):
        """The join that matters most: what the drift check emits is a runnable job.

        A follow-up whose shape the worker rejects would leave a chip that reported
        drift and then did nothing about it — and nothing in a unit test of either
        side would say so, because each side is correct on its own.
        """
        tuner = SimulatedTuner(
            gate_error=0.02, device_config_path=tmp_path / "device.yml"
        )
        config = write_calibration_config(tmp_path)
        check = {
            "mode": "fidelity_check",
            "job_id": "drift_check",
            "fidelity_threshold": 0.999,
            "fidelity_2q_threshold": 0.99,
        }

        checked = run_job(check, tuner, config)
        assert checked["report"]["benchmarks"][0]["fidelity"] < 0.999
        assert checked["follow_up"] == [
            {
                "mode": "partial",
                "job_id": "drift_check_recalibrate",
                "target_qubits": ["q0"],
            }
        ]

        recalibrated = run_job(checked["follow_up"][0], tuner, config)
        assert "error" not in recalibrated, recalibrated
        assert recalibrated["report"]["mode"] == "partial"
        assert recalibrated["report"]["status"] == "success", recalibrated["report"][
            "errors"
        ]

    def test_a_healthy_chip_queues_nothing(self, tmp_path):
        """The control: the follow-up must come from the measurement, not from the mode.

        A gate error of 0.04% is a good transmon rather than a perfect one, so this
        also pins down that the measured fidelity tracks the injected error instead
        of saturating somewhere below the threshold — which is exactly what it used
        to do, and would have made the test above pass for the wrong reason.
        """
        tuner = SimulatedTuner(gate_error=0.0004)
        config = write_calibration_config(tmp_path)

        checked = run_job(
            {
                "mode": "fidelity_check",
                "job_id": "drift_check",
                "fidelity_threshold": 0.999,
            },
            tuner,
            config,
        )
        assert checked["report"]["benchmarks"][0]["fidelity"] > 0.999
        assert "follow_up" not in checked


class TestPartialRecalibration:
    """Narrowing the targets, which is most of why a partial run is cheaper than a full one."""

    def test_a_partial_recalibration_leaves_the_other_qubits_alone(self, tmp_path):
        """Narrowing the targets is most of why a partial run is cheaper than a full one."""
        tuner = SimulatedTuner(qubits=("q0", "q1"))
        config = write_calibration_config(tmp_path, qubits=("q0", "q1"))

        report = run_job(
            {"mode": "partial", "job_id": "job-2", "target_qubits": ["q0"]},
            tuner,
            config,
        )["report"]

        assert report["status"] == "success", report["errors"]
        assert {r["target"] for r in report["routine_results"]} == {"q0"}

        untouched = tuner.device.get_element("q1")
        assert read_path(untouched, "rxy.amp180") == 0.18
        assert read_path(untouched, "clock_freqs.f01") == pytest.approx(
            tuner.simulator.f01 * GHZ + 3e6
        )

    def test_a_partial_recalibration_with_no_targets_is_refused(self, tmp_path):
        """Running a full calibration instead would be hours nobody asked for."""
        tuner = SimulatedTuner()
        config = write_calibration_config(tmp_path)

        result = run_job({"mode": "partial", "job_id": "job-3"}, tuner, config)

        assert "needs target_qubits" in result["error"]
        assert "report" not in result

    def test_an_unknown_mode_is_refused(self, tmp_path):
        tuner = SimulatedTuner()
        config = write_calibration_config(tmp_path)

        result = run_job(
            {"mode": "recalibrate-everything", "job_id": "job-4"}, tuner, config
        )

        assert "unknown calibration mode" in result["error"]


class TestTheWriteBackIsATrustBoundary:
    """RFC 0004 §10: a bad write-back does not fail the calibration, it fails every job after it."""

    def test_a_calibration_that_fails_outright_leaves_the_device_file_alone(
        self, tmp_path
    ):
        """RFC 0004 §10: a bad write-back does not fail the calibration, it fails every job after it.

        ``allxy`` has no simulated acquisition, so enabling it alone makes every
        routine in the walk fail — which is the case where the in-memory device may
        hold half-applied parameters and must not reach the file.
        """
        device_path = tmp_path / "quantify.device.yml"
        device_path.write_text("q0: {this is the good config}\n")
        tuner = SimulatedTuner(device_config_path=device_path)
        config = write_calibration_config(tmp_path, enabled=("allxy",))

        report = run_job({"mode": "full", "job_id": "job-5"}, tuner, config)["report"]

        assert report["status"] == "failed"
        assert report["routine_results"] == []
        assert device_path.read_text() == "q0: {this is the good config}\n"
        assert not device_path.with_suffix(".yml.prev").exists()
