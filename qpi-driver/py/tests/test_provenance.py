"""The provenance sidecar's round trip and its failure modes (RFC 0008 §9, tier 1).

Every test here is about the same property from a different angle: nothing may fail
because provenance is missing, malformed, or about parameters the device no longer has.
A device config is what runs circuits; this file only remembers where its numbers came
from, so its worst outcome must be forgetting rather than raising.
"""

from types import SimpleNamespace

import yaml

from qpi_driver.tuners.base.provenance import (
    PROVENANCE_SUFFIX,
    Provenance,
    ProvenanceStore,
    fit_summary,
    provenance_path,
)


def _measured(routine: str = "ramsey", **kwargs) -> Provenance:
    return Provenance(routine=routine, at="2026-08-12T09:22:17Z", **kwargs)


class TestWhereItLives:
    def test_it_sits_beside_the_device_config(self, tmp_path):
        assert provenance_path(tmp_path / "quantify.device.yml") == (
            tmp_path / f"quantify.device{PROVENANCE_SUFFIX}"
        )

    def test_a_tuner_with_no_device_config_gets_a_store_that_writes_nothing(
        self, tmp_path
    ):
        store = ProvenanceStore.load(None)
        store.record("q0", "clock_freqs.f01", _measured())

        store.save()

        assert list(tmp_path.iterdir()) == []


class TestRoundTrip:
    def test_what_is_recorded_is_what_is_read_back(self, tmp_path):
        device = tmp_path / "device.yml"
        store = ProvenanceStore.load(device)
        store.record(
            "q0", "clock_freqs.f01", _measured(run="job-1743", fit={"snr": 13.0})
        )
        store.save()

        reloaded = ProvenanceStore.load(device)

        recorded = reloaded.of("q0", "clock_freqs.f01")
        assert recorded == Provenance(
            routine="ramsey",
            at="2026-08-12T09:22:17Z",
            run="job-1743",
            fit={"snr": 13.0},
        )

    def test_an_unmeasured_parameter_is_a_prior(self, tmp_path):
        store = ProvenanceStore.load(tmp_path / "device.yml")
        store.record("q0", "clock_freqs.f01", _measured())

        assert store.is_measured("q0", "clock_freqs.f01")
        assert not store.is_measured("q0", "rxy.amp180")
        assert not store.is_measured("q9", "clock_freqs.f01")
        assert store.of("q0", "rxy.amp180") is None

    def test_the_file_is_keyed_by_target_then_dotted_path(self, tmp_path):
        """The shape is read by operators and by whatever watches the directory."""
        device = tmp_path / "device.yml"
        store = ProvenanceStore.load(device)
        store.record("q0", "clock_freqs.f01", _measured())
        store.save()

        raw = yaml.safe_load(provenance_path(device).read_text())

        assert raw == {
            "q0": {
                "clock_freqs.f01": {"routine": "ramsey", "at": "2026-08-12T09:22:17Z"}
            }
        }


class TestMergePerKey:
    def test_a_later_run_keeps_what_it_did_not_measure(self, tmp_path):
        """The property the whole file rests on: a partial run touches few parameters."""
        device = tmp_path / "device.yml"
        first = ProvenanceStore.load(device)
        first.record("q0", "clock_freqs.readout", _measured("resonator_spectroscopy"))
        first.record("q1", "clock_freqs.f01", _measured("qubit_spectroscopy"))
        first.save()

        second = ProvenanceStore.load(device)
        second.record("q0", "rxy.amp180", _measured("rabi"))
        second.save()

        final = ProvenanceStore.load(device)
        assert final.is_measured("q0", "clock_freqs.readout")
        assert final.is_measured("q1", "clock_freqs.f01")
        assert final.is_measured("q0", "rxy.amp180")

    def test_remeasuring_replaces_the_earlier_record(self, tmp_path):
        device = tmp_path / "device.yml"
        first = ProvenanceStore.load(device)
        first.record("q0", "clock_freqs.f01", _measured("qubit_spectroscopy"))
        first.save()

        second = ProvenanceStore.load(device)
        second.record("q0", "clock_freqs.f01", _measured("ramsey"))
        second.save()

        assert (
            ProvenanceStore.load(device).of("q0", "clock_freqs.f01").routine == "ramsey"
        )

    def test_a_concurrent_write_is_not_erased(self, tmp_path):
        """`save` re-reads before merging, so it cannot flatten what it never loaded."""
        device = tmp_path / "device.yml"
        store = ProvenanceStore.load(device)
        store.record("q0", "rxy.amp180", _measured("rabi"))

        other = ProvenanceStore.load(device)
        other.record("q0", "clock_freqs.f01", _measured("qubit_spectroscopy"))
        other.save()
        store.save()

        final = ProvenanceStore.load(device)
        assert final.is_measured("q0", "clock_freqs.f01")
        assert final.is_measured("q0", "rxy.amp180")


class TestNothingFailsForWantOfIt:
    def test_an_absent_file_makes_every_parameter_a_prior(self, tmp_path):
        store = ProvenanceStore.load(tmp_path / "device.yml")

        assert not store.is_measured("q0", "clock_freqs.f01")
        assert store.targets() == []

    def test_a_corrupt_file_makes_every_parameter_a_prior(self, tmp_path):
        device = tmp_path / "device.yml"
        provenance_path(device).write_text("q0: {clock_freqs.f01: [unclosed\n")

        store = ProvenanceStore.load(device)

        assert not store.is_measured("q0", "clock_freqs.f01")

    def test_a_file_that_is_not_a_mapping_makes_every_parameter_a_prior(self, tmp_path):
        device = tmp_path / "device.yml"
        provenance_path(device).write_text("- q0\n- q1\n")

        assert not ProvenanceStore.load(device).is_measured("q0", "clock_freqs.f01")

    def test_an_empty_file_makes_every_parameter_a_prior(self, tmp_path):
        device = tmp_path / "device.yml"
        provenance_path(device).write_text("")

        assert ProvenanceStore.load(device).targets() == []

    def test_a_record_naming_no_routine_attributes_nothing(self, tmp_path):
        device = tmp_path / "device.yml"
        provenance_path(device).write_text(
            yaml.safe_dump({"q0": {"clock_freqs.f01": {"at": "2026-08-12T09:22:17Z"}}})
        )

        assert not ProvenanceStore.load(device).is_measured("q0", "clock_freqs.f01")

    def test_a_target_the_device_no_longer_has_is_read_and_ignored(self, tmp_path):
        """A retired qubit leaves records behind; asking about a live one still works."""
        device = tmp_path / "device.yml"
        first = ProvenanceStore.load(device)
        first.record("q7", "clock_freqs.f01", _measured())
        first.record("q0", "clock_freqs.f01", _measured())
        first.save()

        store = ProvenanceStore.load(device)

        assert store.is_measured("q0", "clock_freqs.f01")
        assert store.is_measured("q7", "clock_freqs.f01")

    def test_a_partial_record_reads_as_far_as_it_goes(self, tmp_path):
        device = tmp_path / "device.yml"
        provenance_path(device).write_text(
            yaml.safe_dump({"q0": {"clock_freqs.f01": {"routine": "ramsey"}}})
        )

        recorded = ProvenanceStore.load(device).of("q0", "clock_freqs.f01")

        assert recorded.routine == "ramsey"
        assert recorded.at == ""
        assert recorded.fit == {}

    def test_an_unwritable_path_does_not_raise(self, tmp_path):
        device = tmp_path / "nowhere" / "device.yml"
        device.parent.mkdir()
        device.parent.chmod(0o500)
        store = ProvenanceStore.load(device)
        store.record("q0", "clock_freqs.f01", _measured())
        try:
            store.save()
        finally:
            device.parent.chmod(0o700)


class TestFitSummary:
    def test_it_keeps_the_numbers_a_guard_judged(self):
        summary = fit_summary(
            {
                "snr": 13.02499,
                "reach": 194.0,
                "contrast": 0.4,
                "separation": 2.0,
                "frequencies": [1, 2, 3],
                "magnitudes": [0.1, 0.2],
                "centre": 4.4e9,
            }
        )

        assert summary == {
            "snr": 13.025,
            "reach": 194.0,
            "contrast": 0.4,
            "separation": 2.0,
        }

    def test_it_drops_infinities_and_non_numbers(self):
        """`snr` is infinite when a fit had no residual scatter, and YAML `.inf` travels badly."""
        assert (
            fit_summary(
                {"snr": float("inf"), "reach": float("nan"), "contrast": "high"}
            )
            == {}
        )

    def test_a_routine_that_reported_no_fit_summarises_to_nothing(self):
        assert fit_summary(None) == {}


class TestARefusedRoutineIsNotAttributedAParameter:
    """The gate RFC 0008 §7 asks for: a parameter is attributable when its producer
    succeeded, its guards passed, and its value was persisted.

    It used to be positional — the DAG only appended a `RoutineResult` on the success
    path, so iterating them was the check. The DAG now appends one on the failure path
    too, carrying the sweep a guard refused, so the gate is `RoutineResult.failed` and
    has to be tested rather than assumed.
    """

    def _tuner(self, tmp_path):
        """Enough of a `Tuner` for `_record_provenance`, which is called unbound below.

        A real one needs instruments; this needs a device config path, the routines, and
        an element that *does* carry the path — so the only thing that can stop the record
        is the flag under test.
        """
        from qpi_driver.tuners.routines import all_routines

        element = SimpleNamespace(clock_freqs=SimpleNamespace(f01=5.0e9))
        return SimpleNamespace(
            _device_config_path=tmp_path / "device.yml",
            device=SimpleNamespace(get_element=lambda _name: element),
            routines=all_routines,
        )

    def _run(self, tmp_path, *, failed: bool):
        from qpi_driver.tuners.base import Tuner
        from qpi_driver.tuners.base.report import CalibrationReport, RoutineResult

        tuner = self._tuner(tmp_path)
        report = CalibrationReport(timestamp="run-1", duration_s=0.0, mode="full")
        report.add_routine(
            RoutineResult(
                routine_name="ramsey",
                target="q0",
                parameters={} if failed else {"clock_freq_01": 5.0e9},
                timestamp="2026-08-14T00:44:51Z",
                duration_s=1.0,
                fit={"x": [1.0], "measured": [2.0], "fitted": [2.0]},
                failed=failed,
            )
        )
        Tuner._record_provenance(tuner, report)
        return ProvenanceStore.load(tmp_path / "device.yml").of("q0", "clock_freqs.f01")

    def test_a_refused_result_records_nothing(self, tmp_path):
        assert self._run(tmp_path, failed=True) is None

    def test_the_same_result_that_succeeded_is_recorded(self, tmp_path):
        """The positive control: what stops attribution is the flag, not the empty
        parameters or anything else incidental to how a refusal is shaped."""
        assert self._run(tmp_path, failed=False).routine == "ramsey"
