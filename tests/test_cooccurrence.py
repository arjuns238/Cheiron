"""Co-occurrence network tests.

The thing under test is not "does a graph come out" — it is whether an edge means what it
claims. A drug↔drug edge asserts that two agents were given together, and the registry does
not record that directly: it records interventions, arm groups, and a membership between
them. Getting the edge rule wrong produces a graph that looks authoritative and is wrong
about clinical fact.

Measured on 500 melanoma trials: 217 co-list two or more agents, but only 157 have two or
more sharing an arm. Trial-level pairing would therefore assert combinations for ~28% more
trials than actually have one, including drug↔placebo edges from double-dummy designs.

No network, no LLM.
"""

from __future__ import annotations

import pytest

from cheiron.agg.aggregator import (
    NO_COOCCURRING_VALUES,
    _cooccurrence_pairs,
    aggregate,
    check_invariants,
)
from cheiron.ctgov.compiler import ARM_SCOPED_SOURCES, projection
from cheiron.ctgov.normalizer import (
    NormalizedRecord,
    combination_groups,
    is_placebo,
    normalize_study,
)
from cheiron.ctgov.retrieval import Retrieval
from cheiron.schemas.plan import Filters, Granularity, Layout, Leg, Metric, Plan, validate_plan
from cheiron.schemas.response import NetworkData, VizType
from cheiron.viz.assembler import assemble
from cheiron.viz.rules import describe_shape, legal_charts


def study(nct_id: str, interventions: list[dict], **modules) -> dict:
    return {
        "protocolSection": {
            "identificationModule": {"nctId": nct_id, "briefTitle": f"Study {nct_id}"},
            "armsInterventionsModule": {"interventions": interventions},
            **modules,
        }
    }


def agent(name: str, arms: list[str], kind: str = "DRUG") -> dict:
    return {"name": name, "type": kind, "armGroupLabels": arms}


def record(nct_id: str, interventions: list[dict]) -> NormalizedRecord:
    normalized = normalize_study(study(nct_id, interventions))
    assert isinstance(normalized, NormalizedRecord)
    return normalized


def plan_for(field: str = "intervention_names", **kwargs) -> Plan:
    return Plan(
        legs=[Leg(label="Slice", filters=Filters(condition="melanoma"))],
        group_by=field,
        layout=Layout.COOCCURRENCE,
        **kwargs,
    )


# --------------------------------------------------------------------------------------
# combination_groups: what the registry actually says was given together
# --------------------------------------------------------------------------------------


def test_agents_sharing_an_arm_form_a_combination() -> None:
    """NCT00002882's shape: three drugs, one arm, one real regimen."""
    groups = combination_groups(
        [
            agent("Cisplatin", ["Adjuvant Biochemotherapy"]),
            agent("Dacarbazine", ["Adjuvant Biochemotherapy"]),
            agent("Vinblastine", ["Adjuvant Biochemotherapy"]),
        ]
    )
    assert groups == ["Cisplatin || Dacarbazine || Vinblastine"]


def test_agents_in_different_arms_are_not_a_combination() -> None:
    """NCT01748448's shape: Vitamin D versus Placebo. A naive pairing draws an edge
    between a drug and its own control, which is the failure this rule prevents."""
    assert combination_groups(
        [agent("Vitamin D", ["Vitamin D"]), agent("Placebo: Oil", ["Placebo: Oil"])]
    ) == []


def test_a_lone_agent_in_an_arm_produces_no_group() -> None:
    assert combination_groups([agent("Pembrolizumab", ["Treatment"])]) == []


def test_a_trial_can_contribute_several_regimens() -> None:
    """NCT02224781's shape: two experimental arms, two distinct combinations."""
    groups = combination_groups(
        [
            agent("Dabrafenib", ["Arm A"]),
            agent("Trametinib", ["Arm A"]),
            agent("Ipilimumab", ["Arm B"]),
            agent("Nivolumab", ["Arm B"]),
        ]
    )
    assert groups == ["Dabrafenib || Trametinib", "Ipilimumab || Nivolumab"]


def test_biologicals_count_as_agents() -> None:
    """Sponsors filed pembrolizumab as DRUG 405 times and BIOLOGICAL 94 times — the same
    molecule. A boundary that splits one molecule 80/20 is recording paperwork, not
    pharmacology, so both types are agents."""
    groups = combination_groups(
        [agent("Pembrolizumab", ["A"], "BIOLOGICAL"), agent("Lenvatinib", ["A"], "DRUG")]
    )
    assert groups == ["Lenvatinib || Pembrolizumab"]


@pytest.mark.parametrize("kind", ["PROCEDURE", "DEVICE", "RADIATION", "BEHAVIORAL", "OTHER"])
def test_non_agents_do_not_form_drug_combinations(kind: str) -> None:
    assert combination_groups([agent("Pembrolizumab", ["A"]), agent("Surgery", ["A"], kind)]) == []


def test_placebos_in_an_active_arm_are_excluded() -> None:
    """Double-dummy blinding puts a placebo in the *active* arm, typed DRUG.

    NCT01721772 yields "BMS-936558 (Nivolumab) || Placebo matching Dacarbazine". Arm
    membership alone is not enough; an edge there asserts a drug is combined with a sham.
    """
    groups = combination_groups(
        [
            agent("BMS-936558 (Nivolumab)", ["Arm A"]),
            agent("Placebo matching Dacarbazine", ["Arm A"]),
        ]
    )
    assert groups == []


@pytest.mark.parametrize(
    "name", ["Placebo", "placebo oral tablet", "Sham injection", "Vehicle Control"]
)
def test_placebo_names_are_recognized(name: str) -> None:
    assert is_placebo(name)


@pytest.mark.parametrize("name", ["Pembrolizumab", "Cisplatin", "Dexamethasone"])
def test_real_agents_are_not_mistaken_for_placebos(name: str) -> None:
    assert not is_placebo(name)


def test_an_agent_appearing_twice_in_one_arm_is_counted_once() -> None:
    groups = combination_groups(
        [agent("Nivolumab", ["A"]), agent("Nivolumab", ["A"]), agent("Ipilimumab", ["A"])]
    )
    assert groups == ["Ipilimumab || Nivolumab"]


# --------------------------------------------------------------------------------------
# Pairing
# --------------------------------------------------------------------------------------


def test_interventions_pair_only_within_an_arm() -> None:
    combo = record(
        "NCT1", [agent("A", ["Arm 1"]), agent("B", ["Arm 1"]), agent("C", ["Arm 2"])]
    )
    pairs, missing = _cooccurrence_pairs(combo, "intervention_names")

    assert missing is None
    assert [(a, b) for a, b, _ in pairs] == [("A", "B")]
    assert "C" not in {v for a, b, _ in pairs for v in (a, b)}


def test_other_fields_pair_the_whole_trial_list() -> None:
    """`conditions` and `intervention_mesh` have no arm structure, and co-occurrence
    within the trial is the relationship being asked about."""
    raw = study("NCT2", [])
    raw["protocolSection"]["conditionsModule"] = {"conditions": ["Melanoma", "Skin Cancer"]}
    normalized = normalize_study(raw)
    assert isinstance(normalized, NormalizedRecord)

    pairs, missing = _cooccurrence_pairs(normalized, "conditions")
    assert missing is None
    assert [(a, b) for a, b, _ in pairs] == [("Melanoma", "Skin Cancer")]


def test_pairs_are_ordered_so_a_b_and_b_a_are_one_edge() -> None:
    forward = record("NCT3", [agent("Zeta", ["A"]), agent("Alpha", ["A"])])
    pairs, _ = _cooccurrence_pairs(forward, "intervention_names")
    assert [(a, b) for a, b, _ in pairs] == [("Alpha", "Zeta")]


def test_a_regimen_repeated_across_arms_contributes_one_edge() -> None:
    twice = record(
        "NCT4",
        [
            agent("A", ["Arm 1", "Arm 2"]),
            agent("B", ["Arm 1", "Arm 2"]),
        ],
    )
    pairs, _ = _cooccurrence_pairs(twice, "intervention_names")
    assert len(pairs) == 1


def test_three_agents_in_one_arm_produce_three_edges() -> None:
    triple = record(
        "NCT5", [agent("A", ["X"]), agent("B", ["X"]), agent("C", ["X"])]
    )
    pairs, _ = _cooccurrence_pairs(triple, "intervention_names")
    assert {(a, b) for a, b, _ in pairs} == {("A", "B"), ("A", "C"), ("B", "C")}


def test_a_trial_with_nothing_to_pair_is_counted_not_dropped() -> None:
    """A sparse network raises "why so few trials?"; the exclusion count answers it."""
    lonely = record("NCT6", [agent("Solo", ["Only arm"])])
    pairs, missing = _cooccurrence_pairs(lonely, "intervention_names")
    assert pairs == []
    assert missing == NO_COOCCURRING_VALUES


# --------------------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------------------


def combo_records() -> list[NormalizedRecord]:
    return [
        record("NCT1", [agent("Ipilimumab", ["A"]), agent("Nivolumab", ["A"])]),
        record("NCT2", [agent("Ipilimumab", ["A"]), agent("Nivolumab", ["A"])]),
        record("NCT3", [agent("Dabrafenib", ["A"]), agent("Trametinib", ["A"])]),
        record("NCT4", [agent("Solo", ["A"])]),
    ]


def test_edge_weight_is_the_number_of_trials_containing_both() -> None:
    result = aggregate(plan_for(), {"Slice": combo_records()})

    weights = {(b.dimension, b.series): b.value for b in result.buckets}
    assert weights[("Ipilimumab", "Nivolumab")] == 2.0
    assert weights[("Dabrafenib", "Trametinib")] == 1.0
    assert result.used == 3
    assert result.excluded_by_reason == {NO_COOCCURRING_VALUES: 1}


def test_an_edge_carries_the_trials_that_justify_it() -> None:
    """The whole point of the bucket structure: weight and citations are one object."""
    result = aggregate(plan_for(), {"Slice": combo_records()})
    edge = next(b for b in result.buckets if b.dimension == "Ipilimumab")

    assert edge.nct_ids == ["NCT1", "NCT2"]
    assert edge.value == len(edge.nct_ids)
    assert all(c.field_path.endswith("armGroupLabels") for c in edge.contributions)


def test_the_accounting_reconciles_for_a_network() -> None:
    plan = plan_for()
    result = aggregate(plan, {"Slice": combo_records()})
    check_invariants(plan, result)
    assert result.used + sum(result.excluded_by_reason.values()) == result.retrieved


def test_top_n_keeps_the_busiest_nodes_and_their_edges() -> None:
    """A network cannot use an "Other" bucket: "Other" has nothing to co-occur with."""
    records = combo_records() + [
        record("NCT5", [agent("Rare1", ["A"]), agent("Rare2", ["A"])]),
    ]
    result = aggregate(plan_for(top_n=2), {"Slice": records})

    nodes = {b.dimension for b in result.buckets} | {b.series for b in result.buckets}
    assert nodes == {"Ipilimumab", "Nivolumab"}, "only the busiest pair survives"
    assert result.collapsed_dimensions == 4


def test_an_edge_is_dropped_when_either_endpoint_is_trimmed() -> None:
    """Keeping a half-edge would draw a link to a node that is not in the graph."""
    result = aggregate(plan_for(top_n=3), {"Slice": combo_records()})
    nodes = {b.dimension for b in result.buckets} | {b.series for b in result.buckets}
    for bucket in result.buckets:
        assert bucket.dimension in nodes and bucket.series in nodes


# --------------------------------------------------------------------------------------
# Plan validation
# --------------------------------------------------------------------------------------


def test_cooccurrence_requires_a_multi_valued_entity_field() -> None:
    errors = validate_plan(plan_for("phases"))
    assert any("multi-valued entity field" in e for e in errors)


def test_cooccurrence_rejects_a_second_dimension() -> None:
    plan = Plan(
        legs=[Leg(label="S", filters=Filters(condition="melanoma"))],
        group_by="intervention_names",
        series_by="conditions",
        layout=Layout.COOCCURRENCE,
    )
    assert any("series_by does not apply" in e for e in validate_plan(plan))


def test_cooccurrence_rejects_metrics_other_than_count() -> None:
    plan = plan_for(metric=Metric.MEDIAN, metric_field="enrollment")
    assert any("must be 'count'" in e for e in validate_plan(plan))


def test_cooccurrence_rejects_axis_settings() -> None:
    plan = Plan(
        legs=[Leg(label="S", filters=Filters(condition="melanoma"))],
        group_by="intervention_names",
        layout=Layout.COOCCURRENCE,
        granularity=Granularity.YEAR,
    )
    assert any("granularity and bins do not apply" in e for e in validate_plan(plan))


def test_a_well_formed_cooccurrence_plan_passes() -> None:
    assert validate_plan(plan_for(top_n=15)) == []


# --------------------------------------------------------------------------------------
# Compilation
# --------------------------------------------------------------------------------------


def test_the_projection_fetches_what_arm_pairing_needs() -> None:
    """Projecting only InterventionName returns interventions with no type and no arm
    labels, so every trial yields no pairs and the graph comes back empty — silently,
    because an empty graph looks like a slice with no combinations in it."""
    fields = projection(plan_for())
    for piece in ("InterventionName", "InterventionType", "InterventionArmGroupLabel"):
        assert piece in fields


def test_a_non_arm_scoped_cooccurrence_does_not_over_project() -> None:
    fields = projection(plan_for("conditions"))
    assert "InterventionArmGroupLabel" not in fields


def test_the_compiler_and_aggregator_agree_on_which_fields_are_arm_scoped() -> None:
    """Two tables, one fact. If they drift the compiler under-projects and the graph
    empties out with no error anywhere."""
    from cheiron.agg.aggregator import _ARM_SCOPED

    assert ARM_SCOPED_SOURCES == _ARM_SCOPED


# --------------------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------------------


def test_a_cooccurrence_result_is_only_ever_a_network() -> None:
    plan = plan_for()
    shape = describe_shape(plan, aggregate(plan, {"Slice": combo_records()}))
    assert legal_charts(shape) == (VizType.NETWORK,)


def response_for(plan: Plan):
    result = aggregate(plan, {"Slice": combo_records()})
    retrieval = Retrieval(records_by_leg={"Slice": combo_records()}, matched=4)
    return assemble(plan, result, retrieval)


def test_both_endpoints_carry_the_same_field_kind() -> None:
    response = response_for(plan_for())
    data = response.visualization.data
    assert isinstance(data, NetworkData)
    assert all(n.kind == "intervention_names" for n in data.nodes)
    assert all(e.source.startswith("intervention_names:") for e in data.edges)


def test_the_title_says_co_administered_not_merely_co_occurring() -> None:
    """Calling an arm-scoped graph a co-occurrence graph would understate it; calling a
    co-listing graph a combination graph would overstate it."""
    assert "Co-administered" in response_for(plan_for()).visualization.title
    assert "Co-occurring" in response_for(plan_for("conditions")).visualization.title


def test_the_edge_rule_is_stated_in_the_warnings() -> None:
    arm_scoped = response_for(plan_for()).meta.warnings[0]
    assert "shared an arm group" in arm_scoped
    assert "two sides of a comparison" in arm_scoped

    co_listed = response_for(plan_for("conditions")).meta.warnings[0]
    assert "same trial" in co_listed
    assert "no arm structure" in co_listed


def test_the_answer_describes_links_and_names_the_strongest() -> None:
    answer = response_for(plan_for()).answer
    assert "entities" in answer and "links" in answer
    assert "Ipilimumab" in answer and "Nivolumab" in answer


def test_node_weight_counts_distinct_trials_not_summed_edges() -> None:
    """A node in three combinations across one trial has weight one, not three."""
    records = [record("NCT1", [agent("A", ["X"]), agent("B", ["X"]), agent("C", ["X"])])]
    plan = plan_for()
    result = aggregate(plan, {"Slice": records})
    retrieval = Retrieval(records_by_leg={"Slice": records}, matched=1)
    data = assemble(plan, result, retrieval).visualization.data

    assert isinstance(data, NetworkData)
    assert len(data.edges) == 3
    assert all(node.weight == 1 for node in data.nodes)


# --------------------------------------------------------------------------------------
# Bounding: advice, not policy
# --------------------------------------------------------------------------------------


def wide_records(n: int = 60) -> list[NormalizedRecord]:
    """A long tail of one-trial agents plus a few recurring ones.

    Roughly 80% of agents in the registry appear in exactly one trial, which is what makes
    an unfiltered graph mostly noise and what the suggested threshold is calibrated against.
    The recurring pair exists so a threshold has something to separate — without varied
    occurrence counts no threshold is meaningful, and none is offered.
    """
    tail = [
        record(f"NCT{i:04d}", [agent(f"Drug{i}A", ["X"]), agent(f"Drug{i}B", ["X"])])
        for i in range(n)
    ]
    recurring = [
        record(f"NCT8{i:03d}", [agent("Common1", ["X"]), agent("Common2", ["X"])])
        for i in range(5)
    ]
    return tail + recurring


def test_a_network_is_returned_complete_by_default() -> None:
    """Thresholding a graph is a presentation decision. A client holding the whole network
    can move the threshold interactively; one handed a trimmed graph cannot get the rest
    back without another request."""
    result = aggregate(plan_for(), {"Slice": wide_records()})

    nodes = {b.dimension for b in result.buckets} | {b.series for b in result.buckets}
    assert len(nodes) == 122, "every node survives"
    assert len(result.buckets) == 61
    assert result.omitted_trials == 0


def test_a_large_network_carries_a_suggested_threshold() -> None:
    result = aggregate(plan_for(), {"Slice": wide_records()})
    assert result.suggested_min_occurrences is not None
    assert result.suggested_min_occurrences >= 2


def test_a_small_network_needs_no_advice() -> None:
    """Nothing to suggest when the graph already renders whole."""
    assert aggregate(plan_for(), {"Slice": combo_records()}).suggested_min_occurrences is None


def test_no_advice_when_every_node_occurs_equally_often() -> None:
    """No threshold separates a uniform graph, and "appears in at least 1 trial" would be
    advice that filters nothing."""
    uniform = [
        record(f"NCT{i:04d}", [agent(f"D{i}A", ["X"]), agent(f"D{i}B", ["X"])])
        for i in range(60)
    ]
    assert aggregate(plan_for(), {"Slice": uniform}).suggested_min_occurrences is None


def test_the_suggestion_removes_nothing() -> None:
    """The whole point of calling it advice: the payload is unchanged by it."""
    result = aggregate(plan_for(), {"Slice": wide_records()})
    graphed = {c.nct_id for b in result.buckets for c in b.contributions}
    assert len(graphed) == 65, "every contributing trial is still represented"


def test_an_explicit_top_n_is_still_honoured() -> None:
    """A plan that asks for a cap is making a request, not accepting a default."""
    result = aggregate(plan_for(top_n=4), {"Slice": wide_records()})
    nodes = {b.dimension for b in result.buckets} | {b.series for b in result.buckets}
    assert len(nodes) <= 4
    assert result.omitted_trials > 0


def test_trimmed_trials_are_counted_so_the_gap_can_be_stated() -> None:
    """Those trials stay in record_counts and appear in no node or edge — visible in
    neither unless the number is reported."""
    plan = plan_for(top_n=4)
    result = aggregate(plan, {"Slice": wide_records()})
    retrieval = Retrieval(records_by_leg={"Slice": wide_records()}, matched=65)
    response = assemble(plan, result, retrieval)

    warning = next(w for w in response.meta.warnings if "drawn in no node or edge" in w)
    assert str(result.omitted_trials) in warning.replace(",", "")


# --------------------------------------------------------------------------------------
# Association strength
# --------------------------------------------------------------------------------------


def hub_records() -> list[NormalizedRecord]:
    """A ubiquitous agent plus one distinctive pairing — the myeloma dexamethasone shape."""
    records = [
        record(f"NCT{i:03d}", [agent("Hub", ["X"]), agent(f"Partner{i}", ["X"])])
        for i in range(6)
    ]
    records += [
        record(f"NCT9{i:02d}", [agent("Alpha", ["X"]), agent("Beta", ["X"])]) for i in range(3)
    ]
    return records


def test_raw_weight_ranks_by_ubiquity() -> None:
    """On myeloma the five heaviest edges all contain dexamethasone, because it is in
    nearly every regimen rather than because those pairings are distinctive."""
    plan = plan_for()
    result = aggregate(plan, {"Slice": hub_records()})
    retrieval = Retrieval(records_by_leg={"Slice": hub_records()}, matched=9)
    data = assemble(plan, result, retrieval).visualization.data

    assert isinstance(data, NetworkData)
    assert data.edges[0].weight == 3, "Alpha–Beta is the heaviest single edge"
    assert sum(1 for e in data.edges if "Hub" in e.source or "Hub" in e.target) == 6


def test_association_strength_corrects_for_degree() -> None:
    """The distinctive pairing outranks the hub's edges once degree is divided out."""
    plan = plan_for()
    result = aggregate(plan, {"Slice": hub_records()})
    retrieval = Retrieval(records_by_leg={"Slice": hub_records()}, matched=9)
    data = assemble(plan, result, retrieval).visualization.data

    assert isinstance(data, NetworkData)
    by_strength = sorted(data.edges, key=lambda e: -(e.strength or 0))
    assert "Alpha" in by_strength[0].source
    hub_edges = [e for e in data.edges if "Hub" in e.source or "Hub" in e.target]
    assert all(e.strength < by_strength[0].strength for e in hub_edges)


def test_strength_is_derived_and_weight_stays_countable() -> None:
    """Only `weight` has trials behind it, and it must remain the citable number."""
    plan = plan_for()
    result = aggregate(plan, {"Slice": hub_records()})
    retrieval = Retrieval(records_by_leg={"Slice": hub_records()}, matched=9)
    data = assemble(plan, result, retrieval).visualization.data

    assert isinstance(data, NetworkData)
    for edge in data.edges:
        assert edge.weight == edge.nct_id_total
        assert edge.nct_id_total >= len(edge.nct_ids)
        assert edge.strength is not None


def test_case_variants_of_one_agent_are_one_node() -> None:
    """`Dexamethasone` and `dexamethasone` are one drug, and were two nodes.

    Measured on 1,000 multiple-myeloma trials: 58 groups differing only in case, covering
    the six commonest agents in the slice. Two nodes for one drug split its weight and
    understate both — visible on the captured network example as duplicate labels.
    """
    # The commonest spelling wins the label, so two trials say "Dexamethasone" and one
    # shouts it.
    records = [
        record("NCT00000001", [agent("Dexamethasone", ["A"]), agent("Lenalidomide", ["A"])]),
        record("NCT00000002", [agent("Dexamethasone", ["A"]), agent("Lenalidomide", ["A"])]),
        record("NCT00000003", [agent("DEXAMETHASONE", ["A"]), agent("lenalidomide", ["A"])]),
    ]
    result = aggregate(plan_for(), {"Slice": records})

    assert len(result.buckets) == 1, "one edge, not three"
    bucket = result.buckets[0]
    assert (bucket.dimension, bucket.series) == ("Dexamethasone", "Lenalidomide")
    assert len(bucket.nct_ids) == 3, "every spelling lands on the same edge"

    # The contribution keeps the pairing under the canonical spelling; the excerpt is
    # located case-insensitively, so the shouting record is still citable.
    assert all("Dexamethasone" in c.field_value for c in bucket.contributions)


def test_route_and_salt_variants_are_deliberately_not_merged() -> None:
    """The line is drawn at case, and stops there on purpose.

    `dexamethasone (iv)` and `dexamethasone (oral)` are the same drug by different routes,
    and merging them is a clinical judgement this system has no basis for. The hazard is
    sharper than it looks: `melphalan hydrochloride` and `melphalan flufenamide` share a
    stem and are *different drugs*.
    """
    records = [
        record("NCT00000001", [
            agent("Dexamethasone (IV)", ["A"]),
            agent("dexamethasone (oral)", ["A"]),
            agent("Melphalan hydrochloride", ["A"]),
            agent("Melphalan flufenamide", ["A"]),
        ])
    ]
    result = aggregate(plan_for(), {"Slice": records})
    labels = {b.dimension for b in result.buckets} | {b.series for b in result.buckets}
    assert labels == {
        "Dexamethasone (IV)",
        "dexamethasone (oral)",
        "Melphalan hydrochloride",
        "Melphalan flufenamide",
    }
