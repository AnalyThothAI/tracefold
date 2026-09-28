"""One deterministic reading view for extraction, citation checks and work identity.

Evidence remains the complete immutable source. A view only describes the spans
shown to this Event's semantic task; it is never a replacement Evidence.
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..events.facts import FactUnit, extract_fact_units, source_blocks
from .contracts import Evidence, ExtractionScope, FrozenInput
from .identity import digest, identity

log = logging.getLogger("tracefold.news")


def item_text(item: Mapping[str, Any]) -> str:
    text = str(item.get("evidence_text") or "").strip()
    if text:
        return text
    return "\n".join(
        part for part in (str(item.get("title") or "").strip(), str(item.get("description") or "").strip()) if part
    )


def extraction_scopes(material: Mapping[str, Any], evidence: Sequence[Evidence]) -> tuple[ExtractionScope, ...]:
    """Join immutable member FactUnits to every new body of their own Item."""

    scopes = []
    facts = material.get("fact_scopes") or {}
    items = {str(row["item_id"]): row for row in material.get("items") or ()}
    recovered: dict[str, dict[str, FactUnit]] = {}
    for item in evidence:
        for member in material.get("members") or ():
            if str(member["item_id"]) != item.source.record_id:
                continue
            fact_id = str(member["fact_id"])
            fact = facts.get(fact_id)
            if fact is None:
                item_id = str(member["item_id"])
                if item_id not in recovered:
                    original = items.get(item_id)
                    recovered[item_id] = (
                        {}
                        if original is None
                        else {
                            unit.fact_id: unit
                            for unit in extract_fact_units(
                                item_id=item_id,
                                raw_text=item_text(original),
                                fallback_title=str(original.get("title") or ""),
                            )
                        }
                    )
                unit = recovered[item_id].get(fact_id)
                if unit is None or unit.text != str(member["fact_text"]):
                    log.warning("news_member_fact_scope_unresolved", extra={"item_id": item_id, "fact_id": fact_id})
                    continue
                fact = unit.as_dict()
            if fact.get("method", "whole_item") == "whole_item":
                continue
            scopes.append(
                ExtractionScope(
                    evidence_ref=item.ref,
                    fact_id=fact_id,
                    fact_text=str(member["fact_text"]),
                    context=str(fact.get("context") or ""),
                    method=str(fact["method"]),
                )
            )
    return tuple(scopes)


PROJECTION_VERSION = "news_task_projection_v2"
_NUMBERED_RE = re.compile(r"^\s*\d{1,2}[.)、:：]\s*\S")
_CLOCK_RE = re.compile(r"^\s*\d{1,2}[:：]\d{2}(?!\d)")
_SPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class ReadingSpan:
    start: int
    end: int
    role: str
    text: str

    def as_dict(self) -> dict[str, object]:
        return {"start": self.start, "end": self.end, "role": self.role, "text": self.text}


@dataclass(frozen=True, slots=True)
class ReadingView:
    evidence_ref: str
    source_version: str
    mode: str
    reason: str | None
    spans: tuple[ReadingSpan, ...]
    material_sha: str
    read_ref: str

    def as_dict(self, evidence: Evidence) -> dict[str, object]:
        return {
            "ref": self.evidence_ref,
            "source": evidence.source.model_dump(mode="json"),
            "mode": self.mode,
            "reason": self.reason,
            "segments": [span.as_dict() for span in self.spans],
            "material_sha": self.material_sha,
        }


def reading_view(event_id: str, evidence: Evidence, scopes: tuple[ExtractionScope, ...]) -> ReadingView:
    """Locate complete task blocks in this exact source version or show it whole."""

    scopes = tuple(scope for scope in scopes if scope.evidence_ref == evidence.ref)
    text = evidence.text
    mode = "whole"
    reason: str | None = None
    ranges: list[tuple[int, int, str]] = []
    if scopes:
        blocks = source_blocks(text)
        numbered = [
            (index, block, start, end)
            for index, (block, start, end) in enumerate(blocks)
            if _NUMBERED_RE.match(block) and not _CLOCK_RE.match(block)
        ]
        chosen: set[int] = set()
        for scope in scopes:
            anchor = _SPACE_RE.sub(" ", html.unescape(scope.fact_text)).strip()
            matches = [index for index, block, _start, _end in numbered if anchor and anchor in block]
            if len(matches) != 1:
                reason = "task_anchor_not_unique_in_source_version"
                break
            chosen.add(matches[0])
        if reason is None and numbered:
            mode = "scoped"
            first_start = numbered[0][2]
            last_end = numbered[-1][3]
            if first_start:
                ranges.append((0, first_start, "context"))
            for position, (block_index, _block, start, _end) in enumerate(numbered):
                if block_index not in chosen:
                    continue
                # The final numbered fact owns its following continuation. A
                # citation may start in the numbered line and finish there.
                next_start = numbered[position + 1][2] if position + 1 < len(numbered) else len(text)
                ranges.append((start, next_start, "task"))
            if last_end < len(text) and numbered[-1][0] not in chosen:
                ranges.append((last_end, len(text), "context"))
        elif reason is None:
            reason = "numbered_structure_missing_in_source_version"
    if mode == "whole":
        ranges = [(0, len(text), "whole")]
    ranges.sort()
    spans: list[ReadingSpan] = []
    for raw_start, raw_end, role in ranges:
        if raw_start >= raw_end:
            continue
        start, end = raw_start, raw_end
        if spans and start <= spans[-1].end and spans[-1].role == role:
            previous = spans.pop()
            start = previous.start
            end = max(end, previous.end)
        spans.append(ReadingSpan(start=start, end=end, role=role, text=text[start:end]))
    material = {
        "version": PROJECTION_VERSION,
        "event_id": event_id,
        "evidence_ref": evidence.ref,
        "source_revision_sequence": evidence.source.revision_sequence,
        "scopes": [scope.model_dump(mode="json") for scope in scopes],
        "mode": mode,
        "reason": reason,
        "segments": [span.as_dict() for span in spans],
    }
    material_sha = digest(material)
    return ReadingView(
        evidence_ref=evidence.ref,
        source_version=evidence.source.artifact_revision,
        mode=mode,
        reason=reason,
        spans=tuple(spans),
        material_sha=material_sha,
        read_ref=identity("news_read", event_id, evidence.ref, material_sha),
    )


def reading_views(source: FrozenInput) -> tuple[ReadingView, ...]:
    return tuple(reading_view(source.event_id, item, source.extraction_scopes) for item in source.evidence)


def extraction_input(source: FrozenInput) -> dict[str, object]:
    """Serialize the only model input shape; never transmit full sibling bodies."""

    document: dict[str, object] = source.model_dump(mode="json")
    document.pop("reanalysis_reason", None)
    document.pop("reanalysis_head_ref", None)
    document["evidence"] = [
        view.as_dict(item) for item, view in zip(source.evidence, reading_views(source), strict=True)
    ]
    document["projection_version"] = PROJECTION_VERSION
    return document


__all__ = ["PROJECTION_VERSION", "ReadingView", "extraction_input", "reading_view", "reading_views"]
