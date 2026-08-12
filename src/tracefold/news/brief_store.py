from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from psycopg.types.json import Jsonb

from .brief import publication_id as content_publication_id
from .models import (
    BRIEF_COMPOSER_VERSION,
    BRIEF_PROMPT_VERSION,
    BRIEF_SCHEMA_VERSION,
    BRIEF_WORKFLOW_VERSION,
    INSIGHTS_SYNTHESIS_GATE,
    INSIGHTS_SYNTHESIS_MISSING_CLUSTER,
    INSIGHTS_SYNTHESIS_PARSE,
    INSIGHTS_SYNTHESIS_PROVIDER,
    NEWS_LOCALE,
    NewsBriefSynthesisResult,
)
from .query_specs import brief_query

BRIEF_SLOT_MS = 30 * 60 * 1_000
BRIEF_LEASE_MS = 120 * 1_000
BRIEF_MAX_FAILURES = 100
_BRIEF_LOCK_KEY = 727_301_985


def peek_brief_candidate(repository: Any, *, now_ms: int) -> dict[str, Any] | None:
    """Return only the current UTC half-hour slot when it can be claimed."""

    conn = repository.conn
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (_BRIEF_LOCK_KEY,))
    current = _current(conn, lock="UPDATE")
    live_selection = _selection(conn)
    slot_at_ms = _slot_at_ms(now_ms)
    active_slot_at_ms = current["slot_at_ms"]
    candidate_selection: dict[str, Any] | None
    if active_slot_at_ms is None or int(active_slot_at_ms) < slot_at_ms:
        if live_selection is None:
            return None
        _open_slot(conn, slot_at_ms=slot_at_ms, now_ms=now_ms)
        candidate_selection = live_selection
        active_slot_at_ms = slot_at_ms
        status = "due"
    else:
        candidate_selection = _json_object(current["active_selection"]) or live_selection
    if int(active_slot_at_ms) != slot_at_ms:
        return None

    if candidate_selection is None or not list(candidate_selection["top_stories"]):
        return None

    if current["slot_at_ms"] is not None and int(current["slot_at_ms"]) == slot_at_ms:
        status = str(current["slot_status"])
    if status == "due" and slot_at_ms <= now_ms:
        return {
            "slot_at_ms": slot_at_ms,
            "next_due_at_ms": slot_at_ms,
        }
    if (
        status == "running"
        and current["lease_expires_at_ms"] is not None
        and int(current["lease_expires_at_ms"]) <= now_ms
    ):
        return {
            "slot_at_ms": slot_at_ms,
            "next_due_at_ms": int(current["lease_expires_at_ms"]),
        }
    return None


def prepare_brief_run(
    repository: Any,
    *,
    slot_at_ms: int,
    lease_owner: str,
    lease_token: str,
    now_ms: int,
) -> dict[str, Any] | None:
    """Claim one slot and freeze its selection exactly once."""

    owner = str(lease_owner).strip()
    token = str(lease_token).strip()
    if not owner or not token:
        raise ValueError("news_brief_lease_identity_required")
    requested_slot_at_ms = int(slot_at_ms)
    if requested_slot_at_ms != _slot_at_ms(now_ms):
        return None

    conn = repository.conn
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (_BRIEF_LOCK_KEY,))
    current = _current(conn, lock="UPDATE")
    if current["slot_at_ms"] is None or int(current["slot_at_ms"]) != requested_slot_at_ms:
        return None

    status = str(current["slot_status"])
    if status == "completed":
        return None
    if status == "due":
        if int(current["slot_at_ms"]) > now_ms:
            return None
        reclaimed_attempt_count = int(current["attempt_count"] or 0)
        reclaimed_failure_count = int(current["failure_count"] or 0)
    elif status == "running":
        lease_expires_at_ms = current["lease_expires_at_ms"]
        if lease_expires_at_ms is None or int(lease_expires_at_ms) > now_ms:
            return None
        reclaimed_attempt_count = int(current["attempt_count"] or 0)
        reclaimed_failure_count = min(
            BRIEF_MAX_FAILURES,
            int(current["failure_count"] or 0) + (1 if current["last_attempt_at_ms"] is not None else 0),
        )
    else:
        raise RuntimeError("news_brief_slot_status_invalid")

    active_selection = _json_object(current["active_selection"])
    if active_selection is None:
        active_selection = _selection(conn)
    if active_selection is None or not list(active_selection["top_stories"]):
        return None

    conn.execute(
        """
        UPDATE news_brief_current
           SET slot_status = 'running',
               lease_owner = %s,
               lease_token = %s,
               lease_expires_at_ms = %s,
               attempt_count = %s,
               failure_count = %s,
               model_outcome = NULL,
               pointer_action = 'none',
               last_error_code = NULL,
               last_attempt_at_ms = NULL,
               completed_at_ms = NULL,
               active_selection = %s,
               updated_at_ms = %s
         WHERE singleton_key = true AND slot_at_ms = %s
        """,
        (
            owner,
            token,
            now_ms + BRIEF_LEASE_MS,
            reclaimed_attempt_count,
            reclaimed_failure_count,
            Jsonb(active_selection),
            now_ms,
            requested_slot_at_ms,
        ),
    )

    claim = {
        "slot_at_ms": requested_slot_at_ms,
        "selection_fingerprint": (
            str(active_selection["selection_fingerprint"])
            if active_selection["selection_fingerprint"] is not None
            else None
        ),
        "lease_owner": owner,
        "lease_token": token,
    }
    stories = [dict(story) for story in active_selection["top_stories"]]
    if not any(_lead_eligible(story) for story in stories):
        result = NewsBriefSynthesisResult(
            brief_kind="none",
            quality="degraded",
            world_brief="",
            brief_story_lines=(),
            sources=(),
            provider="",
            model="",
            validation={"failure_code": INSIGHTS_SYNTHESIS_MISSING_CLUSTER},
        )
        completed_publication_id = _finish_result(
            conn,
            claim=claim,
            result=result,
            now_ms=now_ms,
        )
        return {"completed_without_model": True} if completed_publication_id is not None else None

    return {
        "completed_without_model": False,
        "claim": claim,
        "selection": active_selection,
        "top_stories": stories,
    }


def start_brief_model(
    repository: Any,
    *,
    slot_at_ms: int,
    lease_owner: str,
    lease_token: str,
    now_ms: int,
) -> bool:
    conn = repository.conn
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (_BRIEF_LOCK_KEY,))
    cursor = conn.execute(
        """
        UPDATE news_brief_current
           SET attempt_count = attempt_count + 1,
               last_attempt_at_ms = %s,
               updated_at_ms = %s
         WHERE singleton_key = true
           AND slot_at_ms = %s
           AND slot_status = 'running'
           AND lease_owner = %s
           AND lease_token = %s
           AND lease_expires_at_ms > %s
           AND last_attempt_at_ms IS NULL
        """,
        (now_ms, now_ms, int(slot_at_ms), lease_owner, lease_token, now_ms),
    )
    return int(cursor.rowcount or 0) == 1


def release_brief_claim(
    repository: Any,
    *,
    slot_at_ms: int,
    lease_owner: str,
    lease_token: str,
    now_ms: int,
) -> bool:
    """Release work that never reached the model without changing the frozen input."""

    conn = repository.conn
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (_BRIEF_LOCK_KEY,))
    cursor = conn.execute(
        """
        UPDATE news_brief_current
           SET slot_status = 'due',
               lease_owner = NULL,
               lease_token = NULL,
               lease_expires_at_ms = NULL,
               updated_at_ms = %s
         WHERE singleton_key = true
           AND slot_at_ms = %s
           AND slot_status = 'running'
           AND lease_owner = %s
           AND lease_token = %s
           AND lease_expires_at_ms > %s
        """,
        (now_ms, int(slot_at_ms), lease_owner, lease_token, now_ms),
    )
    return int(cursor.rowcount or 0) == 1


def publish_brief(
    repository: Any,
    *,
    claim: Mapping[str, Any],
    result: NewsBriefSynthesisResult,
    now_ms: int,
) -> str | None:
    """Complete a slot against its frozen selection, never the live selection."""

    conn = repository.conn
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (_BRIEF_LOCK_KEY,))
    return _finish_result(
        conn,
        claim=claim,
        result=result,
        now_ms=now_ms,
    )


def get_brief(repository: Any, *, now_ms: int) -> dict[str, Any]:
    del now_ms  # Serving reads never advance slots or reinterpret leases.
    current = _current(repository.conn)
    publication = _json_object(current["served_payload"])
    if publication is None:
        state = "unavailable"
    elif str(publication["quality"]) != "ok":
        state = "degraded"
    elif (
        current["slot_at_ms"] is not None
        and publication.get("slot_at_ms") == int(current["slot_at_ms"])
        and str(current["slot_status"]) == "completed"
        and str(current["model_outcome"]) == "ok"
    ):
        state = "current"
    else:
        state = "last_known_good"
    return {
        "state": state,
        "slot_at_ms": int(current["slot_at_ms"]) if current["slot_at_ms"] is not None else None,
        "next_due_at_ms": int(current["next_due_at_ms"]),
        "publication": publication,
        "latest_run": _run_payload(current) if current["slot_at_ms"] is not None else None,
    }


def _current(conn: Any, *, lock: str | None = None) -> Mapping[str, Any]:
    if lock is None:
        query = brief_query()
        row = conn.execute(query.sql).fetchone()
    elif lock == "UPDATE":
        row = conn.execute("SELECT * FROM news_brief_current WHERE singleton_key = true FOR UPDATE").fetchone()
    else:
        raise ValueError("news_brief_lock_invalid")
    if row is None:
        raise RuntimeError("news_brief_current_missing")
    return cast(Mapping[str, Any], row)


def _selection(conn: Any) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM news_brief_selection_current WHERE singleton_key = true FOR SHARE").fetchone()
    return dict(row) if row is not None else None


def _open_slot(conn: Any, *, slot_at_ms: int, now_ms: int) -> None:
    conn.execute(
        """
        UPDATE news_brief_current
           SET slot_at_ms = %s,
               slot_status = 'due',
               next_due_at_ms = %s,
               completed_at_ms = NULL,
               lease_owner = NULL,
               lease_token = NULL,
               lease_expires_at_ms = NULL,
               attempt_count = 0,
               failure_count = 0,
               model_outcome = NULL,
               pointer_action = 'none',
               last_error_code = NULL,
               last_attempt_at_ms = NULL,
               active_selection = NULL,
               updated_at_ms = %s
         WHERE singleton_key = true
        """,
        (slot_at_ms, slot_at_ms + BRIEF_SLOT_MS, now_ms),
    )


def _finish_result(
    conn: Any,
    *,
    claim: Mapping[str, Any],
    result: NewsBriefSynthesisResult,
    now_ms: int,
) -> str | None:
    current = _current(conn, lock="UPDATE")
    if not _claim_matches(current, claim=claim, now_ms=now_ms):
        return None
    active_selection = _json_object(current["active_selection"])
    if active_selection is None:
        return None
    frozen_fingerprint = str(active_selection["selection_fingerprint"])
    if claim.get("selection_fingerprint") != frozen_fingerprint:
        return None

    payload = _sealed_payload(
        selection=active_selection,
        slot_at_ms=int(claim["slot_at_ms"]),
        result=result,
        now_ms=now_ms,
    )
    healthy = _healthy_payload(current["served_payload"])
    if result.quality == "degraded" and healthy is not None:
        action = "preserve_lkg"
        served_payload = healthy
    else:
        action = "advance_ok" if result.quality == "ok" else "advance_degraded"
        served_payload = payload
    successful = result.brief_kind == "l1"
    attempt_count = int(current["attempt_count"] or 0)
    if current["last_attempt_at_ms"] is None:
        attempt_count += 1
    cursor = conn.execute(
        """
        UPDATE news_brief_current
           SET slot_status = 'completed',
               next_due_at_ms = %s,
               completed_at_ms = %s,
               lease_owner = NULL,
               lease_token = NULL,
               lease_expires_at_ms = NULL,
               attempt_count = %s,
               failure_count = %s,
               model_outcome = %s,
               pointer_action = %s,
               last_error_code = %s,
               served_payload = %s,
               updated_at_ms = %s
         WHERE singleton_key = true
           AND slot_at_ms = %s
           AND slot_status = 'running'
           AND lease_owner = %s
           AND lease_token = %s
        """,
        (
            int(claim["slot_at_ms"]) + BRIEF_SLOT_MS,
            now_ms,
            attempt_count,
            (
                int(current["failure_count"] or 0)
                if successful
                else min(BRIEF_MAX_FAILURES, int(current["failure_count"] or 0) + 1)
            ),
            "ok" if successful else result.brief_kind,
            action,
            None if successful else _safe_error_code(result.validation.get("failure_code")),
            Jsonb(served_payload),
            now_ms,
            int(claim["slot_at_ms"]),
            str(claim["lease_owner"]),
            str(claim["lease_token"]),
        ),
    )
    if int(cursor.rowcount or 0) != 1:
        return None
    return str(served_payload["publication_id"])


def _claim_matches(current: Mapping[str, Any], *, claim: Mapping[str, Any], now_ms: int) -> bool:
    return bool(
        current["slot_at_ms"] is not None
        and int(current["slot_at_ms"]) == int(claim["slot_at_ms"])
        and str(current["slot_status"]) == "running"
        and str(current["lease_owner"]) == str(claim["lease_owner"])
        and str(current["lease_token"]) == str(claim["lease_token"])
        and current["lease_expires_at_ms"] is not None
        and int(current["lease_expires_at_ms"]) > now_ms
    )


def _lead_eligible(story: Mapping[str, Any]) -> bool:
    return int(story.get("unique_source_count") or 0) >= 2 or story.get("entity_corroboration") is True


def _slot_at_ms(now_ms: int) -> int:
    normalized = int(now_ms)
    if normalized < 0:
        raise ValueError("news_brief_clock_invalid")
    return normalized - (normalized % BRIEF_SLOT_MS)


def _sealed_payload(
    *,
    selection: Mapping[str, Any],
    slot_at_ms: int,
    result: NewsBriefSynthesisResult,
    now_ms: int,
) -> dict[str, Any]:
    top_stories = [dict(story) for story in selection["top_stories"]]
    if result.brief_kind == "l1" and (
        len(result.brief_story_lines) != len(top_stories) or len(result.sources) != len(top_stories)
    ):
        raise ValueError("news_brief_l1_selection_lock_invalid")
    if result.brief_kind == "l1" and tuple(line.n for line in result.brief_story_lines) != tuple(
        range(1, len(top_stories) + 1)
    ):
        raise ValueError("news_brief_l1_selection_lock_invalid")
    primary_times = [int(story["primary_published_at_ms"]) for story in top_stories]
    selection_fingerprint_value = str(selection["selection_fingerprint"])
    payload: dict[str, Any] = {
        "slot_at_ms": int(slot_at_ms),
        "selection_fingerprint": selection_fingerprint_value,
        "quality": result.quality,
        "brief_kind": result.brief_kind,
        "world_brief": result.world_brief,
        "brief_story_lines": [line.model_dump(mode="json") for line in result.brief_story_lines],
        "top_stories": top_stories,
        "sources": [source.model_dump(mode="json") for source in result.sources],
        "source_age_range": {"newest_ms": max(primary_times), "oldest_ms": min(primary_times)},
        "provider": result.provider,
        "model": result.model,
        "prompt_version": BRIEF_PROMPT_VERSION,
        "workflow_version": BRIEF_WORKFLOW_VERSION,
        "composer_version": BRIEF_COMPOSER_VERSION,
        "schema_version": BRIEF_SCHEMA_VERSION,
        "selector_version": str(selection["selector_version"]),
        "identity_version": str(selection["identity_version"]),
        "locale": NEWS_LOCALE,
        "validation": dict(result.validation),
        "provenance": {
            "projection_revision": str(selection["projection_revision"]),
            "selector_evaluated_at_ms": int(selection["selector_evaluated_at_ms"]),
            "selection_stats": dict(selection["selection_stats"]),
        },
        "published_at_ms": now_ms,
    }
    payload["publication_id"] = content_publication_id(payload)
    return payload


def _healthy_payload(value: object) -> dict[str, Any] | None:
    payload = _json_object(value)
    return payload if payload is not None and payload.get("quality") == "ok" else None


def _json_object(value: object) -> dict[str, Any] | None:
    return dict(cast(Mapping[str, Any], value)) if isinstance(value, Mapping) else None


def _safe_error_code(value: object) -> str:
    normalized = str(value or "").strip()
    allowed = {
        INSIGHTS_SYNTHESIS_GATE,
        INSIGHTS_SYNTHESIS_MISSING_CLUSTER,
        INSIGHTS_SYNTHESIS_PARSE,
        INSIGHTS_SYNTHESIS_PROVIDER,
    }
    return normalized if normalized in allowed else INSIGHTS_SYNTHESIS_PROVIDER


def _run_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    payload = {
        key: row[key]
        for key in (
            "slot_at_ms",
            "model_outcome",
            "pointer_action",
            "attempt_count",
            "failure_count",
            "next_due_at_ms",
            "lease_expires_at_ms",
            "last_error_code",
            "last_attempt_at_ms",
            "completed_at_ms",
            "updated_at_ms",
        )
    }
    payload["status"] = row["slot_status"]
    return payload


__all__ = [
    "BRIEF_LEASE_MS",
    "BRIEF_MAX_FAILURES",
    "BRIEF_SLOT_MS",
    "get_brief",
    "peek_brief_candidate",
    "prepare_brief_run",
    "publish_brief",
    "release_brief_claim",
    "start_brief_model",
]
