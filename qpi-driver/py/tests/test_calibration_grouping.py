"""Which targets may be measured at once (RFC 0009 §5)."""

import logging
from types import SimpleNamespace

import pytest

from qpi_driver.tuners.base.config import (
    DEFAULT_EDGE_SPACING,
    DEFAULT_MAX_GROUP,
    DEFAULT_QUBIT_SPACING,
    CalibrationConfig,
    ConfigError,
    ParallelConfig,
)
from qpi_driver.tuners.base.dag import CalibrationDAG
from qpi_driver.tuners.base.grouping import (
    by_output,
    couplings_of,
    endpoints_of,
    groups_of,
    is_too_close,
    outputs_of,
    readout_misfit,
    split_to_fit,
)
from tests.test_calibration_dag import StubRoutine
from tests.utils.chips import chain, heavy_hex, lattice, qubits_of

#: Wide enough not to be the thing under test where the topology is.
UNBOUNDED = 999


def _groups(edges, targets, spacing, **kwargs):
    return groups_of(
        targets,
        adjacency=couplings_of(edges),
        spacing=spacing,
        max_group=kwargs.pop("max_group", UNBOUNDED),
        **kwargs,
    )


class TestWhatATargetOccupies:
    def test_a_qubit_is_its_own_endpoint(self):
        assert endpoints_of("q3") == ("q3",)

    def test_an_edge_is_both_its_qubits(self):
        assert endpoints_of("q0_q1") == ("q0", "q1")

    def test_a_name_that_is_not_a_pair_stays_whole(self):
        """Otherwise a stray underscore would silently become a two-qubit target."""
        assert endpoints_of("q0_") == ("q0_",)
        assert endpoints_of("q0_q1_q2") == ("q0_q1_q2",)


class TestTheCouplingGraph:
    def test_an_edge_couples_both_ways(self):
        assert couplings_of(["q0_q1"]) == {"q0": {"q1"}, "q1": {"q0"}}

    def test_a_chain_couples_only_its_neighbours(self):
        adjacency = couplings_of(chain(5))
        assert adjacency["q0"] == {"q1"}
        assert adjacency["q2"] == {"q1", "q3"}

    def test_a_name_that_is_not_an_edge_couples_nothing(self):
        assert couplings_of(["q0"]) == {}


class TestHowFarApartIsFarEnough:
    """`spacing` is the minimum distance, so conflict is distance < spacing."""

    def setup_method(self):
        self.chain = couplings_of(chain(5))

    def test_a_spacing_of_one_constrains_nothing(self):
        assert not is_too_close("q0", "q1", self.chain, 1)

    def test_the_default_spacing_excludes_adjacent_qubits(self):
        assert is_too_close("q0", "q1", self.chain, DEFAULT_QUBIT_SPACING)
        assert not is_too_close("q0", "q2", self.chain, DEFAULT_QUBIT_SPACING)

    def test_a_spacing_of_three_wants_two_qubits_between(self):
        assert is_too_close("q0", "q2", self.chain, 3)
        assert not is_too_close("q0", "q3", self.chain, 3)

    def test_couplers_sharing_a_qubit_are_never_far_enough(self):
        assert is_too_close("q0_q1", "q1_q2", self.chain, DEFAULT_EDGE_SPACING)

    def test_disjoint_couplers_clear_the_default_edge_spacing(self):
        """Adjacent but disjoint: `edge_spacing` 1 asks only that they share no qubit."""
        assert not is_too_close("q0_q1", "q2_q3", self.chain, DEFAULT_EDGE_SPACING)

    def test_a_higher_edge_spacing_puts_a_qubit_between_two_couplers(self):
        assert is_too_close("q0_q1", "q2_q3", self.chain, 2)
        assert not is_too_close("q0_q1", "q3_q4", self.chain, 2)

    def test_a_spacing_of_zero_is_no_question_at_all(self):
        assert not is_too_close("q0", "q0", self.chain, 0)

    def test_an_unknown_qubit_conflicts_with_nothing_but_itself(self):
        assert not is_too_close("q9", "q0", self.chain, 4)


class TestTheGroupsATopologyAllows:
    """The measured figures behind RFC 0009 §5.5.

    Each topology is asserted at two sizes, which is what pins the claim that the
    group count follows the connectivity graph and not the qubit count.
    """

    @pytest.mark.parametrize("qubits", [5, 10])
    def test_a_chain_needs_two_groups_at_the_default_spacing(self, qubits):
        edges = chain(qubits)
        groups = _groups(edges, qubits_of(edges), DEFAULT_QUBIT_SPACING)
        assert len(groups) == 2

    @pytest.mark.parametrize("side", [3, 5])
    def test_a_lattice_needs_two_groups_at_the_default_spacing(self, side):
        """Two whatever the size, because a lattice is bipartite."""
        edges = lattice(side)
        groups = _groups(edges, qubits_of(edges), DEFAULT_QUBIT_SPACING)
        assert len(groups) == 2

    def test_heavy_hex_needs_two_groups_at_the_default_spacing(self):
        edges = heavy_hex()
        assert len(_groups(edges, qubits_of(edges), DEFAULT_QUBIT_SPACING)) == 2

    def test_a_chain_needs_three_groups_with_two_qubits_between(self):
        edges = chain(10)
        assert len(_groups(edges, qubits_of(edges), 3)) == 3

    def test_greedy_exceeds_the_optimum_on_a_lattice_at_spacing_three(self):
        """Measured, not derived. The five-colour Lee tiling is the *optimum* for an
        infinite lattice; greedy colouring reaches six or seven, which costs runtime
        and never correctness. The default spacing is where greedy is exact."""
        edges = lattice(5)
        assert len(_groups(edges, qubits_of(edges), 3)) == 7

    @pytest.mark.parametrize(
        "edges,expected", [(chain(10), 2), (lattice(5), 4), (heavy_hex(), 3)]
    )
    def test_couplers_group_into_one_class_per_degree(self, edges, expected):
        """Vizing's bound, reached: a chain is degree 2, a lattice 4, heavy-hex 3."""
        assert len(_groups(edges, edges, DEFAULT_EDGE_SPACING)) == expected

    def test_every_target_lands_in_exactly_one_group(self):
        edges = lattice(4)
        targets = qubits_of(edges)
        groups = _groups(edges, targets, DEFAULT_QUBIT_SPACING)

        placed = [target for group in groups for target in group]
        assert sorted(placed) == sorted(targets)
        assert len(placed) == len(set(placed))

    def test_no_group_holds_two_targets_that_are_too_close(self):
        edges = lattice(4)
        adjacency = couplings_of(edges)
        groups = _groups(edges, qubits_of(edges), DEFAULT_QUBIT_SPACING)

        for group in groups:
            for i, first in enumerate(group):
                for second in group[i + 1 :]:
                    assert not is_too_close(
                        first, second, adjacency, DEFAULT_QUBIT_SPACING
                    )

    def test_the_same_config_always_produces_the_same_groups(self):
        edges = lattice(4)
        targets = qubits_of(edges)
        once = _groups(edges, targets, DEFAULT_QUBIT_SPACING)
        assert _groups(edges, targets, DEFAULT_QUBIT_SPACING) == once

    def test_an_excluded_pair_is_never_grouped(self):
        edges = chain(5)
        groups = _groups(
            edges, qubits_of(edges), DEFAULT_QUBIT_SPACING, exclude=[["q0", "q2"]]
        )

        assert not any({"q0", "q2"} <= set(group) for group in groups)

    def test_a_coupler_is_excluded_through_either_of_its_qubits(self):
        edges = chain(5)
        groups = _groups(edges, edges, DEFAULT_EDGE_SPACING, exclude=[["q0", "q3"]])

        assert not any({"q0_q1", "q2_q3"} <= set(group) for group in groups)

    def test_an_exclusion_that_is_not_a_pair_is_ignored(self):
        edges = chain(5)
        groups = _groups(
            edges, qubits_of(edges), DEFAULT_QUBIT_SPACING, exclude=[["q0"]]
        )
        assert len(groups) == 2

    def test_max_group_caps_a_class(self):
        edges = chain(10)
        groups = _groups(edges, qubits_of(edges), DEFAULT_QUBIT_SPACING, max_group=2)

        assert all(len(group) <= 2 for group in groups)
        assert sum(len(group) for group in groups) == 10


class TestWhatOneOutputCanPlayAtOnce:
    """§5.4 — three ceilings, all arithmetic on the configs."""

    def test_readouts_inside_the_band_and_the_scale_fit(self):
        assert readout_misfit([6.4e9, 6.8e9], [0.03, 0.04], band_hz=500e6) is None

    def test_a_lone_readout_is_never_out_of_band(self):
        assert readout_misfit([6.4e9], [0.03], band_hz=1.0) is None

    def test_clocks_further_apart_than_the_band_do_not_fit(self):
        reason = readout_misfit([6.0e9, 7.5e9], [0.03, 0.03], band_hz=500e6)

        assert reason is not None
        # The figure and the ceiling, so an operator can act on it.
        assert "750.0 MHz from the band centre" in reason
        assert "500 MHz" in reason

    def test_amplitudes_that_would_clip_do_not_fit(self):
        reason = readout_misfit([6.4e9] * 3, [0.4, 0.4, 0.4], band_hz=500e6)

        assert reason is not None and "1.200 of full scale" in reason

    def test_more_clocks_than_the_module_has_sequencers_do_not_fit(self):
        reason = readout_misfit([6.4e9] * 7, [0.01] * 7, band_hz=500e6, sequencers=6)

        assert reason is not None and "the module has 6" in reason

    def test_a_group_that_fits_is_left_alone(self):
        assert split_to_fit(["q0", "q1"], lambda _: None) == [["q0", "q1"]]

    def test_a_group_that_does_not_fit_is_bisected_until_it_does(self):
        def misfit(group):
            return "too many" if len(group) > 2 else None

        assert split_to_fit(["q0", "q1", "q2", "q3"], misfit) == [
            ["q0", "q1"],
            ["q2", "q3"],
        ]

    def test_a_single_target_that_cannot_fit_runs_alone_and_says_so(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert split_to_fit(["q0"], lambda _: "nothing fits") == [["q0"]]

        assert "q0 alone does not fit" in caplog.text


class TestReadingTheWiring:
    def _device(self, pairs, as_graph=False):
        graph = SimpleNamespace(edges=pairs) if as_graph else pairs
        return SimpleNamespace(
            hardware_config=lambda: SimpleNamespace(
                connectivity=SimpleNamespace(graph=graph)
            )
        )

    def test_it_maps_a_port_to_the_output_it_hangs_off(self):
        wiring = outputs_of(self._device([["clusterA.module20.out0", "q0:res"]]))

        assert wiring == {"q0:res": "clusterA.module20.out0"}

    def test_either_end_may_be_the_port(self):
        """A graph object may present an edge in either direction."""
        wiring = outputs_of(self._device([["q0:res", "clusterA.module20.out0"]]))

        assert wiring == {"q0:res": "clusterA.module20.out0"}

    def test_a_graph_object_is_read_through_its_edges(self):
        wiring = outputs_of(
            self._device([("clusterA.module20.out0", "q0:res")], as_graph=True)
        )

        assert wiring == {"q0:res": "clusterA.module20.out0"}

    def test_unreadable_wiring_imposes_no_constraint(self):
        assert outputs_of(SimpleNamespace()) == {}

    def test_targets_are_grouped_by_the_output_they_share(self):
        wiring = {"q0:res": "out0", "q1:res": "out0", "q2:res": "out1"}

        assert by_output(["q0", "q1", "q2"], wiring, "res") == {
            "out0": ["q0", "q1"],
            "out1": ["q2"],
        }

    def test_a_target_the_wiring_does_not_name_stays_with_the_rest(self):
        """An unreadable or partial wiring must not quietly drop the checks."""
        assert by_output(["q0", "q1"], {}, "res") == {"res": ["q0", "q1"]}


class TestTheParallelConfig:
    def test_it_is_off_unless_the_file_says_otherwise(self):
        assert not CalibrationConfig.from_dict({}).parallel.enabled

    def test_it_reads_the_spacings_and_the_ceiling(self):
        config = ParallelConfig.from_dict(
            {"enabled": True, "qubit_spacing": 3, "edge_spacing": 2, "max_group": 4}
        )

        assert config.enabled
        assert config.spacing_for("qubits") == 3
        assert config.spacing_for("edges") == 2
        assert config.max_group == 4

    def test_the_defaults_are_the_conservative_ones(self):
        config = ParallelConfig.from_dict({"enabled": True})

        assert config.spacing_for("qubits") == DEFAULT_QUBIT_SPACING
        assert config.spacing_for("edges") == DEFAULT_EDGE_SPACING
        assert config.max_group == DEFAULT_MAX_GROUP

    @pytest.mark.parametrize(
        "data,message",
        [
            ({"qubit_spacing": "wide"}, "must be a whole number"),
            ({"max_group": 0}, "must be at least 1"),
            ({"exclude": [["q0", "q1", "q2"]]}, "takes pairs"),
            ({"groups": {"couplers": []}}, "knows 'qubits' and 'edges'"),
            ({"groups": []}, "must be a mapping"),
            ({"exclude": {"q0": "q1"}}, "must be a list of target pairs"),
        ],
    )
    def test_a_setting_it_cannot_use_is_a_startup_error(self, data, message):
        with pytest.raises(ConfigError, match=message):
            ParallelConfig.from_dict(data)

    def test_a_parallel_block_that_is_not_a_mapping_is_a_startup_error(self):
        with pytest.raises(ConfigError, match="'parallel' must be a mapping"):
            CalibrationConfig.from_dict({"parallel": ["enabled"]})


class TestTheGroupsAWalkPublishes:
    """`CalibrationDAG.groups_for`, and the `groups` key it puts on the plan."""

    def _dag(self, **parallel):
        config = CalibrationConfig(
            target_qubits=["q0", "q1", "q2", "q3", "q4"],
            target_edges=chain(5),
            parallel=ParallelConfig(**parallel),
        )
        return CalibrationDAG([StubRoutine("a")], config), config

    def test_a_walk_that_did_not_ask_runs_one_target_at_a_time(self):
        """The default has to leave every existing chip calibrating as it did."""
        dag, config = self._dag()

        assert dag.groups_for("a", config) == [["q0"], ["q1"], ["q2"], ["q3"], ["q4"]]

    def test_enabling_it_groups_by_the_coupling_graph(self):
        dag, config = self._dag(enabled=True)

        assert dag.groups_for("a", config) == [["q0", "q2", "q4"], ["q1", "q3"]]

    def test_explicit_groups_skip_the_colouring(self):
        dag, config = self._dag(enabled=True, groups={"qubits": [["q0", "q1"], ["q2"]]})

        groups = dag.groups_for("a", config)

        assert groups[:2] == [["q0", "q1"], ["q2"]]

    def test_a_target_the_explicit_groups_forgot_is_still_calibrated(self):
        dag, config = self._dag(enabled=True, groups={"qubits": [["q0", "q2"]]})

        assert dag.groups_for("a", config) == [["q0", "q2"], ["q1"], ["q3"], ["q4"]]

    def test_explicit_groups_cannot_add_a_target_the_run_excludes(self):
        """A config naming a qubit this run does not walk would otherwise put it back."""
        dag, config = self._dag(enabled=True, groups={"qubits": [["q0", "q9"]]})

        placed = [target for group in dag.groups_for("a", config) for target in group]
        assert "q9" not in placed

    def test_the_plan_carries_the_groups_for_the_drawing(self):
        dag, config = self._dag(enabled=True)

        node = dag.plan(["a"], config)["nodes"][0]

        assert node["groups"] == [["q0", "q2", "q4"], ["q1", "q3"]]

    def test_a_single_target_needs_no_colouring(self):
        config = CalibrationConfig(
            target_qubits=["q0"], parallel=ParallelConfig(enabled=True)
        )
        dag = CalibrationDAG([StubRoutine("a")], config)

        assert dag.groups_for("a", config) == [["q0"]]

    def test_an_unknown_routine_has_no_groups(self):
        dag, config = self._dag(enabled=True)

        assert dag.groups_for("nope", config) == []


class TestAnUnreadableCouplingGraph:
    """No adjacency must not read as nothing being adjacent (RFC 0009 §5.2)."""

    def _groups(self, targets, edges):
        config = CalibrationConfig(
            target_qubits=list(targets),
            target_edges=list(edges),
            parallel=ParallelConfig(enabled=True),
        )
        dag = CalibrationDAG([StubRoutine("a")], config)
        return dag.groups_for("a", config)

    def test_targets_run_one_at_a_time_when_nothing_says_what_couples(self):
        """Otherwise every qubit sits at infinite distance and lands in one group — the
        most aggressive setting there is, arrived at by accident."""
        assert self._groups(["q0", "q1", "q2"], []) == [["q0"], ["q1"], ["q2"]]

    def test_one_target_needs_no_coupling_graph(self):
        assert self._groups(["q0"], []) == [["q0"]]

    def test_a_configured_edge_is_enough_to_group_from(self):
        groups = self._groups(["q0", "q1", "q2"], ["q0_q1", "q1_q2"])

        assert sorted(sorted(g) for g in groups) == [["q0", "q2"], ["q1"]]
