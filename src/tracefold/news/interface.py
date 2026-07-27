from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from typing import Any

from tracefold.news.repository import NewsRepository

_EVIDENCE_POSTURES = frozenset(
    {
        "single_origin_reported",
        "independently_corroborated",
        "primary_source_confirmed",
        "contested",
        "corrected",
        "withdrawn",
    }
)


class NewsInterface:
    """The sole package-external News product seam."""

    def __init__(self, repository: NewsRepository) -> None:
        self._repository = repository

    def list_stories(
        self,
        *,
        limit: int,
        cursor: str | None = None,
        view: str = "latest",
        q: str | None = None,
        evidence_posture: str | None = None,
        source: str | None = None,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 200:
            raise ValueError("news_story_limit_out_of_bounds")
        normalized_view = str(view or "").strip().lower()
        if normalized_view not in {"latest", "priority"}:
            raise ValueError("news_story_view_invalid")
        posture = str(evidence_posture or "").strip().lower() or None
        if posture is not None and posture not in _EVIDENCE_POSTURES:
            raise ValueError("news_story_evidence_posture_invalid")
        rows = self._repository.list_story_rows(
            limit=limit + 1,
            cursor=_decode_cursor(cursor, expected_view=normalized_view),
            view=normalized_view,
            q=str(q or "").strip() or None,
            evidence_posture=posture,
            source=str(source or "").strip() or None,
        )
        visible = rows[:limit]
        next_cursor = None
        if len(rows) > limit and visible:
            last = visible[-1]
            next_cursor = _encode_cursor(
                view=normalized_view,
                priority_score=int(last["priority_score"]),
                last_material_at_ms=int(last["last_material_evidence_at_ms"]),
                story_id=str(last["story_id"]),
            )
        return {
            "items": [_story_summary(row) for row in visible],
            "next_cursor": next_cursor,
            "view": normalized_view,
        }

    def get_story(self, *, story_id: str) -> dict[str, Any] | None:
        normalized = str(story_id or "").strip()
        if not normalized:
            raise ValueError("news_story_id_required")
        row = self._repository.get_story(story_id=normalized)
        return _story_detail(row) if row is not None else None

    def request_story_analysis(self, *, story_id: str, now_ms: int) -> dict[str, Any] | None:
        normalized = str(story_id or "").strip()
        if not normalized:
            raise ValueError("news_story_id_required")
        return self._repository.request_story_analysis(story_id=normalized, now_ms=now_ms)

    def get_global_brief(self) -> dict[str, Any]:
        row = self._repository.get_current_brief()
        active = row["active_selection"]
        analysis = row["analysis"]
        active_attempt = row["active_attempt"]
        if analysis is not None:
            analysis_status = "reused" if str(analysis["attachment_kind"]) == "reused" else "available"
        elif active is None or not list(active["selected_story_ids"]):
            analysis_status = "unavailable"
        elif active_attempt is not None and str(active_attempt["status"]) == "failed":
            analysis_status = "failed"
        else:
            analysis_status = "pending"
        return {
            "active_selection": _brief_active_selection(active) if active else None,
            "analysis": _brief_publication(analysis) if analysis else None,
            "analysis_status": analysis_status,
            "previous_publication": (
                _brief_publication(row["previous_publication"]) if row["previous_publication"] else None
            ),
            "pending_proposal": (_brief_pending_proposal(row["pending_proposal"]) if row["pending_proposal"] else None),
            "latest_failure": row["latest_failure"],
        }

    def list_global_brief_history(self, *, limit: int) -> dict[str, Any]:
        if not 1 <= limit <= 100:
            raise ValueError("news_brief_history_limit_out_of_bounds")
        return {"items": [_brief_publication(row) for row in self._repository.list_brief_publications(limit=limit)]}

    def list_sources(self) -> dict[str, Any]:
        return {"items": self._repository.list_sources()}

    def health(self, *, now_ms: int) -> dict[str, Any]:
        return self._repository.health_snapshot(now_ms=now_ms)

    def reset_story_projection(self) -> dict[str, int]:
        return self._repository.reset_story_projection()

    def project_story_batch(self, *, now_ms: int, limit: int) -> dict[str, int]:
        if not 1 <= limit <= 10_000:
            raise ValueError("news_story_projection_limit_out_of_bounds")
        return self._repository.project_pending_revisions(now_ms=now_ms, limit=limit)

    def refresh_story_presentation(self, *, now_ms: int, limit: int) -> int:
        if not 1 <= limit <= 100_000:
            raise ValueError("news_story_presentation_limit_out_of_bounds")
        return self._repository.refresh_story_presentation(now_ms=now_ms, limit=limit)


def _story_summary(row: dict[str, Any]) -> dict[str, Any]:
    analysis_payload = row.get("analysis_payload")
    return {
        "story_id": str(row["story_id"]),
        "title": str(row["title"]),
        "snippet": str(row["snippet"]),
        "languages": list(row["languages"]),
        "first_seen_at_ms": int(row["first_seen_at_ms"]),
        "last_material_evidence_at_ms": int(row["last_material_evidence_at_ms"]),
        "last_presentation_at_ms": int(row["updated_at_ms"]),
        "breaking": bool(row["breaking"]),
        "breaking_reason": _breaking_reason(row),
        "evidence_posture": str(row["evidence_posture"]),
        "evidence_factors": dict(row["evidence_factors"]),
        "lifecycle": str(row["lifecycle"]),
        "material_evolution_state": str(row["material_evolution_state"]),
        "impact_score": int(row["impact_score"]),
        "impact_profile": dict(row["impact_profile"]),
        "priority_score": int(row["priority_score"]),
        "priority_profile": dict(row["priority_profile"]),
        "primary_member_count": int(row["primary_member_count"]),
        "contextual_member_count": int(row["contextual_member_count"]),
        "independent_origin_count": int(row["independent_origin_count"]),
        "source_count": int(row["source_count"]),
        "brief_eligible": bool(row["brief_eligible"]),
        "representative_evidence": {
            "article_id": str(row["representative_article_id"]),
            "revision_id": str(row["representative_revision_id"]),
            "title": str(row["representative_title"]),
            "source_id": str(row["representative_source_id"]),
            "source_name": str(row["representative_source_name"]),
            "source_domain": str(row["representative_source_domain"]),
            "source_published_at_ms": int(row["representative_published_at_ms"]),
        },
        "analysis": {
            "status": "available" if analysis_payload else "unavailable",
            "publication_id": row.get("analysis_publication_id"),
            "published_at_ms": row.get("analysis_published_at_ms"),
            "short_conclusion": (dict(analysis_payload).get("why_it_matters") if analysis_payload else None),
        },
    }


def _story_detail(row: dict[str, Any]) -> dict[str, Any]:
    analysis_request = row.get("analysis_request")
    articles = list(row["articles"])
    return {
        **{
            key: row[key]
            for key in (
                "story_id",
                "seed_article_id",
                "identity_version",
                "identity_status",
                "event_core",
                "title",
                "snippet",
                "languages",
                "representative_revision_id",
                "first_seen_at_ms",
                "last_material_evidence_at_ms",
                "material_evidence_hash",
                "presentation_state_hash",
                "evidence_posture",
                "evidence_factors",
                "lifecycle",
                "lifecycle_version",
                "material_evolution_state",
                "impact_score",
                "impact_profile",
                "priority_score",
                "priority_profile",
                "scoring_version",
                "primary_member_count",
                "contextual_member_count",
                "independent_origin_count",
                "brief_eligible",
                "brief_eligibility_reason",
                "breaking",
            )
        },
        "last_presentation_at_ms": int(row["updated_at_ms"]),
        "breaking_reason": _breaking_reason(row),
        "source_count": len({str(article["source_id"]) for article in articles}),
        "memberships": list(row["memberships"]),
        "articles": articles,
        "identity_decisions": list(row["identity_decisions"]),
        "material_events": list(row["material_events"]),
        "selection_audit": list(row["selection_audit"]),
        "analysis": {
            "status": (
                "available"
                if row["current_analysis_publication"]
                else str(analysis_request["status"])
                if analysis_request and str(analysis_request["status"]) != "published"
                else "unavailable"
            ),
            "request": dict(analysis_request) if analysis_request else None,
            "current": (dict(row["current_analysis_publication"]) if row["current_analysis_publication"] else None),
            "history": list(row["analysis_publications"]),
        },
    }


def _brief_publication(row: dict[str, Any]) -> dict[str, Any]:
    publication = {
        "publication_id": str(row["publication_id"]),
        "selection_id": str(row["selection_id"]),
        "synthesis_input_hash": str(row["synthesis_input_hash"]),
        "evidence_cutoff_at_ms": int(row["evidence_cutoff_at_ms"]),
        "published_at_ms": int(row["published_at_ms"]),
        "contract": {
            "model": str(row["model"]),
            "prompt_version": str(row["prompt_version"]),
            "workflow_version": str(row["workflow_version"]),
            "schema_version": str(row["schema_version"]),
            "locale": str(row["locale"]),
        },
        "payload": dict(row["payload"]),
        "evidence_references": list(row["evidence_references"]),
        "selection_fingerprint": str(row["selection_fingerprint"]),
        "selected_story_ids": list(row["selected_story_ids"]),
        "selection_decisions": list(row["decisions"]),
        "narrative_groups": list(row["narrative_groups"]),
        "evidence_bundle": dict(row["evidence_bundle"]),
        "receipt": dict(row["receipt"]),
        "activation_id": (str(row["activation_id"]) if row.get("activation_id") is not None else None),
        "activation_sequence": (
            int(row["activation_sequence"]) if row.get("activation_sequence") is not None else None
        ),
        "activated_at_ms": (int(row["activated_at_ms"]) if row.get("activated_at_ms") is not None else None),
        "attachment_kind": (str(row["attachment_kind"]) if row.get("attachment_kind") is not None else None),
        "attached_at_ms": (int(row["attached_at_ms"]) if row.get("attached_at_ms") is not None else None),
    }
    return publication


def _brief_active_selection(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "activation_id": str(row["activation_id"]),
        "activation_sequence": int(row["activation_sequence"]),
        "activated_at_ms": int(row["activated_at_ms"]),
        "activation_lane": str(row["lane"]),
        "selection_id": str(row["selection_id"]),
        "selection_fingerprint": str(row["selection_fingerprint"]),
        "selection_policy_version": str(row["policy_version"]),
        "synthesis_input_hash": str(row["synthesis_input_hash"]),
        "evidence_cutoff_at_ms": int(row["evidence_cutoff_at_ms"]),
        "selected_story_ids": list(row["selected_story_ids"]),
        "selection_decisions": list(row["decisions"]),
        "narrative_groups": list(row["narrative_groups"]),
        "evidence_bundle": dict(row["evidence_bundle"]),
    }


def _brief_pending_proposal(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "proposal_id": str(row["proposal_id"]),
        "selection_id": str(row["selection_id"]),
        "selection_fingerprint": str(row["selection_fingerprint"]),
        "selected_story_ids": list(row["selected_story_ids"]),
        "lane": str(row["lane"]),
        "first_proposed_at_ms": int(row["first_proposed_at_ms"]),
        "last_observed_at_ms": int(row["last_observed_at_ms"]),
        "activation_due_at_ms": int(row["activation_due_at_ms"]),
    }


def _breaking_reason(row: Mapping[str, Any]) -> str:
    if bool(row.get("breaking")):
        return "fresh_material_high_impact"
    if str(row.get("evidence_posture")) == "withdrawn":
        return "withdrawn"
    if int(row.get("impact_score") or 0) < 70:
        return "impact_below_breaking_floor"
    if int(row.get("primary_member_count") or 0) < 1:
        return "no_primary_evidence"
    return "outside_30_minute_material_window"


def _encode_cursor(
    *,
    view: str,
    priority_score: int,
    last_material_at_ms: int,
    story_id: str,
) -> str:
    values: list[object] = (
        [view, last_material_at_ms, story_id]
        if view == "latest"
        else [view, priority_score, last_material_at_ms, story_id]
    )
    payload = json.dumps(
        values,
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_cursor(
    value: str | None,
    *,
    expected_view: str,
) -> tuple[int, int, str] | tuple[int, str] | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    try:
        padded = normalized + ("=" * (-len(normalized) % 4))
        decoded = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("news_story_cursor_invalid") from exc
    if not isinstance(decoded, list) or not decoded or decoded[0] not in {"latest", "priority"}:
        raise ValueError("news_story_cursor_invalid")
    if decoded[0] != expected_view:
        raise ValueError("news_story_cursor_view_mismatch")
    if expected_view == "latest":
        if (
            len(decoded) != 3
            or isinstance(decoded[1], bool)
            or not isinstance(decoded[1], int)
            or not isinstance(decoded[2], str)
            or not decoded[2]
        ):
            raise ValueError("news_story_cursor_invalid")
        return decoded[1], decoded[2]
    if (
        len(decoded) != 4
        or isinstance(decoded[1], bool)
        or not isinstance(decoded[1], int)
        or isinstance(decoded[2], bool)
        or not isinstance(decoded[2], int)
        or not isinstance(decoded[3], str)
        or not decoded[3]
    ):
        raise ValueError("news_story_cursor_invalid")
    return decoded[1], decoded[2], decoded[3]


__all__ = ["NewsInterface"]
