"""One interface over Anthropic and OpenAI.

Four touchpoints in this system talk to a model, and all four want the same thing: a
value of a known Pydantic type, or an honest failure. Nothing here streams, holds a
conversation, or calls tools — the planner's probe tools arrive in a later milestone and
extend `complete` rather than replacing it.

The abstraction is deliberately thin. It exists because the assignment's request was to
support both providers, and because a `Protocol` with one method is the cheapest way to
make the four call sites provider-agnostic — not because a general-purpose LLM framework
was wanted. Anything a provider does that this interface cannot express is a thing this
system does not do.

**Structured output is the whole contract.** Both providers can constrain generation to a
JSON Schema, so a `Plan` that comes back is at least shaped like a `Plan`. That is not the
same as being a *legal* plan — the deterministic validator in `schemas.plan` decides that,
and its errors feed the repair loop. Schema validity is the floor, not the ceiling.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class Provider(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"


class Tier(StrEnum):
    """Which model size a touchpoint needs.

    The split is by *difficulty of the decision*, not by importance. The router and the
    chart selector choose one member of a closed set that deterministic code has already
    constrained, so a small model cannot produce an illegal answer — only a suboptimal one.
    The planner and the judge reason over the whole query and the field registry, which is
    where model quality actually shows up.
    """

    SMALL = "small"
    LARGE = "large"


class LLMError(RuntimeError):
    """The model could not be reached, or returned something unusable.

    Every touchpoint decides its own response to this. The router defaults to in-domain, the
    chart selector falls back to the rules' default, and the planner surfaces it — a system
    that cannot plan has nothing honest to draw.
    """


@dataclass(frozen=True)
class LLMSettings:
    """Provider configuration, read once at startup."""

    provider: Provider
    api_key: str
    model_small: str
    model_large: str

    @classmethod
    def from_env(cls) -> LLMSettings:
        """Build settings from the environment, failing loudly on a missing key.

        Raises:
            LLMError: if the provider is unknown or its key is absent. The assignment's
                system requires an API key — there is no heuristic planner to fall back
                to — so this is a startup failure rather than a degraded mode.
        """
        raw = os.getenv("LLM_PROVIDER", Provider.ANTHROPIC.value).strip().lower()
        try:
            provider = Provider(raw)
        except ValueError:
            raise LLMError(
                f"LLM_PROVIDER={raw!r} is not supported; use one of: "
                f"{', '.join(p.value for p in Provider)}"
            ) from None

        prefix = provider.value.upper()
        key = os.getenv(f"{prefix}_API_KEY", "").strip()
        if not key:
            raise LLMError(
                f"{prefix}_API_KEY is not set. This service requires an LLM API key; see "
                f".env.example."
            )

        defaults = _DEFAULT_MODELS[provider]
        return cls(
            provider=provider,
            api_key=key,
            model_small=os.getenv(f"{prefix}_MODEL_SMALL", defaults[Tier.SMALL]).strip(),
            model_large=os.getenv(f"{prefix}_MODEL_LARGE", defaults[Tier.LARGE]).strip(),
        )

    def model_for(self, tier: Tier) -> str:
        return self.model_small if tier is Tier.SMALL else self.model_large


#: Fallbacks when the environment names a provider but not its models. Kept here rather
#: than only in `.env.example` so the service starts with a working configuration.
_DEFAULT_MODELS: dict[Provider, dict[Tier, str]] = {
    Provider.ANTHROPIC: {Tier.SMALL: "claude-haiku-4-5", Tier.LARGE: "claude-opus-5"},
    Provider.OPENAI: {Tier.SMALL: "gpt-5.4-mini", Tier.LARGE: "gpt-5.4"},
}


class LLMClient(Protocol):
    """What the four touchpoints require of a provider."""

    settings: LLMSettings

    async def complete(
        self,
        *,
        system: str,
        user: str,
        schema: type[T],
        tier: Tier,
        max_tokens: int = 2048,
        drop: frozenset[str] = frozenset(),
    ) -> T: ...


# --------------------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------------------


def _validate(schema: type[T], payload: str | dict[str, Any]) -> T:
    """Parse and validate a model's output, raising `LLMError` on anything unusable.

    Pydantic's own error text is preserved rather than replaced. For the planner it is
    handed straight back to the model as repair feedback, and "field required" is more
    actionable than any message this layer could invent.
    """
    try:
        data = json.loads(payload) if isinstance(payload, str) else payload
    except json.JSONDecodeError as exc:
        raise LLMError(f"model returned text that is not JSON: {exc}") from exc
    try:
        return schema.model_validate(data)
    except ValidationError as exc:
        raise LLMError(f"model output does not fit {schema.__name__}: {exc}") from exc


#: Keywords Pydantic emits that at least one provider rejects. Both refusals were observed
#: against the live APIs rather than inferred from documentation:
#:
#:   Anthropic: "For 'array' type, property 'maxItems' is not supported"
#:   OpenAI:    "$ref cannot have keywords {'default'}"
#:
#: Dropping them does not weaken validation. Pydantic still enforces every one of these
#: constraints when the response is parsed, so a model that returns seven legs against a
#: six-leg limit fails in `_validate` and its error becomes repair-loop feedback. The
#: constraint moves from generation-time to validation-time; it does not disappear.
_UNSUPPORTED_KEYWORDS = frozenset(
    {
        "default",
        "title",
        "minItems",
        "maxItems",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minLength",
        "maxLength",
        "pattern",
        "multipleOf",
        "format",
    }
)


class Optionality(StrEnum):
    """How a schema expresses "this field may be left unset".

    The two providers want opposite idioms, and neither accepts the other's:

    * **OpenAI strict mode** requires every property to appear in `required`, so an
      optional field is expressed as required-but-nullable — `anyOf [T, null]`.
    * **Anthropic** does not require that, and caps a schema at 16 union-typed
      parameters. `Plan` has 24 nullable fields, so the OpenAI idiom is rejected outright:
      *"Schemas contains too many parameters with union types (24 parameters with type
      arrays or anyOf) … limit: 16."* Expressing optionality as absence — omit the field
      from `required` and drop the `null` branch — removes the unions entirely.

    Both idioms describe the same Pydantic model, which accepts an absent field with a
    default and an explicit null alike. Neither weakens validation.
    """

    NULLABLE = "nullable"
    OMITTABLE = "omittable"


def json_schema_for(
    schema: type[BaseModel],
    optionality: Optionality = Optionality.NULLABLE,
    drop: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """The JSON Schema a provider should constrain generation to.

    Pydantic's output needs three transformations before either provider accepts it:

    1. **`$ref`s are inlined.** OpenAI rejects a `$ref` carrying sibling keywords, which
       Pydantic emits for any field with both an enum type and a default. Inlining removes
       the construct entirely rather than trying to rearrange it.
    2. **Unsupported keywords are stripped** — see `_UNSUPPORTED_KEYWORDS`.
    3. **Objects are closed** with `additionalProperties: false`, and optionality is
       rendered in the requested idiom — see `Optionality`.

    Args:
        drop: Property names to omit from the schema entirely, at any nesting level. Used
            to fit a provider's schema-size limits; a dropped field keeps its Pydantic
            default, so the model simply cannot choose it. Only ever pass fields whose
            default is correct or derivable — a dropped field is a decision the model no
            longer gets to make, not one that disappears.
    """
    generated = schema.model_json_schema()
    definitions = generated.pop("$defs", {})
    return _normalize(generated, definitions, optionality, drop, seen=())


def _collapse_null(node: dict[str, Any]) -> dict[str, Any]:
    """Turn `anyOf [T, null]` into `T`, leaving genuine unions alone."""
    branches = node.get("anyOf")
    if not isinstance(branches, list):
        return node
    concrete = [b for b in branches if not (isinstance(b, dict) and b.get("type") == "null")]
    if len(concrete) != 1 or len(concrete) == len(branches):
        return node
    merged = dict(concrete[0])
    for key, value in node.items():
        if key != "anyOf":
            merged.setdefault(key, value)
    return merged


def _normalize(
    node: Any,
    definitions: dict[str, Any],
    optionality: Optionality,
    drop: frozenset[str],
    seen: tuple[str, ...],
) -> Any:
    """Inline references, drop unsupported keywords, and close objects."""
    if isinstance(node, list):
        return [_normalize(item, definitions, optionality, drop, seen) for item in node]
    if not isinstance(node, dict):
        return node

    # Collapse before resolving: `anyOf [{$ref}, null]` hides a reference one level down,
    # and dropping the null branch lifts it up to where it can be inlined.
    if optionality is Optionality.OMITTABLE:
        node = _collapse_null(node)

    ref = node.get("$ref")
    if isinstance(ref, str):
        name = ref.rsplit("/", 1)[-1]
        if name in seen:
            # No model here is recursive, and a silently truncated schema would be worse
            # than a loud failure: the model would be constrained to something other than
            # what the code validates against.
            raise LLMError(
                f"schema for {name!r} is recursive, which structured outputs cannot express"
            )
        target = definitions.get(name)
        if target is None:
            raise LLMError(f"schema reference {ref!r} could not be resolved")
        # Sibling keywords beside a `$ref` (a default, a description) are dropped with the
        # reference itself; the inlined definition carries the meaning.
        return _normalize(target, definitions, optionality, drop, (*seen, name))

    cleaned = {
        key: _normalize(value, definitions, optionality, drop, seen)
        for key, value in node.items()
        if key not in _UNSUPPORTED_KEYWORDS
    }
    if drop and isinstance(cleaned.get("properties"), dict):
        cleaned["properties"] = {
            key: value for key, value in cleaned["properties"].items() if key not in drop
        }
    if cleaned.get("type") == "object" and "properties" in cleaned:
        cleaned["additionalProperties"] = False
        if optionality is Optionality.NULLABLE:
            cleaned["required"] = list(cleaned["properties"])
        else:
            # Keep Pydantic's own required list: the fields with no default, and only
            # those. Everything else may simply be omitted.
            cleaned["required"] = [
                key for key in cleaned.get("required", []) if key in cleaned["properties"]
            ]
    return cleaned


# --------------------------------------------------------------------------------------
# Anthropic
# --------------------------------------------------------------------------------------


class AnthropicClient:
    """Anthropic implementation, using the Messages API's structured outputs."""

    def __init__(self, settings: LLMSettings) -> None:
        from anthropic import AsyncAnthropic

        self.settings = settings
        self._client = AsyncAnthropic(api_key=settings.api_key)

    async def complete(
        self,
        *,
        system: str,
        user: str,
        schema: type[T],
        tier: Tier,
        max_tokens: int = 2048,
        drop: frozenset[str] = frozenset(),
    ) -> T:
        import anthropic

        model = self.settings.model_for(tier)
        try:
            response = await self._client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={
                    "format": {
                        "type": "json_schema",
                        "schema": json_schema_for(schema, Optionality.OMITTABLE, drop),
                    }
                },
            )
        except anthropic.APIStatusError as exc:
            raise LLMError(f"anthropic {model} returned {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"could not reach anthropic: {exc}") from exc

        # A refusal is a real outcome, not a malformed response: the model declined rather
        # than failed, and `content` is empty or partial. Reading content[0] would raise
        # an IndexError that says nothing about what happened.
        if response.stop_reason == "refusal":
            raise LLMError(f"anthropic {model} declined the request")

        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise LLMError(f"anthropic {model} returned no text block")
        return _validate(schema, text)


# --------------------------------------------------------------------------------------
# OpenAI
# --------------------------------------------------------------------------------------


class OpenAIClient:
    """OpenAI implementation, using `response_format` with a strict JSON schema."""

    def __init__(self, settings: LLMSettings) -> None:
        from openai import AsyncOpenAI

        self.settings = settings
        self._client = AsyncOpenAI(api_key=settings.api_key)

    async def complete(
        self,
        *,
        system: str,
        user: str,
        schema: type[T],
        tier: Tier,
        max_tokens: int = 2048,
        drop: frozenset[str] = frozenset(),
    ) -> T:
        import openai

        model = self.settings.model_for(tier)
        try:
            response = await self._client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": schema.__name__.lower(),
                        "strict": True,
                        "schema": json_schema_for(schema, Optionality.NULLABLE, drop),
                    },
                },
                max_completion_tokens=max_tokens,
            )
        except openai.APIStatusError as exc:
            raise LLMError(f"openai {model} returned {exc.status_code}: {exc}") from exc
        except openai.APIConnectionError as exc:
            raise LLMError(f"could not reach openai: {exc}") from exc

        choice = response.choices[0]
        if choice.message.refusal:
            raise LLMError(f"openai {model} declined the request: {choice.message.refusal}")
        if not choice.message.content:
            raise LLMError(f"openai {model} returned an empty response")
        return _validate(schema, choice.message.content)


def build_client(settings: LLMSettings | None = None) -> LLMClient:
    """Construct the configured provider's client."""
    settings = settings or LLMSettings.from_env()
    if settings.provider is Provider.ANTHROPIC:
        return AnthropicClient(settings)
    return OpenAIClient(settings)


__all__ = [
    "AnthropicClient",
    "LLMClient",
    "LLMError",
    "LLMSettings",
    "OpenAIClient",
    "Optionality",
    "Provider",
    "Tier",
    "build_client",
    "json_schema_for",
]
