"""Cheiron — a ClinicalTrials.gov query-to-visualization agent.

The package is layered so that the deterministic core can be tested without a network or
an API key. Reading order, which is also the request's path through the system:

    schemas/   the field registry, the Plan, and the request/response envelope
    llm/       the four model touchpoints: router, planner, judge, chart selector
    ctgov/     compile a Plan to registry queries, fetch, and flatten the records
    agg/       fold records into buckets — where every charted value is born
    viz/       chart legality, the response envelope, and citation verification
    pipeline   the stages in sequence
    api/       the HTTP surface and the demo frontend

The invariant the layering exists to protect: the models see aggregate facts about the
data and never a trial record, and no model-visible number ever reaches the output.
"""
