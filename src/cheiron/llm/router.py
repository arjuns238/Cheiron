"""① Router: is this a question about clinical trials, or is it chit-chat?

The only gate before retrieval, and the only stage that can end a request without touching
ClinicalTrials.gov. "hi" costs one cheap classification and zero API requests.

**It fails open.** An unreachable or confused model routes the query as in-domain, because
the two failure modes are not symmetric: routing chit-chat into the pipeline wastes a few
API calls and produces an empty chart, while refusing a real question produces a system
that appears broken for a query it could have answered. `plan.md` states the same
preference — a wrong chart beats a wrong refusal.

The bar for `false` is deliberately high. Anything naming a trial, drug, condition,
sponsor, country, or any measurable property of a study is in-domain, including questions
the planner will later decide it cannot answer. Deciding *supportability* is the planner's
and the judge's job; this stage only separates questions from greetings.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, ConfigDict, Field

from cheiron.llm.client import LLMClient, LLMError, Tier

log = logging.getLogger(__name__)


class RouterVerdict(BaseModel):
    """What the router decides."""

    model_config = ConfigDict(extra="forbid")

    in_domain: bool = Field(
        description="True when the message asks something about clinical trials data."
    )
    reply: str = Field(
        default="",
        description="A brief conversational reply, used only when in_domain is false.",
    )


SYSTEM_PROMPT = """\
You decide whether a message is a question about clinical trials data, or ordinary
conversation.

Answer in_domain=true for anything asking about trials, drugs, interventions, conditions,
sponsors, countries, phases, enrollment, dates, or any other property of clinical studies —
including questions this system may turn out not to support. Deciding what is answerable
happens later; you are only separating questions from conversation.

Answer in_domain=false only for greetings, thanks, questions about you or your abilities,
and messages with no informational request about trials at all. When you do, write one or
two sentences in `reply` that answer conversationally and say the system can chart clinical
trials data from ClinicalTrials.gov.

When genuinely unsure, choose true. A question wrongly refused looks broken; a greeting
wrongly analysed merely returns nothing."""


async def route(client: LLMClient, query: str) -> RouterVerdict:
    """Classify one message. Never raises — an unusable answer means in-domain."""
    try:
        return await client.complete(
            system=SYSTEM_PROMPT,
            user=query,
            schema=RouterVerdict,
            tier=Tier.SMALL,
            max_tokens=256,
        )
    except LLMError as exc:
        log.warning("router unavailable, defaulting to in-domain: %s", exc)
        return RouterVerdict(in_domain=True)


__all__ = ["SYSTEM_PROMPT", "RouterVerdict", "route"]
