from __future__ import annotations

import base64
import json
from typing import Any

from tracefold.news.models import NewsAnalysisContract
from tracefold.news.repository import NewsRepository

_VERIFICATION_STATUSES = frozenset({"corroborated", "trusted", "attributed", "unverified"})


class StoryInterface:
    """The sole external News read interface.

    Callers supply pagination and filters and receive Story-shaped results.
    Article identity, Story membership, source-chain handling, projection, and
    analysis persistence remain implementation details behind this seam.
    """

    def __init__(
        self,
        repository: NewsRepository,
        *,
        analysis_contract: NewsAnalysisContract | None,
    ) -> None:
        self._repository = repository
        self._analysis_contract = analysis_contract

    def list_stories(
        self,
        *,
        limit: int,
        cursor: str | None = None,
        q: str | None = None,
        verification_status: str | None = None,
        source: str | None = None,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 200:
            raise ValueError("news_story_limit_out_of_bounds")
        normalized_verification = str(verification_status or "").strip().lower() or None
        if normalized_verification is not None and normalized_verification not in _VERIFICATION_STATUSES:
            raise ValueError("news_story_verification_status_invalid")
        normalized_q = str(q or "").strip() or None
        normalized_source = str(source or "").strip() or None
        rows = self._repository.list_story_rows(
            limit=limit + 1,
            cursor=_decode_cursor(cursor),
            q=normalized_q,
            verification_status=normalized_verification,
            source=normalized_source,
            analysis_contract=self._analysis_contract,
        )
        has_more = len(rows) > limit
        visible = rows[:limit]
        items = [
            _story_summary(row, analysis_available=self._analysis_contract is not None)
            for row in visible
        ]
        next_cursor = None
        if has_more and visible:
            last = visible[-1]
            next_cursor = _encode_cursor(int(last["last_seen_at_ms"]), str(last["story_id"]))
        return {"items": items, "next_cursor": next_cursor}

    def get_story(self, *, story_id: str) -> dict[str, Any] | None:
        normalized = str(story_id or "").strip()
        if not normalized:
            raise ValueError("news_story_id_required")
        row = self._repository.get_story(
            story_id=normalized,
            analysis_contract=self._analysis_contract,
        )
        if row is None:
            return None
        return _story_detail(row, analysis_available=self._analysis_contract is not None)

    def list_sources(self) -> dict[str, Any]:
        return {"items": self._repository.list_sources()}


def _story_summary(row: dict[str, Any], *, analysis_available: bool) -> dict[str, Any]:
    return {
        "story_id": str(row["story_id"]),
        "title": str(row["title"]),
        "snippet": str(row["snippet"]),
        "language": str(row["language"]),
        "primary_article": dict(row["primary_article"]),
        "first_seen_at_ms": int(row["first_seen_at_ms"]),
        "last_seen_at_ms": int(row["last_seen_at_ms"]),
        "source_count": int(row["source_count"]),
        "article_count": int(row["article_count"]),
        "trusted_source_count": int(row["trusted_source_count"]),
        "independent_origin_count": int(row["independent_origin_count"]),
        "verification_status": str(row["verification_status"]),
        "phase": str(row["phase"]),
        "importance_score": int(row["importance_score"]),
        "importance_factors": dict(row["importance_factors"]),
        "analysis_status": _analysis_status(row, analysis_available=analysis_available),
        "short_conclusion": row.get("short_conclusion"),
        "analysis_published_at_ms": row.get("analysis_published_at_ms"),
    }


def _story_detail(row: dict[str, Any], *, analysis_available: bool) -> dict[str, Any]:
    analysis = None
    if row.get("analysis_id"):
        analysis = {
            "analysis_id": str(row["analysis_id"]),
            "model": str(row["analysis_model"]),
            "prompt_version": str(row["analysis_prompt_version"]),
            "workflow_version": str(row["analysis_workflow_version"]),
            "schema_version": str(row["analysis_schema_version"]),
            "what_happened": str(row["what_happened"]),
            "why_it_matters": str(row["why_it_matters"]),
            "political_impact": str(row["political_impact"]),
            "economic_market_impact": str(row["economic_market_impact"]),
            "confirmed_facts": list(row["confirmed_facts"]),
            "disagreements_unknowns": list(row["disagreements_unknowns"]),
            "next_checkpoint": str(row["next_checkpoint"]),
            "evidence_references": list(row["evidence_references"]),
            "published_at_ms": int(row["analysis_published_at_ms"]),
        }
    return {
        "story_id": str(row["story_id"]),
        "anchor_article_id": str(row["anchor_article_id"]),
        "primary_article_id": str(row["primary_article_id"]),
        "title": str(row["title"]),
        "snippet": str(row["snippet"]),
        "language": str(row["language"]),
        "first_seen_at_ms": int(row["first_seen_at_ms"]),
        "last_seen_at_ms": int(row["last_seen_at_ms"]),
        "source_count": int(row["source_count"]),
        "article_count": int(row["article_count"]),
        "trusted_source_count": int(row["trusted_source_count"]),
        "independent_origin_count": int(row["independent_origin_count"]),
        "verification_status": str(row["verification_status"]),
        "phase": str(row["phase"]),
        "lifecycle_version": str(row["lifecycle_version"]),
        "importance_score": int(row["importance_score"]),
        "importance_version": str(row["importance_version"]),
        "importance_factors": dict(row["importance_factors"]),
        "identity_version": str(row["identity_version"]),
        "evidence_set_hash": str(row["evidence_set_hash"]),
        "analysis_status": _analysis_status(row, analysis_available=analysis_available),
        "analysis_error": row.get("analysis_last_error") if row.get("attempt_status") == "failed" else None,
        "analysis": analysis,
        "articles": list(row["articles"]),
        "memberships": list(row["memberships"]),
    }


def _analysis_status(row: dict[str, Any], *, analysis_available: bool) -> str:
    if row.get("analysis_id"):
        return "available"
    if row.get("attempt_status") == "failed":
        return "failed"
    if row.get("attempt_status") == "running":
        return "pending"
    return "pending" if analysis_available else "unavailable"


def _encode_cursor(last_seen_at_ms: int, story_id: str) -> str:
    payload = json.dumps([last_seen_at_ms, story_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_cursor(value: str | None) -> tuple[int, str] | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    try:
        padded = normalized + ("=" * (-len(normalized) % 4))
        decoded = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("news_story_cursor_invalid") from exc
    if (
        not isinstance(decoded, list)
        or len(decoded) != 2
        or isinstance(decoded[0], bool)
        or not isinstance(decoded[0], int)
        or decoded[0] < 0
        or not isinstance(decoded[1], str)
        or not decoded[1]
    ):
        raise ValueError("news_story_cursor_invalid")
    return decoded[0], decoded[1]


__all__ = ["StoryInterface"]
