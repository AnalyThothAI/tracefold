from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from .models import (
    BRIEF_PROMPT_VERSION,
    BRIEF_SCHEMA_VERSION,
    BRIEF_WORKFLOW_VERSION,
    NEWS_LOCALE,
    NewsBriefDraft,
    NewsBriefStory,
)

_CITATION_RE = re.compile(r"\[(\d{1,2})\]")
_GROUNDING_TOKEN_RE = re.compile(r"\b(?:[A-Za-z][A-Za-z0-9.'’-]{1,}|\d+(?:[.,]\d+)*)\b")
_GROUNDING_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "as",
        "at",
        "by",
        "for",
        "from",
        "in",
        "is",
        "of",
        "on",
        "or",
        "the",
        "to",
        "with",
    }
)


def brief_fingerprint(stories: Sequence[Mapping[str, Any]]) -> str:
    payload = {
        "contract": {
            "prompt": BRIEF_PROMPT_VERSION,
            "workflow": BRIEF_WORKFLOW_VERSION,
            "schema": BRIEF_SCHEMA_VERSION,
            "locale": NEWS_LOCALE,
        },
        "stories": [
            {
                "story_id": str(story["story_id"]),
                "state_fingerprint": str(story["state_fingerprint"]),
                "rank": index + 1,
            }
            for index, story in enumerate(stories)
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def validate_and_repair_brief(
    draft: NewsBriefDraft,
    stories: Sequence[NewsBriefStory],
) -> tuple[NewsBriefDraft, dict[str, Any], bool]:
    """Enforce index lock; degrade individual lines without shifting sources."""

    repaired_lines: list[str] = []
    line_fallbacks: list[int] = []
    raw_lines = list(draft.lines)
    grounding_failures: list[int] = []
    for index, story in enumerate(stories, start=1):
        candidate = raw_lines[index - 1].strip() if index <= len(raw_lines) else ""
        citations = [int(value) for value in _CITATION_RE.findall(candidate)]
        bare = _CITATION_RE.sub("", candidate).strip()
        grounded = _is_grounded(bare, (story.title,))
        valid = bool(bare) and citations == [index] and len(bare) <= 220 and grounded
        if valid:
            repaired_lines.append(f"{bare} [{index}]")
        else:
            repaired_lines.append(f"第{index}条：{story.title} [{index}]")
            line_fallbacks.append(index)
            if bare and not grounded:
                grounding_failures.append(index)

    lead = draft.lead.strip()
    lead_citations = [int(value) for value in _CITATION_RE.findall(lead)]
    valid_indexes = set(range(1, len(stories) + 1))
    lead_valid = (
        20 <= len(lead) <= 700
        and bool(lead_citations)
        and all(value in valid_indexes for value in lead_citations)
        and _lead_is_grounded(lead, stories)
    )
    if not lead_valid:
        lead = f"今日重点：{stories[0].title} [1]"

    degraded = not lead_valid or bool(line_fallbacks)
    repaired = draft.model_copy(update={"lead": lead, "lines": tuple(repaired_lines)})
    validation = {
        "citation_index_lock": True,
        "citation_closure": True,
        "proper_noun_grounding": not grounding_failures and lead_valid,
        "no_cross_story_stitching": True,
        "story_count": len(stories),
        "lead_fallback": not lead_valid,
        "line_fallbacks": line_fallbacks,
        "grounding_failures": grounding_failures,
        "model_line_coverage": len(stories) - len(line_fallbacks),
        "final_story_coverage": len(stories),
    }
    return repaired, validation, degraded


def _grounding_tokens(value: str) -> set[str]:
    return {
        token.casefold() for token in _GROUNDING_TOKEN_RE.findall(value) if token.casefold() not in _GROUNDING_STOPWORDS
    }


def _is_grounded(value: str, titles: Sequence[str]) -> bool:
    allowed = " ".join(titles).casefold()
    return all(token in allowed for token in _grounding_tokens(value))


def _lead_is_grounded(lead: str, stories: Sequence[NewsBriefStory]) -> bool:
    sentences = [part.strip() for part in re.split(r"[。！？!?；;]", lead) if part.strip()]
    if not sentences:
        return False
    for sentence in sentences:
        citations = [int(value) for value in _CITATION_RE.findall(sentence)]
        if not citations:
            return False
        allowed_titles = tuple(stories[index - 1].title for index in citations if 1 <= index <= len(stories))
        if len(allowed_titles) != len(citations):
            return False
        if not _is_grounded(_CITATION_RE.sub("", sentence), allowed_titles):
            return False
    return True


__all__ = ["brief_fingerprint", "validate_and_repair_brief"]
