"""Deep citations: excerpts located, verified, and dropped rather than guessed.

The assignment asks each visualized datum to reference the trial records behind it, with
"an exact text excerpt from the API response (or a specific field/value) that supports the
datum". Two properties make that claim worth anything, and both are enforced here:

1. **Nothing is generated.** An excerpt is a literal substring of the record the registry
   returned, taken at recorded offsets. No model writes one, and no string is assembled
   from parts.
2. **Every excerpt is re-verified before it ships.** The payload is re-sliced at the
   offsets and compared. A citation that fails is dropped, never emitted — an unverifiable
   citation is worse than none, because it looks like evidence.

**What the offsets index into.** The client parses JSON, so the original wire bytes for a
single record are not retained. Offsets are therefore into the record re-serialized
canonically:

    json.dumps(record, separators=(",", ":"), ensure_ascii=False)

That is stated in the response schema and the README so a reader can rebuild the exact
string and check any span by hand. `serialize` below produces it, and a test asserts it is
byte-identical to that `json.dumps` call.

**Which text is cited.** Preference order, both forms verified identically:

* A **prose span** from the trial's title, when the supporting value literally appears
  there — closest to the assignment's illustrative example.
* Otherwise the **JSON span** at the field that put the trial in its bucket, e.g.
  `"phases":["PHASE3"]` — the parenthetical "specific field/value".

Prose is the minority, and deliberately so rather than by omission. Measured over 200
melanoma Phase 3 trials, the grouping value appears in the title for 83% of intervention
groupings and 82% of conditions, but only 58% of phases, **1% of sponsors and 0% of
countries**. A sponsor bar chart has essentially no citable prose, which is why the JSON
span is the backbone: it exists for every datum, because the value is *why* the trial is
in that bucket.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

#: Characters of context kept around a matched value in a prose excerpt. An official title
#: can run past 200 characters, and quoting all of it buries the part that supports the
#: datum. The offsets stay exact — this only chooses a narrower span of the same payload.
PROSE_WINDOW = 60

#: Longest excerpt worth emitting. A composite label's components can sit far apart in the
#: record — two drugs in a combination may be separated by every other intervention — and
#: the smallest span containing both is then the entire `interventions` array, measured at
#: 1,435 characters on NCT02224781. That is a true substring and useless as evidence.
MAX_EXCERPT_CHARS = 400

#: Paths whose text is worth quoting as prose, in preference order.
_PROSE_PATHS = (
    "protocolSection.identificationModule.briefTitle",
    "protocolSection.identificationModule.officialTitle",
)


@dataclass(frozen=True)
class Excerpt:
    """A located span, before verification."""

    text: str
    start: int
    end: int
    kind: str  # "prose" | "field"


def serialize(record: dict[str, Any]) -> str:
    """The canonical payload that offsets index into."""
    return json.dumps(record, separators=(",", ":"), ensure_ascii=False)


# --------------------------------------------------------------------------------------
# Span index
# --------------------------------------------------------------------------------------


def index_spans(record: dict[str, Any]) -> tuple[str, dict[str, tuple[int, int]]]:
    """Serialize a record and record the span of every node in it.

    Offsets are *computed while writing* rather than searched for afterwards. Searching
    would be ambiguous — `"name"` occurs under the lead sponsor, every collaborator and
    every intervention — and an excerpt pointing at the wrong `"name"` would be a wrong
    citation that still verifies, which is the one failure this module must not have.

    A dict member's span includes its key, so the excerpt reads `"phases":["PHASE3"]`
    rather than a bare `["PHASE3"]` that says nothing about what field it came from.
    """
    parts: list[str] = []
    spans: dict[str, tuple[int, int]] = {}
    position = 0

    def emit(text: str) -> None:
        nonlocal position
        parts.append(text)
        position += len(text)

    def write(node: Any, path: str, key_prefix: str = "") -> None:
        start = position
        emit(key_prefix)
        if isinstance(node, dict):
            emit("{")
            for index, (key, value) in enumerate(node.items()):
                if index:
                    emit(",")
                child = f"{path}.{key}" if path else key
                write(value, child, f"{json.dumps(key, ensure_ascii=False)}:")
            emit("}")
        elif isinstance(node, list):
            emit("[")
            for index, item in enumerate(node):
                if index:
                    emit(",")
                write(item, f"{path}[{index}]")
            emit("]")
        else:
            emit(json.dumps(node, ensure_ascii=False))
        if path:
            spans[path] = (start, position)

    write(record, "")
    return "".join(parts), spans


# --------------------------------------------------------------------------------------
# Locating an excerpt
# --------------------------------------------------------------------------------------


def _humanized(value: str) -> list[str]:
    """Renderings of a coded value that might appear in prose.

    `PHASE3` is never written that way in a title; "Phase 3" and "Phase III" both are.
    Without this the phase coverage measured at 58% would collapse to nearly zero, since
    no title contains the literal enum member.

    The variants only decide *where to look*. The excerpt itself is still whatever the
    payload literally says at the matched offsets, so a variant never invents text.
    """
    variants = [value]
    match = re.fullmatch(r"(EARLY_)?PHASE(\d)", value)
    if match:
        number = match.group(2)
        roman = {"1": "I", "2": "II", "3": "III", "4": "IV"}.get(number, number)
        prefix = "Early Phase " if match.group(1) else "Phase "
        variants += [f"{prefix}{number}", f"{prefix}{roman}"]
    return variants


def _find_prose(payload: str, spans: dict[str, tuple[int, int]], value: str) -> Excerpt | None:
    """A window of the trial's title containing the supporting value, if it says it."""
    for path in _PROSE_PATHS:
        span = spans.get(path)
        if span is None:
            continue
        # The recorded span covers the whole member, `"briefTitle":"…"`. Narrow it to the
        # string's contents so a window can never straddle the key and emit a fragment
        # like `itle":"Dabrafenib…`, which is a true substring and still unreadable.
        start, end = span
        member = payload[start:end]
        colon = member.find(":")
        if colon == -1 or not member[colon + 1 :].startswith('"'):
            continue
        start, end = start + colon + 2, end - 1
        text = payload[start:end]

        for variant in _humanized(value):
            at = text.lower().find(variant.lower())
            if at == -1:
                continue
            # Clamp to the title's own span so the excerpt can never bleed into a
            # neighbouring field and appear to say something the title does not.
            low = max(start, start + at - PROSE_WINDOW)
            high = min(end, start + at + len(variant) + PROSE_WINDOW)
            return Excerpt(payload[low:high], low, high, "prose")
    return None


#: Separators the normalizer uses to build one bucket label out of several source values:
#: `PHASE1|PHASE2` for a multi-phase trial, `A || B` for agents sharing an arm group.
_COMPOSITE_SEPARATORS = (" || ", "|")


def _components(value: str) -> list[str]:
    """The source values a composite bucket label was built from.

    A composite label is never a literal substring of the record — the registry stores
    `["PHASE1","PHASE2"]`, not `"PHASE1|PHASE2"` — so requiring a literal match would drop
    a citation for every multi-phase trial and every drug combination, which are among the
    most interesting datums in the system.
    """
    for separator in _COMPOSITE_SEPARATORS:
        if separator in value:
            return [part for part in value.split(separator) if part]
    return [value]


def _supports(text: str, value: str) -> bool:
    """Whether a span genuinely states the value, component by component for composites.

    A composite is supported only when *every* component is present; two of three drugs is
    a different regimen, not a partial citation.
    """
    return all(json.dumps(part, ensure_ascii=False) in text for part in _components(value))


def _find_field(
    payload: str,
    spans: dict[str, tuple[int, int]],
    field_path: str,
    value: str,
) -> Excerpt | None:
    """The span of the field that put this trial in its bucket.

    The plan's `field_path` is tried first. It does not always resolve, and that is
    expected rather than a bug: the normalizer deduplicates multi-valued fields, so the
    third distinct country a trial ran in is not the third entry of `locations[]`. When
    the path misses, the value is located instead — every path whose leaf equals the value
    is a place the record genuinely says it.
    """
    encoded = json.dumps(value, ensure_ascii=False)

    span = spans.get(field_path)
    if span is not None and _supports(payload[span[0] : span[1]], value):
        return Excerpt(payload[span[0] : span[1]], span[0], span[1], "field")

    # A resolving path is not sufficient. Deduplication means the third distinct country
    # is not `locations[2]`, so a path can resolve to a *different* value than the datum
    # claims — and that excerpt would verify, because its offsets are internally
    # consistent. Verification proves the text is really there; only this check proves it
    # is the text that supports the datum.
    #
    # Fall back to any leaf holding this exact value, preferring the one nearest the
    # declared path so a citation stays inside the module it claims to come from.
    prefix = field_path.split("[")[0]
    candidates = [
        (path, start, end)
        for path, (start, end) in spans.items()
        if payload[start:end].endswith(encoded)
    ]
    if not candidates and len(_components(value)) > 1:
        # A composite has no single leaf. Prefer the smallest span stating every
        # component; when even that is too long to read, fall back to the narrowest span
        # showing one component in context — for a combination that is the intervention
        # entry carrying its `armGroupLabels`, which is the linkage itself and far better
        # evidence than the whole array it sits in.
        whole = sorted(
            (
                (path, start, end)
                for path, (start, end) in spans.items()
                if _supports(payload[start:end], value)
            ),
            key=lambda c: c[2] - c[1],
        )
        candidates = [c for c in whole if c[2] - c[1] <= MAX_EXCERPT_CHARS][:1]
        # Only shorten a composite the record *does* state. Without this guard a composite
        # the record only partly contains — `PHASE1|PHASE4` against a Phase 1/2 trial —
        # would fall through and be cited from its first component alone, asserting a
        # regimen that does not exist.
        if whole and not candidates:
            first = json.dumps(_components(value)[0], ensure_ascii=False)
            # Widest span that still fits, not the narrowest. The narrowest is the bare
            # `"name":"Ipilimumab"` leaf, which proves the agent is in the trial and says
            # nothing about what it was given with. One level out is the intervention
            # object carrying `armGroupLabels` — the linkage the edge actually asserts.
            candidates = sorted(
                (
                    (path, start, end)
                    for path, (start, end) in spans.items()
                    if first in payload[start:end] and end - start <= MAX_EXCERPT_CHARS
                ),
                key=lambda c: c[1] - c[2],
            )[:1]
    if not candidates:
        return None
    candidates.sort(key=lambda c: (not c[0].startswith(prefix), len(c[0])))
    path, start, end = candidates[0]
    return Excerpt(payload[start:end], start, end, "field")


def locate(
    record: dict[str, Any],
    field_path: str,
    field_value: str,
) -> tuple[str, Excerpt] | None:
    """Find the best verifiable excerpt supporting one datum.

    Returns the payload alongside the excerpt so the caller can verify without
    re-serializing, or None when the record says nothing that supports the datum — in
    which case no citation is emitted for it.
    """
    payload, spans = index_spans(record)
    found = _find_prose(payload, spans, field_value) or _find_field(
        payload, spans, field_path, field_value
    )
    return (payload, found) if found else None


def verify(payload: str, excerpt: Excerpt) -> bool:
    """Re-slice the payload and confirm the excerpt is what is there.

    The whole citation claim rests on this one line. It is cheap, and it catches the
    failure mode that matters: an offset that drifted, so the response would carry text
    the record does not contain at the position it names.
    """
    return (
        0 <= excerpt.start < excerpt.end <= len(payload)
        and payload[excerpt.start : excerpt.end] == excerpt.text
        and excerpt.text != ""
    )


__all__ = [
    "PROSE_WINDOW",
    "Excerpt",
    "index_spans",
    "locate",
    "serialize",
    "verify",
]
