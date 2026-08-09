"""The response envelope.

One shape for every response. `response_type` discriminates; a frontend written against
this schema never has to branch on anything else to know what it received.

The envelope is deliberately custom rather than Vega-Lite. Two reasons, both in the
README: network graphs do not fit Vega-Lite's grammar without an extension, and the
assignment asks for a documented schema of the candidate's own design that a frontend
engineer can implement against without guessing.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from cheiron.schemas.plan import Plan


class ResponseType(StrEnum):
    """What kind of answer this is.

    `CONVERSATIONAL` is the only type with a null `visualization`. `NO_RESULTS` and
    `UNSUPPORTED` still carry a full visualization block with empty data and the reason in
    `meta.warnings`, so the frontend renders one shape always and never special-cases an
    empty state into a different code path.
    """

    VISUALIZATION = "visualization"
    CONVERSATIONAL = "conversational"
    UNSUPPORTED = "unsupported"
    NO_RESULTS = "no_results"


class VizType(StrEnum):
    LINE = "line"
    BAR = "bar"
    GROUPED_BAR = "grouped_bar"
    STACKED_BAR = "stacked_bar"
    STACKED_AREA = "stacked_area"
    PIE = "pie"
    SCATTER = "scatter"
    HISTOGRAM = "histogram"
    NETWORK = "network"
    CHOROPLETH = "choropleth"
    KPI = "kpi"


class Channel(BaseModel):
    """One visual channel binding: which field, rendered how."""

    model_config = ConfigDict(extra="forbid")

    field: str = Field(description="Key present on every datum in `data`.")
    label: str = Field(description="Human-readable axis or legend title.")
    type: Literal["quantitative", "temporal", "nominal", "ordinal"] = Field(
        description="How the frontend should scale and format this channel."
    )
    unit: str | None = Field(
        None, description="e.g. 'trials', 'participants'. Null when dimensionless."
    )


class Encoding(BaseModel):
    """Field-to-channel mapping: which key on each datum drives which visual channel.

    Read `x.field` and `y.field` rather than assuming a key name. Nothing in `data` is
    positional and no key is guaranteed by chart type — a phase chart carries `phases`, a
    country chart `countries` — so a renderer that hardcodes either will break on the next
    question. This is the one object that makes the payload self-describing.

    **Networks reuse x/y rather than adding node/edge channels.** `x` binds the node
    identifier and `y` binds the weight carried by both `Node` and `Edge`; `data` is a
    `NetworkData` object, not a list, and that switch is what tells a renderer to draw a
    graph. The channels are described per field below so the convention can be read here
    rather than inferred.
    """

    model_config = ConfigDict(extra="forbid")

    x: Channel | None = Field(
        None,
        description="The dimension axis. Its `field` names the key holding each datum's "
        "bucket label — `phases`, `countries`, `start_date`. For `network` it binds "
        "`Node.id`; for `kpi` it is null.",
    )
    y: Channel | None = Field(
        None,
        description="The measure axis, whose `field` is `value` on every flat datum. For "
        "`network` it binds `weight` — trials per node, shared trials per edge.",
    )
    series: Channel | None = Field(
        None,
        description="The splitting channel, present only when a datum carries a second "
        "coordinate: the leg of a comparison, or a crossed second field. Null otherwise, "
        "including for `network`, where the second coordinate is the edge's other endpoint.",
    )


class Citation(BaseModel):
    """A deep citation.

    `excerpt` is a literal substring of the fetched API payload, taken at `offset`. The
    spec assembler re-asserts the substring match at those offsets before emitting, and
    drops the citation rather than emit an unverified one. Excerpts are never generated
    by an LLM.
    """

    model_config = ConfigDict(extra="forbid")

    nct_id: str
    url: str
    brief_title: str
    field_path: str = Field(description="Dotted path in the source record, e.g. "
                            "'protocolSection.designModule.phases[0]'.")
    field_value: str = Field(
        description="The value at that path that put this trial in the bucket."
    )
    excerpt: str = Field(description="Verbatim substring of the fetched payload.")
    offset: tuple[int, int] = Field(
        description="[start, end) offsets of `excerpt` within the payload."
    )
    supports: Literal["value", "series"] = Field(
        "value",
        description="Which half of the datum this excerpt evidences. A grouped datum has "
        "two coordinates — the bucket and the series — and one excerpt rarely states "
        "both, so they are cited separately. `series` citations are absent when the "
        "record never states the leg's term: a leg is a search expression, and the "
        "registry's search expands it in ways the record does not repeat.",
    )


class Datum(BaseModel):
    """One rendered mark: a bar, a point, a time bucket, or a KPI value.

    `value` is always produced by a deterministic fold over `nct_ids`' underlying records.
    It is never LLM-authored, and the invariant check enforces that.
    """

    model_config = ConfigDict(extra="allow")  # dimension keys are added dynamically

    value: float | int = Field(description="The computed measure for this bucket.")
    nct_ids: list[str] = Field(
        default_factory=list,
        description="Up to 5 contributing trial IDs, as a sample of `nct_id_total`.",
    )
    nct_id_total: int = Field(
        0, description="How many trials actually contributed, before the sample was taken."
    )
    citations: list[Citation] = Field(
        default_factory=list,
        description="Evidence for **this** datum, one entry per sampled trial. Held here "
        "rather than in a response-level map keyed by NCT ID: on a multi-valued dimension "
        "one trial belongs to several datums, so a per-trial map can only carry one of "
        "its excerpts and every other datum silently reads a citation stating a different "
        "datum's value.",
    )


class Node(BaseModel):
    """A network node."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="`kind:label`, so two entities sharing a label but drawn "
                                "from different fields stay distinct.")
    label: str = Field(description="Display name. Sponsor-authored names are folded to one "
                                   "spelling per entity — see `docs/readme-notes.md` §22.")
    kind: str = Field(description="The field key this node came from, e.g. 'sponsor_name'.")
    weight: int = Field(description="Number of distinct trials this node appears in.")


class Edge(BaseModel):
    """A network edge.

    `weight` is the number of trials in which both endpoints appear — computed as the
    length of the same trial list that produced the edge's citations, so the two cannot
    disagree.
    """

    model_config = ConfigDict(extra="forbid")

    source: str = Field(description="`Node.id` of one endpoint.")
    target: str = Field(description="`Node.id` of the other endpoint. Edges are unordered; "
                                    "(A,B) and (B,A) are one edge.")
    weight: int = Field(description="Trials in which both endpoints appear — the countable "
                                    "value, folded from the trial list that carries the "
                                    "citations, so the two cannot disagree.")
    strength: float | None = Field(
        None,
        description="Association strength: 2m·w / (k_source · k_target), where k is a "
        "node's total edge weight and m the graph's. **Derived arithmetic, not a fold over "
        "records** — unlike `weight` it has no citations behind it, and it ranks edges "
        "rather than replacing the countable value. It corrects for degree: by raw weight "
        "an agent present in most regimens dominates purely by ubiquity. "
        "**Do not sort by this alone.** A pair occurring only with each other scores "
        "maximally on a single trial, so the top of an unfiltered strength ranking is "
        "noise; apply `config.suggested_min_occurrences` to `Node.weight` first.",
    )
    nct_ids: list[str] = Field(
        default_factory=list, description="Up to 5 contributing trials, a sample of "
                                          "`nct_id_total`."
    )
    nct_id_total: int = Field(0, description="Trials behind this edge, before sampling.")
    citations: list[Citation] = Field(
        default_factory=list,
        description="Evidence that both endpoints share an arm group in these trials. An "
        "edge is the network's datum, so its citations belong to it — a trial commonly "
        "lies on several edges.",
    )


class NetworkData(BaseModel):
    """Payload for `type: "network"`, in place of a flat datum list."""

    model_config = ConfigDict(extra="forbid")

    nodes: list[Node]
    edges: list[Edge]


class VizConfig(BaseModel):
    """Rendering hints that are not field bindings."""

    model_config = ConfigDict(extra="forbid")

    sort: str | None = Field(
        None, description="How `data` is already ordered — 'value_desc', 'dimension_asc'. "
        "The order in the payload is the intended one; re-sorting is a choice, not a fix."
    )
    granularity: str | None = Field(
        None, description="Time bucket width for temporal dimensions: 'year' or 'quarter'."
    )
    top_n: int | None = Field(
        None, description="The cap the plan applied to distinct dimension values. Null "
        "means every value is present."
    )
    other_bucket: bool = Field(
        False, description="True when values beyond top_n were collapsed into 'Other'."
    )
    stacked: bool = Field(
        False, description="Whether series should be stacked rather than grouped."
    )
    y_starts_at_zero: bool = Field(
        True, description="False only where a zero baseline would compress the signal to "
        "nothing; truncating an axis otherwise misleads."
    )
    x_scale: Literal["linear", "log"] = Field(
        "linear",
        description="Scale for the x axis, derived from the bound field's distribution "
        "rather than chosen by a model. 'log' when the field is heavy-tailed enough that a "
        "linear axis collapses the data against one edge.",
    )
    y_scale: Literal["linear", "log"] = Field(
        "linear",
        description="Scale for the y axis, same rule as `x_scale`. Measured on the "
        "captured scatter: median enrolment 44 against a maximum of 2,953,748, with 99.6% "
        "of points below 1% of that maximum — linear is correct and unreadable.",
    )
    suggested_min_occurrences: int | None = Field(
        None,
        description="Networks only, and **advisory** — the graph is returned complete and "
        "nothing has been removed on account of this. It is the smallest 'node appears in "
        "at least N trials' filter that would render legibly, offered as a starting "
        "position because roughly 80% of agents appear in exactly one trial and an "
        "unfiltered graph is mostly nodes that say nothing about frequent co-occurrence. "
        "Filter client-side on `Node.weight`, which is that trial count.",
    )
    value_format: str | None = Field(None, description="e.g. 'integer', 'decimal:1'.")


class Visualization(BaseModel):
    """The visualization specification: what to draw, titled, bound and populated.

    Type names are this service's own (`bar`, `line`, `network`) rather than the
    assignment's illustrative `bar_chart` / `time_series` / `network_graph`. The brief
    offers those as examples; a single vocabulary is used here because eight of the eleven
    types it does not name would otherwise have to be invented in a second style.
    """

    model_config = ConfigDict(extra="forbid")

    type: VizType = Field(
        description="Which renderer to use. Chosen by deterministic rules from the result "
        "shape; a model may only pick within the legal set, never outside it."
    )
    title: str = Field(description="Human-readable, derived from the plan rather than "
                                   "written by a model.")
    subtitle: str | None = Field(
        None,
        description="The counting semantics when they are not obvious — 'each trial "
        "counted once per bucket', 'median of enrolment over the trials in that bucket'. "
        "Null when the default reading is correct.",
    )
    encoding: Encoding = Field(
        description="Which key on each datum drives which channel. Read this instead of "
        "assuming key names."
    )
    data: list[Datum] | NetworkData = Field(
        description="A flat list of datums for every type except `network`, which carries "
        "`{nodes, edges}`. The union is the discriminator: a renderer branches on the "
        "shape of `data`, or equivalently on `type == 'network'`."
    )
    config: VizConfig = Field(
        default_factory=VizConfig,
        description="Rendering hints that are not field bindings: sort order, time "
        "granularity, whether an 'Other' bucket exists, value formatting.",
    )


class RecordCounts(BaseModel):
    """The transparency block, which is also the invariant check's own state.

    The same four numbers that guard correctness are the ones reported, so they cannot
    drift apart from each other.

    Invariants (enforced, not merely reported):
      * `used + sum(excluded_by_reason.values()) == retrieved`
      * `retrieved == matched` or `truncated is True`
    """

    model_config = ConfigDict(extra="forbid")

    matched: int = Field(description="totalCount reported by ClinicalTrials.gov for the query.")
    retrieved: int = Field(description="Records actually fetched, after pagination and any cap.")
    used: int = Field(description="Records that survived normalization and local filters.")
    excluded_by_reason: dict[str, int] = Field(
        default_factory=dict,
        description="Every dropped record, counted by reason. Nothing disappears silently.",
    )
    truncated: bool = Field(
        False, description="True when the page cap was hit and the chart is a sample."
    )


class ProbeCall(BaseModel):
    """One planner probe, recorded for transparency.

    Probe results are aggregate counts. They may influence plan fields such as `top_n`,
    but they can never become a chart value — the invariant check enforces that every
    datum traces to an aggregator fold.
    """

    model_config = ConfigDict(extra="forbid")

    tool: str
    args: dict[str, Any]
    result: Any


class Review(BaseModel):
    """What the plan reviewer decided, recorded whether or not it objected.

    An approval used to leave no trace at all — only unactioned concerns produced a
    warning — so "the judge approved this" and "the judge never ran" were
    indistinguishable from the response. In a system whose case rests on its audit trail
    that is the wrong default: a silent reviewer is not evidence of a reviewed plan.
    """

    model_config = ConfigDict(extra="forbid")

    verdict: str = Field(description='"ok" or "concern". A malformed verdict is recorded '
                                     "as given rather than normalised away.")
    concerns: list[str] = Field(default_factory=list)
    revised: bool = Field(
        False, description="Whether the concerns produced a second plan. False with a "
        "non-empty `concerns` means they were raised and not acted on — the reviewer is "
        "advisory and gets one re-plan, not a veto."
    )


class Meta(BaseModel):
    """Everything a reader needs to audit the chart rather than trust it.

    How the plan was reached (`plan`, `planning_trace`, `review`), what was actually asked
    of the registry (`api_requests`, verbatim), what the numbers mean
    (`counting_semantics`, `assumptions`, `warnings`) and what happened to every fetched
    record (`record_counts`).

    `api_requests` is the field that cannot lie about what was issued, which is what makes
    `filters_applied` checkable: a filter reported as applied but compiled to nothing is
    otherwise indistinguishable from one that worked.
    """

    model_config = ConfigDict(extra="forbid")

    interpretation: str = Field(description="Plain-language restatement of what was computed.")
    plan: Plan | None = Field(None, description="Echo of the committed plan.")
    filters_applied: dict[str, Any] = Field(default_factory=dict)
    counting_semantics: str | None = Field(
        None,
        description="Set whenever the counting rule is not 'one trial, one unit' — e.g. a "
        "multi-valued grouping field, where column sums exceed the distinct trial count.",
    )
    record_counts: RecordCounts | None = None
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    planning_trace: list[ProbeCall] = Field(default_factory=list)
    review: Review | None = Field(
        None,
        description="The plan reviewer's verdict, present whenever the reviewer ran. Null "
        "means it did not run at all (a conversational or unsupported response, or a "
        "provider outage), which is a different fact from an approval.",
    )
    api_requests: list[str] = Field(default_factory=list)
    suggested_requests: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Complete, postable request bodies. Populated on `unsupported` so that "
        "a refusal becomes a redirect.",
    )
    generated_at: str
    cache_hit: bool = False
    llm_provider: str | None = None
    elapsed_ms: int | None = None


class AnalyzeResponse(BaseModel):
    """Body of `POST /analyze`."""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    response_type: ResponseType
    answer: str = Field(
        description="One-sentence summary. Templated, with numeric slots filled from the "
        "aggregator — never written by an LLM."
    )
    visualization: Visualization | None = Field(
        None, description="Null if and only if response_type is 'conversational'."
    )
    meta: Meta


class CapabilitiesResponse(BaseModel):
    """Body of `GET /capabilities`. Generated from the field registry, not hand-written."""

    model_config = ConfigDict(extra="forbid")

    fields: list[dict[str, Any]]
    metrics: list[str]
    viz_types: list[str]
    max_legs: int
    limitations: list[str]


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "degraded"]
    ctgov_reachable: bool
    llm_configured: bool
    version: str


Datums = Annotated[list[Datum], Field(description="Flat datum list for non-network charts.")]
