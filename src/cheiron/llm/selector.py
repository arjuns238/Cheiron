"""④ Chart selector: pick a chart from the set the rules already permit.

This is the safest LLM call in the system, and deliberately the least powerful. The viz
rules have already computed which chart types would render the result honestly; the model
chooses among them and nothing else. Its answer is validated against that set, so the worst
outcome is falling back to the rule's own default — it cannot produce an illegal chart.

**It earns its place because the rules cannot see the phrasing.** `[line, bar]` are both
defensible for the same yearly counts: "how has X changed" wants a line, "which year had
the most" wants a bar. Nothing in the aggregation distinguishes them. Only the question
does, and only this stage reads it.

**It sees shape, never values.** The `Shape` handed over carries bucket counts, dimension
kinds and label samples — no bucket value appears in it, which is asserted by a test. A
model that cannot read a number cannot write one into the output.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, ConfigDict, Field

from cheiron.llm.client import LLMClient, LLMError, Tier
from cheiron.schemas.response import VizType
from cheiron.viz.rules import Shape, choose, legal_charts

log = logging.getLogger(__name__)


class ChartChoice(BaseModel):
    """The selector's pick."""

    model_config = ConfigDict(extra="forbid")

    chart: str = Field(description="One chart type, copied exactly from the legal set.")
    reason: str = Field(
        default="",
        description="One short sentence on why this chart suits the question's phrasing.",
    )


SYSTEM_PROMPT = """\
You choose how to draw a result that has already been computed.

You are given the question, a description of the result's shape, and the list of chart
types that would render it honestly. Pick exactly one from that list. You may not propose
anything outside it — the list was derived from the data's actual shape, and a chart
outside it would misrepresent the result.

Choose on the question's phrasing, which is the one thing the shape cannot tell you:

- "how has X changed", "trend", "over time" → line
- "which had the most", "compare across", ranking language → bar
- "where", "which countries", "geographic distribution" → choropleth, whenever it is
  offered. A map answers a question about place directly; a bar chart of country names
  makes the reader reconstruct the geography themselves. Prefer bar only when the question
  is explicitly a ranking ("the top five countries", "which country leads").
- a part-to-whole reading of a small closed set → pie, but only if offered
- a comparison of several series over time → stacked_area for composition, grouped_bar for
  reading individual values

If the list has one entry, return it. If the phrasing does not favour any option, return
the first — it is the rules' own default and the safest reading of the shape."""


def build_prompt(query: str, shape: Shape, legal: tuple[VizType, ...]) -> str:
    """Question plus shape plus the legal set. No values, by construction."""
    lines = [
        f"QUESTION: {query}",
        "",
        "RESULT SHAPE:",
        f"  grouping dimension: {shape.group_field or 'none'} "
        f"({shape.group_kind.value if shape.group_kind else 'none'})",
        f"  buckets: {shape.bucket_count}",
        f"  series: {shape.series_count}",
        f"  binned: {shape.binned}",
        f"  has an 'Other' bucket: {shape.has_other}",
        f"  example bucket labels: {', '.join(shape.sample_labels) or 'none'}",
        "",
        f"LEGAL CHART TYPES: {', '.join(c.value for c in legal)}",
    ]
    return "\n".join(lines)


async def select(
    client: LLMClient, query: str, shape: Shape, *, legal: tuple[VizType, ...] | None = None
) -> VizType:
    """Choose a chart type. Never raises — any failure falls back to the rules' default.

    The returned value is passed through `choose`, so membership is enforced here rather
    than trusted. A model that answers "pie" for a time series gets the rule's default and
    the user gets a correct chart.
    """
    legal = legal or legal_charts(shape)
    if len(legal) == 1:
        return legal[0]

    try:
        choice = await client.complete(
            system=SYSTEM_PROMPT,
            user=build_prompt(query, shape, legal),
            schema=ChartChoice,
            tier=Tier.SMALL,
            max_tokens=256,
        )
    except LLMError as exc:
        log.warning("chart selector unavailable, using the rules' default: %s", exc)
        return legal[0]

    picked = choose(shape, choice.chart)
    if picked.value != choice.chart:
        log.info("chart selector proposed %r, outside the legal set", choice.chart)
    return picked


__all__ = ["SYSTEM_PROMPT", "ChartChoice", "build_prompt", "select"]
