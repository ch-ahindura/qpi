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
from statistics import mean

import numpy as np
import pytest
import yaml
from qpi_driver.builtins.calibrate import _execute_calibration
from qpi_driver.tuners.base.config import CalibrationConfig
from qpi_driver.tuners.base.device import read_path
from qpi_driver.tuners.base.provenance import ProvenanceStore, provenance_path
from qpi_driver.tuners.routines import routine_names
from qpi_driver.tuners.utils.clifford import clifford_to_gates

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


class TestFidelityAgainstWhatTheSimulatorInjected:
    """RFC 0007 §12: what "high fidelity" means in an acceptance test.

    A constant threshold cannot say it. Set it low enough that the simulated chip's own
    gate error dominates and it passes whatever the calibration did; set it high enough to
    mean something and it pins the test to how the simulator happens to be tuned, so
    retuning the model breaks a test about the driver.

    So assert against the error the simulator was *given*, as
    `test_rb_recovers_a_known_gate_error` does one tier down: a calibration good enough to
    benchmark recovers the injected error, and one that left a gate miscalibrated reports
    worse than it. Both hold at any injected level, so neither depends on the tuning.
    """

    def test_rb_recovers_the_injected_error_after_calibrating(self, tmp_path):
        injected = 0.02
        tuner = SimulatedTuner(
            gate_error=injected, device_config_path=tmp_path / "device.yml"
        )

        report = tuner.calibrate(write_calibration_config(tmp_path))

        assert report.status == "success", report.errors
        assert _rb_error_per_gate(report) == pytest.approx(
            _average_gate_error(injected), rel=0.15
        )

    def test_a_worse_chip_benchmarks_worse(self, tmp_path):
        """The absolute number could be luck; that it tracks the model cannot."""
        measured = {}
        for injected in (0.004, 0.05):
            room = tmp_path / f"p{injected}"
            room.mkdir()
            tuner = SimulatedTuner(
                gate_error=injected, device_config_path=room / "device.yml"
            )
            report = tuner.calibrate(write_calibration_config(room))
            assert report.status == "success", report.errors
            measured[injected] = _rb_error_per_gate(report)

        assert measured[0.004] < measured[0.05]
        for injected, error in measured.items():
            assert error == pytest.approx(_average_gate_error(injected), rel=0.15)


def _average_gate_error(per_primitive: float) -> float:
    """The error per Clifford `rb` should report, given the per-gate error injected.

    The simulator depolarises once per primitive rotation, so a Clifford of n of them
    decays by 1-(1-p)**n; averaged over the Clifford group that is an average gate error
    of half as much, which is the p·(d-1)/d that `fit_rb_decay` inverts with d = 2.
    """
    primitives = mean(len(clifford_to_gates(clifford)) for clifford in range(24))
    return 0.5 * (1.0 - (1.0 - per_primitive) ** primitives)


def _rb_error_per_gate(report) -> float:
    errors = [
        benchmark.error_per_gate
        for benchmark in report.benchmarks
        if benchmark.protocol == "rb" and benchmark.error_per_gate is not None
    ]
    assert errors, f"rb reported no error, only {report.benchmarks}"
    return errors[0]


class TestProvenanceFromARealWalk:
    """RFC 0008 §9 tier 2: provenance for what a walk measured, and for nothing else.

    A record that claims a parameter was measured when it was not is worse than no record,
    because the whole file exists to be trusted on that one question. So both directions
    are asserted here against a walk that really ran, rather than against `updates`, which
    is a declaration and is what the recording is derived *from*.
    """

    def test_it_records_every_parameter_the_walk_wrote_and_no_others(
        self, tmp_path, monkeypatch
    ):
        device_path = tmp_path / "quantify.device.yml"
        tuner = SimulatedTuner(device_config_path=device_path)
        written = _recording_writes(monkeypatch)

        report = tuner.calibrate(write_calibration_config(tmp_path))

        assert report.status == "success", report.errors
        store = ProvenanceStore.load(device_path)
        recorded = {
            (target, path) for target in store.targets() for path in store.paths(target)
        }
        assert recorded == written
        assert recorded, "a successful walk recorded nothing"

    def test_the_record_names_the_routine_that_measured_it(self, tmp_path):
        device_path = tmp_path / "quantify.device.yml"
        tuner = SimulatedTuner(device_config_path=device_path)

        report = tuner.calibrate(write_calibration_config(tmp_path))

        store = ProvenanceStore.load(device_path)
        # f01 has two writers: `qubit_spectroscopy` produces it and `ramsey` refines it, so
        # the record must name the refiner — the last routine to measure it, not the first.
        f01 = store.of("q0", "clock_freqs.f01")
        assert f01.routine == "ramsey"
        assert f01.run == report.timestamp
        assert f01.at in {r.timestamp for r in report.routine_results}
        assert store.of("q0", "rxy.amp180").routine == "rabi"

    def test_a_benchmark_records_nothing_because_it_calibrates_nothing(self, tmp_path):
        device_path = tmp_path / "quantify.device.yml"
        tuner = SimulatedTuner(device_config_path=device_path)

        tuner.calibrate(write_calibration_config(tmp_path))

        store = ProvenanceStore.load(device_path)
        recorded = {store.of("q0", path).routine for path in store.paths("q0")}
        assert "rb" not in recorded
        assert "t1" not in recorded  # measures a number nothing is tuned from

    def test_a_failed_routine_leaves_no_provenance(self, tmp_path):
        """The property the rest rests on: only a measurement is attributable."""
        device_path = tmp_path / "quantify.device.yml"
        tuner = SimulatedTuner(device_config_path=device_path)
        config = write_calibration_config(tmp_path)
        # A one-point sweep cannot fit a cosine, so `rabi` fails while its upstream
        # succeeds — and every node reading amp180 is then skipped, per RFC 0007 §11.
        config.routines["rabi"].params["amplitudes"] = [0.1]

        report = tuner.calibrate(config)

        assert report.status == "partial_failure", report.status
        store = ProvenanceStore.load(device_path)
        assert not store.is_measured("q0", "rxy.amp180")
        assert store.is_measured("q0", "clock_freqs.f01")

    def test_a_failed_run_records_nothing(self, tmp_path):
        """Provenance describes what is in the file, so it cannot outrun the write-back."""
        device_path = tmp_path / "quantify.device.yml"
        tuner = SimulatedTuner(device_config_path=device_path)
        config = write_calibration_config(tmp_path, enabled=("rabi",))
        config.routines["rabi"].params["amplitudes"] = [0.1]

        report = tuner.calibrate(config)

        assert report.status == "failed", report.status
        assert not provenance_path(device_path).exists()

    def test_a_failed_write_back_records_nothing(self, tmp_path, monkeypatch):
        """The values never reached the file, so nothing about them is attributable."""
        from qpi_driver.tuners import base as base_mod

        device_path = tmp_path / "quantify.device.yml"
        tuner = SimulatedTuner(device_config_path=device_path)
        monkeypatch.setattr(base_mod, "save_device_config", _refusing("disk full"))

        report = tuner.calibrate(write_calibration_config(tmp_path))

        assert report.status == "partial_failure"
        assert any("write-back failed" in error for error in report.errors)
        assert not provenance_path(device_path).exists()

    def test_a_second_walk_keeps_what_the_first_measured(self, tmp_path):
        """Merge per key over a real walk, not just over the store's own unit tests."""
        device_path = tmp_path / "quantify.device.yml"
        tuner = SimulatedTuner(device_config_path=device_path)
        tuner.calibrate(write_calibration_config(tmp_path))
        first = ProvenanceStore.load(device_path)
        before = {(t, p) for t in first.targets() for p in first.paths(t)}

        tuner.calibrate(write_calibration_config(tmp_path, enabled=("rabi",)))

        second = ProvenanceStore.load(device_path)
        after = {(t, p) for t in second.targets() for p in second.paths(t)}
        assert before <= after
        assert second.of("q0", "rxy.amp180").run != first.of("q0", "rxy.amp180").run


def _refusing(message: str):
    def refuse(*_args, **_kwargs):
        raise OSError(message)

    return refuse


def _recording_writes(monkeypatch) -> set[tuple[str, str]]:
    """``(target, dotted path)`` for every device write, as the walk makes them.

    The same instrumentation `test_a_routine_declares_every_parameter_it_reads` uses for
    the other direction, and for the same reason: `write_path` is the one funnel every
    `apply` goes through, so patching it observes what was really written rather than what
    a routine said it would write.
    """
    from qpi_driver.tuners.base import device as device_mod
    from qpi_driver.tuners.routines import ef, readout, single_qubit, spectroscopy
    from qpi_driver.tuners.routines import two_qubit

    written: set[tuple[str, str]] = set()
    original = device_mod.write_path

    def recording(component, dotted, value):
        written.add((getattr(component, "name", "?"), dotted))
        return original(component, dotted, value)

    for module in (device_mod, ef, readout, single_qubit, spectroscopy, two_qubit):
        if hasattr(module, "write_path"):
            monkeypatch.setattr(module, "write_path", recording)
    return written


class TestWhatTheReportSaysAboutItsInputs:
    """RFC 0008 §9 tier 3 and its regression test: a prior is visible before it costs a run.

    Report-only. Nothing here changes what runs — that is phase 4 — so every assertion is
    about what an operator reading the run can now see and could not before.
    """

    def test_a_precise_looking_frequency_nothing_measured_is_reported_as_a_prior(
        self, tmp_path
    ):
        """The August 2026 failure, written down.

        That chip's config held `clock_freqs.f01: 4735509751.238763`. Nine significant
        figures, so it read as a measurement, and the qubit was 302 MHz away — the line had
        never been there. Six runs went into the consequences, because nothing in the report
        distinguished that number from one this driver had fitted.
        """
        tuner = SimulatedTuner(device_config_path=tmp_path / "device.yml")
        seeded = read_path(tuner.device.get_element("q0"), "clock_freqs.f01")
        assert len(f"{seeded:.0f}") >= 9, "the fixture should look like a measurement"

        report = tuner.calibrate(write_calibration_config(tmp_path))

        assert any(
            "clock_freqs.f01" in note and "not measured" in note
            for note in report.notes
        ), report.notes
        spectroscopy = _result_for(report, "qubit_spectroscopy")
        assert "clock_freqs.f01" in spectroscopy.priors

    def test_a_second_walk_finds_the_first_walks_parameters_attributable(
        self, tmp_path
    ):
        device_path = tmp_path / "device.yml"
        tuner = SimulatedTuner(device_config_path=device_path)
        tuner.calibrate(write_calibration_config(tmp_path))

        second = tuner.calibrate(write_calibration_config(tmp_path))

        assert second.notes == []
        assert all(result.priors == () for result in second.routine_results)

    def test_deleting_the_sidecar_calibrates_identically_and_reports_priors_again(
        self, tmp_path
    ):
        """Safe to delete. Forgetting where a value came from must not change what runs."""
        device_path = tmp_path / "device.yml"
        tuner = SimulatedTuner(device_config_path=device_path)
        tuner.calibrate(write_calibration_config(tmp_path))
        with_memory = tuner.calibrate(write_calibration_config(tmp_path))

        provenance_path(device_path).unlink()
        forgetful = tuner.calibrate(write_calibration_config(tmp_path))

        assert forgetful.status == with_memory.status == "success"
        assert [r.routine_name for r in forgetful.routine_results] == [
            r.routine_name for r in with_memory.routine_results
        ]
        assert _result_for(forgetful, "qubit_spectroscopy").priors == (
            "clock_freqs.f01",
        )

    def test_priors_stay_out_of_the_wire_payload(self, tmp_path):
        """One contract written twice, as `CalibrationReport.notes` already is."""
        tuner = SimulatedTuner(device_config_path=tmp_path / "device.yml")

        report = tuner.calibrate(write_calibration_config(tmp_path))

        assert _result_for(report, "qubit_spectroscopy").priors
        payload = report.to_event_payload()
        assert all("priors" not in result for result in payload["routine_results"])
        assert "notes" not in payload

    def test_a_chip_with_no_device_config_still_calibrates(self, tmp_path):
        """No path means no sidecar to read or write, and must mean no difference."""
        report = SimulatedTuner().calibrate(write_calibration_config(tmp_path))

        assert report.status == "success", report.errors


def _result_for(report, routine_name: str):
    for result in report.routine_results:
        if result.routine_name == routine_name:
            return result
    raise AssertionError(f"{routine_name} produced no result in {report.summary()}")
