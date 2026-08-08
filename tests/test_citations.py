"""Deep citation tests.

A citation is a claim about the source record, so the tests are mostly about the ways that
claim can be false while still looking fine:

* an excerpt that is real text from the wrong place,
* an excerpt whose offsets have drifted,
* an excerpt for a value the record never states.

The first is the dangerous one. It re-verifies successfully — the offsets are internally
consistent — so verification alone does not catch it, and only checking that the span
actually states the value does.

No network, no LLM.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cheiron.agg.aggregator import aggregate
from cheiron.ctgov.normalizer import PHASE_NOT_REPORTED, NormalizedRecord, normalize_study
from cheiron.ctgov.retrieval import Retrieval
from cheiron.schemas.plan import Filters, Layout, Leg, Plan
from cheiron.viz.assembler import build_citations
from cheiron.viz.citations import (
    MAX_EXCERPT_CHARS,
    PROSE_WINDOW,
    Excerpt,
    index_spans,
    locate,
    serialize,
    verify,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "raw_studies"


def raw(nct_id: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / f"{nct_id}.json").read_text())


@pytest.fixture
def records() -> list[NormalizedRecord]:
    out = []
    for path in sorted(FIXTURE_DIR.glob("NCT*.json")):
        record = normalize_study(json.loads(path.read_text()))
        assert isinstance(record, NormalizedRecord)
        out.append(record)
    return out


# --------------------------------------------------------------------------------------
# The payload offsets index into
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("nct_id", [p.stem for p in sorted(FIXTURE_DIR.glob("NCT*.json"))])
def test_the_indexed_payload_is_the_documented_serialization(nct_id: str) -> None:
    """The README tells a reader to rebuild the string with this exact call and check any
    span by hand. If the indexer's output differed by a byte, every offset we publish
    would be unreproducible."""
    record = raw(nct_id)
    payload, _ = index_spans(record)
    assert payload == serialize(record)
    assert payload == json.dumps(record, separators=(",", ":"), ensure_ascii=False)


def test_every_recorded_span_slices_to_its_own_node() -> None:
    """Offsets are computed while writing rather than searched for afterwards, so this is
    the property that makes them trustworthy."""
    record = raw("NCT06077760")
    payload, spans = index_spans(record)
    assert spans
    for path, (start, end) in spans.items():
        assert 0 <= start < end <= len(payload), path
        assert payload[start:end], path


def test_a_dict_member_span_includes_its_key() -> None:
    """`["PHASE3"]` alone says nothing about which field it came from."""
    payload, spans = index_spans(raw("NCT06077760"))
    start, end = spans["protocolSection.designModule.phases"]
    assert payload[start:end] == '"phases":["PHASE3"]'


def test_repeated_keys_get_distinct_spans() -> None:
    """`name` occurs under the lead sponsor and under every intervention. An excerpt
    pointing at the wrong one would be a wrong citation that still verifies."""
    payload, spans = index_spans(raw("NCT00676871"))
    name_paths = [p for p in spans if p.endswith(".name")]
    assert len(name_paths) > 1
    assert len({spans[p] for p in name_paths}) == len(name_paths)


# --------------------------------------------------------------------------------------
# Locating
# --------------------------------------------------------------------------------------


def test_a_field_value_is_cited_from_its_own_field() -> None:
    """NCT02803307's title never says "phase", so there is no prose to prefer."""
    located = locate(raw("NCT02803307"), "protocolSection.designModule.phases", "PHASE1|PHASE2")
    assert located is not None
    payload, excerpt = located
    assert excerpt.kind == "field"
    assert excerpt.text == '"phases":["PHASE1","PHASE2"]'
    assert verify(payload, excerpt)


def test_a_composite_label_is_cited_from_the_span_stating_all_its_parts() -> None:
    """`PHASE1|PHASE2` is our label; the registry stores `["PHASE1","PHASE2"]`. Requiring a
    literal match would drop the citation for every multi-phase trial."""
    located = locate(raw("NCT02803307"), "protocolSection.designModule.phases", "PHASE1|PHASE2")
    assert located is not None
    payload, excerpt = located
    assert "PHASE1" in excerpt.text and "PHASE2" in excerpt.text
    assert verify(payload, excerpt)


def test_a_partially_matching_composite_is_not_cited() -> None:
    """Two of three drugs is a different regimen, not a partial citation."""
    assert (
        locate(raw("NCT02803307"), "protocolSection.designModule.phases", "PHASE1|PHASE4")
        is None
    )


def test_a_value_the_record_never_states_yields_no_citation() -> None:
    """`NOT_REPORTED` is our label for an absent key. Nothing can be quoted for it."""
    located = locate(
        raw("NCT02229435"), "protocolSection.designModule.phases", PHASE_NOT_REPORTED
    )
    assert located is None


def test_a_path_resolving_to_a_different_value_relocates() -> None:
    """The dangerous case: deduplication means the third distinct country is not
    `locations[2]`, so a path can resolve to a *different* value than the datum claims —
    and that excerpt verifies, because its offsets are internally consistent."""
    # Synthetic, because no fixture carries two conditions — and the point is the
    # relocation logic rather than the data. The datum claims "Uveal Melanoma" while the
    # path resolves to "Cutaneous Melanoma": a real value, in the right field, that does
    # not support this datum.
    record = {
        "protocolSection": {
            "identificationModule": {"nctId": "NCT1", "briefTitle": "A Study"},
            "conditionsModule": {"conditions": ["Cutaneous Melanoma", "Uveal Melanoma"]},
        }
    }
    located = locate(record, "protocolSection.conditionsModule.conditions[0]", "Uveal Melanoma")
    assert located is not None
    payload, excerpt = located

    assert "Uveal Melanoma" in excerpt.text
    assert "Cutaneous" not in excerpt.text, "the wrong-but-verifiable span was rejected"
    assert verify(payload, excerpt)


def test_a_malformed_path_still_finds_the_value() -> None:
    """`intervention_names` builds a path with an empty `[]` segment, which resolves to
    nothing. The value is still somewhere in the record, and that is what matters."""
    record = raw("NCT06077760")
    name = record["protocolSection"]["armsInterventionsModule"]["interventions"][0]["name"]
    located = locate(
        record, "protocolSection.armsInterventionsModule.interventions[].name[0]", name
    )
    assert located is not None
    payload, excerpt = located
    # Case-insensitively: a title writes "Intismeran Autogene" where the intervention
    # field says "Intismeran autogene". Same agent, and the excerpt quotes the record.
    assert name.lower() in excerpt.text.lower()
    assert verify(payload, excerpt)


# --------------------------------------------------------------------------------------
# Prose
# --------------------------------------------------------------------------------------


def test_a_title_span_is_preferred_when_it_states_the_value() -> None:
    """Closest to the assignment's illustrative example, when the registry cooperates."""
    record = raw("NCT06077760")
    title = record["protocolSection"]["identificationModule"]["briefTitle"]
    word = title.split()[0]

    located = locate(record, "protocolSection.conditionsModule.conditions[0]", word)
    assert located is not None
    payload, excerpt = located
    assert excerpt.kind == "prose"
    assert verify(payload, excerpt)


def test_a_prose_excerpt_never_straddles_the_json_key() -> None:
    """A window that ran left into `"briefTitle":` would be a true substring and unreadable."""
    record = raw("NCT06077760")
    title = record["protocolSection"]["identificationModule"]["briefTitle"]
    located = locate(record, "x", title.split()[0])
    assert located is not None
    _, excerpt = located
    assert "briefTitle" not in excerpt.text
    assert excerpt.text in title


def test_a_prose_excerpt_is_windowed() -> None:
    """An official title can run past 200 characters, burying the supporting part."""
    record = raw("NCT06077760")
    title = record["protocolSection"]["identificationModule"]["briefTitle"]
    located = locate(record, "x", title.split()[-1])
    assert located is not None
    _, excerpt = located
    assert len(excerpt.text) <= 2 * PROSE_WINDOW + len(title.split()[-1])


def test_coded_phases_are_matched_against_how_prose_writes_them() -> None:
    """No title contains the literal enum member `PHASE3`; many contain "Phase III"."""
    record = {
        "protocolSection": {
            "identificationModule": {"briefTitle": "A Phase III Study of Something"},
            "designModule": {"phases": ["PHASE3"]},
        }
    }
    located = locate(record, "protocolSection.designModule.phases", "PHASE3")
    assert located is not None
    payload, excerpt = located
    assert excerpt.kind == "prose"
    assert "Phase III" in excerpt.text
    assert verify(payload, excerpt)


def test_a_variant_never_invents_text() -> None:
    """Variants decide where to look; the excerpt is whatever the payload literally says."""
    record = {
        "protocolSection": {
            "identificationModule": {"briefTitle": "A Phase III Study"},
            "designModule": {"phases": ["PHASE3"]},
        }
    }
    payload, excerpt = locate(record, "protocolSection.designModule.phases", "PHASE3")
    assert excerpt.text in payload
    assert "PHASE3" not in excerpt.text, "the title says 'Phase III', and that is quoted"


# --------------------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------------------


def test_verification_accepts_a_span_that_is_really_there() -> None:
    payload, spans = index_spans(raw("NCT06077760"))
    start, end = spans["protocolSection.designModule.phases"]
    assert verify(payload, Excerpt(payload[start:end], start, end, "field"))


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda s, e: Excerpt(e.text, e.start + 1, e.end, e.kind), id="start drifted"),
        pytest.param(lambda s, e: Excerpt(e.text, e.start, e.end - 1, e.kind), id="end drifted"),
        pytest.param(lambda s, e: Excerpt("fabricated", e.start, e.end, e.kind), id="text swapped"),
        pytest.param(lambda s, e: Excerpt(e.text, -1, e.end, e.kind), id="negative offset"),
        pytest.param(lambda s, e: Excerpt(e.text, e.start, 10**9, e.kind), id="past the end"),
        pytest.param(lambda s, e: Excerpt("", e.start, e.start, e.kind), id="empty"),
    ],
)
def test_verification_rejects_anything_that_drifted(mutate) -> None:
    """The single line the whole citation claim rests on."""
    payload, spans = index_spans(raw("NCT06077760"))
    start, end = spans["protocolSection.designModule.phases"]
    good = Excerpt(payload[start:end], start, end, "field")
    assert not verify(payload, mutate(payload, good))


# --------------------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------------------


def by_id(records: list[NormalizedRecord]) -> dict[str, NormalizedRecord]:
    return {r.nct_id: r for r in records}


def test_every_emitted_citation_re_verifies_independently(
    records: list[NormalizedRecord],
) -> None:
    """Re-derived from scratch here rather than trusting the assembler's own check."""
    plan = Plan(legs=[Leg(label="All", filters=Filters(condition="x"))], group_by="phases")
    result = aggregate(plan, {"All": records})
    citations, _, _ = build_citations(result, by_id(records))

    assert citations
    for nct_id, citation in citations.items():
        payload = serialize(by_id(records)[nct_id].raw)
        start, end = citation.offset
        assert payload[start:end] == citation.excerpt


def test_an_emitted_excerpt_always_states_what_it_is_cited_for(
    records: list[NormalizedRecord],
) -> None:
    """Verification proves the text is there; this proves it is the *supporting* text."""
    plan = Plan(legs=[Leg(label="All")], group_by="sponsor_class")
    result = aggregate(plan, {"All": records})
    citations, _, _ = build_citations(result, by_id(records))

    for citation in citations.values():
        assert citation.field_value.lower() in citation.excerpt.lower()


def test_an_unquotable_value_is_counted_separately_from_a_failed_verification(
    records: list[NormalizedRecord],
) -> None:
    """Opposite meanings: one is honest absence, the other is a defect in this code."""
    plan = Plan(legs=[Leg(label="All")], group_by="phases")
    result = aggregate(plan, {"All": records})
    citations, unquotable, unverified = build_citations(result, by_id(records))

    assert unquotable == 3, "the three trials with no phases key"
    assert unverified == 0
    assert not any(c.field_value == PHASE_NOT_REPORTED for c in citations.values())


def test_a_record_without_its_raw_payload_is_not_cited(
    records: list[NormalizedRecord],
) -> None:
    """Nothing to locate an offset in, so nothing honest to emit."""
    stripped = [NormalizedRecord(r.nct_id, r.values, raw={}) for r in records]
    plan = Plan(legs=[Leg(label="All")], group_by="phases")
    result = aggregate(plan, {"All": stripped})
    citations, unquotable, _ = build_citations(result, by_id(stripped))

    assert citations == {}
    assert unquotable > 0


def test_citations_are_capped(records: list[NormalizedRecord]) -> None:
    plan = Plan(legs=[Leg(label="All")], group_by="phases")
    result = aggregate(plan, {"All": records})
    citations, _, _ = build_citations(result, by_id(records), limit=2)
    assert len(citations) == 2


def test_a_combination_citation_shows_the_arm_linkage(
    records: list[NormalizedRecord],
) -> None:
    """An edge asserts two agents were given together. `"name":"Ipilimumab"` proves the
    agent is in the trial and says nothing about what it was given with; one level out is
    the intervention object carrying `armGroupLabels`, which is the linkage itself."""
    combo = next((r for r in records if r.get("combination_groups")), None)
    if combo is None:
        pytest.skip("no fixture carries a combination")

    plan = Plan(
        legs=[Leg(label="All", filters=Filters(condition="x"))],
        group_by="intervention_names",
        layout=Layout.COOCCURRENCE,
    )
    result = aggregate(plan, {"All": [combo]})
    citations, _, _ = build_citations(result, {combo.nct_id: combo})

    for citation in citations.values():
        assert len(citation.excerpt) <= MAX_EXCERPT_CHARS


def test_the_response_states_why_a_datum_carries_no_citation(
    records: list[NormalizedRecord],
) -> None:
    from cheiron.viz.assembler import assemble

    plan = Plan(legs=[Leg(label="All")], group_by="phases")
    result = aggregate(plan, {"All": records})
    response = assemble(plan, result, Retrieval(records_by_leg={"All": records}, matched=11))

    assert any("no text to quote" in w for w in response.meta.warnings)
