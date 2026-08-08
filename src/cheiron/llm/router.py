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
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from cheiron.llm.client import LLMClient, LLMError, Tier

log = logging.getLogger(__name__)


class Intent(StrEnum):
    """What kind of message this is, and therefore which response shape it gets."""

    QUESTION = "question"
    CONVERSATIONAL = "conversational"
    UNSUPPORTED = "unsupported"


class RouterVerdict(BaseModel):
    """What the router decides."""

    model_config = ConfigDict(extra="forbid")

    intent: str = Field(
        description="One of: question, conversational, unsupported.",
    )
    reply: str = Field(
        default="",
        description="A brief conversational reply. Used only for intent=conversational.",
    )
    reason: str = Field(
        default="",
        description="Why the registry cannot answer this, naming the specific obstruction "
        "in the data rather than a generic refusal. Used only for intent=unsupported.",
    )
    suggestions: list[str] = Field(
        default_factory=list,
        description="Up to three related questions this system *can* answer, phrased as "
        "complete questions a user could send unchanged. Used only for unsupported.",
    )

    @property
    def in_domain(self) -> bool:
        """Whether the pipeline should run. Anything not explicitly diverted proceeds."""
        return self.intent not in (Intent.CONVERSATIONAL, Intent.UNSUPPORTED)


SYSTEM_PROMPT = """\
You classify a message into exactly one of three intents.

**question** — anything answerable from ClinicalTrials.gov registration records: counts,
trends over time, distributions across phases or countries, comparisons between drugs or
conditions, sponsors, enrolment figures, study types, networks of co-occurring entities.
This is the default and by far the most common. Choose it whenever the question is about
trials, even if you are unsure the system supports that exact framing.

**conversational** — greetings, thanks, questions about you or your abilities, anything
with no informational request about trials. Write one or two sentences in `reply` that
answer naturally and mention you chart ClinicalTrials.gov data.

**unsupported** — the registry genuinely does not hold what is being asked. Only these:

- COMPARATIVE EFFICACY ("which drug works better", "is X more effective than Y"). Posted
  results exist, but each sponsor defines its own endpoints, units and analysis windows, so
  there is no comparable field meaning "worked better". Safety *volume* can be compared —
  serious adverse events and deaths against their denominators — which is a different
  question and is answerable.
- INDIVIDUAL PARTICIPANTS ("which patients responded", "list the participants"). Records
  are aggregate: trial-level, and for trials with posted results, arm-level. Aggregate
  demographics *are* available — baseline age and sex counts — so questions about the
  typical age or sex balance of a population ARE answerable and are questions, not this.
- ENROLMENT BY PLACE ("how many patients were enrolled in France"). Enrolment is recorded
  once per trial, not per site, so a multi-country trial has no per-country figure.
- ELIGIBILITY TEXT ("trials that exclude diabetics"). Eligibility criteria are free prose
  and are not searched semantically.
- SPECIFIC OUTCOME VALUES ("what was the response rate", "what was median PFS"). Posted
  results are read for participant flow, adverse events and baseline demographics, but not
  for outcome measures: sponsors define their own endpoints, so 25 melanoma trials produced
  144 distinct measure titles in 34 different units. There is nothing to aggregate.

For unsupported, put the specific obstruction in `reason` — name what the registry records
instead, so the person understands the limit rather than just meeting a refusal. Then give
up to three `suggestions`: complete questions, answerable from registration data, close to
what they actually wanted. Someone asking which drug works better is usually interested in
that drug, so suggest what *can* be shown about it — trial counts by phase, how activity
changed over time, who sponsors it.

When genuinely unsure between question and unsupported, choose question. A question wrongly
refused looks broken; one wrongly attempted returns a chart with a caveat."""


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
        return RouterVerdict(intent=Intent.QUESTION)


__all__ = ["SYSTEM_PROMPT", "Intent", "RouterVerdict", "route"]
