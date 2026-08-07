from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from psycopg.types.json import Jsonb

from .brief import publication_id as content_publication_id
from .brief import target_fingerprint
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

BRIEF_DEBOUNCE_MS = 10 * 60 * 1_000
BRIEF_RETRY_MS = 30 * 60 * 1_000
BRIEF_LEASE_MS = 120 * 1_000
BRIEF_MAX_FAILURES = 100
_BRIEF_LOCK_KEY = 727_301_985


def peek_brief_candidate(repository: Any, *, now_ms: int) -> dict[str, Any] | None:
    """Catch the mutable target up to the one frozen Story-owned selection."""

    conn = repository.conn
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (_BRIEF_LOCK_KEY,))
    selection = _selection(conn)
    if selection is None:
        return None
    target = target_fingerprint(str(selection["selection_fingerprint"]))
    current = conn.execute("SELECT * FROM news_brief_current WHERE singleton_key = true FOR UPDATE").fetchone()
    if current is None:
        raise RuntimeError("news_brief_current_missing")
    if not list(selection["top_stories"]):
        _activate_empty_target(
            conn,
            current=current,
            target_fingerprint_value=target,
            now_ms=now_ms,
        )
        return None
    if current["target_fingerprint"] != target:
        if current["latest_run_id"] is not None:
            conn.execute(
                """
                UPDATE news_brief_runs
                   SET status = 'retry_wait', model_outcome = 'none',
                       pointer_action = 'none', next_due_at_ms = %s,
                       lease_owner = NULL, lease_token = NULL,
                       lease_expires_at_ms = NULL,
                       completed_at_ms = %s, updated_at_ms = %s
                 WHERE run_id = %s AND status = 'running'
                """,
                (
                    now_ms + BRIEF_RETRY_MS,
                    now_ms,
                    now_ms,
                    str(current["latest_run_id"]),
                ),
            )
        conn.execute(
            """
            UPDATE news_brief_current
               SET target_fingerprint = %s,
                   latest_run_id = NULL,
                   pending_first_dirty_at_ms = %s,
                   pending_due_at_ms = %s,
                   updated_at_ms = %s
             WHERE singleton_key = true
            """,
            (target, now_ms, now_ms + BRIEF_DEBOUNCE_MS, now_ms),
        )
        return None

    run = conn.execute(
        "SELECT * FROM news_brief_runs WHERE target_fingerprint = %s",
        (target,),
    ).fetchone()
    if run is None:
        pending_due_at_ms = current["pending_due_at_ms"]
        if pending_due_at_ms is None:
            conn.execute(
                """
                UPDATE news_brief_current
                   SET pending_first_dirty_at_ms = %s,
                       pending_due_at_ms = %s,
                       updated_at_ms = %s
                 WHERE singleton_key = true
                """,
                (now_ms, now_ms + BRIEF_DEBOUNCE_MS, now_ms),
            )
            return None
        if int(pending_due_at_ms) > now_ms:
            return None
        return {"target_fingerprint": target, "next_due_at_ms": int(pending_due_at_ms)}

    status = str(run["status"])
    if status == "retry_wait" and int(run["next_due_at_ms"]) <= now_ms:
        return {"target_fingerprint": target, "next_due_at_ms": int(run["next_due_at_ms"])}
    if status == "running" and int(run["lease_expires_at_ms"]) <= now_ms:
        return {"target_fingerprint": target, "next_due_at_ms": int(run["lease_expires_at_ms"])}
    return None


def prepare_brief_run(
    repository: Any,
    *,
    target_fingerprint_value: str,
    lease_owner: str,
    lease_token: str,
    now_ms: int,
) -> dict[str, Any] | None:
    """Claim one frozen target or finish the public no-eligible-lead outcome."""

    owner = str(lease_owner).strip()
    token = str(lease_token).strip()
    if not owner or not token:
        raise ValueError("news_brief_lease_identity_required")
    conn = repository.conn
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (_BRIEF_LOCK_KEY,))
    current = conn.execute("SELECT * FROM news_brief_current WHERE singleton_key = true FOR UPDATE").fetchone()
    if current is None or current["target_fingerprint"] != target_fingerprint_value:
        return None
    selection = _selection(conn)
    if selection is None or target_fingerprint(str(selection["selection_fingerprint"])) != target_fingerprint_value:
        return None
    stories = list(selection["top_stories"])
    if not stories:
        return None
    run = conn.execute(
        "SELECT * FROM news_brief_runs WHERE target_fingerprint = %s FOR UPDATE",
        (target_fingerprint_value,),
    ).fetchone()
    if not _run_is_due(run, current=current, now_ms=now_ms):
        return None

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
        _finish_without_model(
            conn,
            selection=selection,
            target_fingerprint_value=target_fingerprint_value,
            result=result,
            existing_run=run,
            now_ms=now_ms,
        )
        return {"completed_without_model": True}

    run_id = _run_id(target_fingerprint_value)
    failure_count = int(run["failure_count"] or 0) if run is not None else 0
    if run is not None and str(run["status"]) == "running":
        failure_count = min(BRIEF_MAX_FAILURES, failure_count + 1)
    if run is None:
        conn.execute(
            """
            INSERT INTO news_brief_runs (
              run_id, target_fingerprint, selection_fingerprint,
              status, model_outcome, pointer_action, failure_count,
              next_due_at_ms, lease_owner, lease_token, lease_expires_at_ms,
              last_error_code, created_at_ms, updated_at_ms,
              last_attempt_at_ms, completed_at_ms
            )
            VALUES (
              %s, %s, %s, 'running', NULL, 'none', %s,
              NULL, %s, %s, %s, NULL, %s, %s, NULL, NULL
            )
            """,
            (
                run_id,
                target_fingerprint_value,
                str(selection["selection_fingerprint"]),
                failure_count,
                owner,
                token,
                now_ms + BRIEF_LEASE_MS,
                now_ms,
                now_ms,
            ),
        )
    else:
        run_id = str(run["run_id"])
        conn.execute(
            """
            UPDATE news_brief_runs
               SET selection_fingerprint = %s,
                   status = 'running', model_outcome = NULL,
                   pointer_action = 'none', failure_count = %s,
                   next_due_at_ms = NULL,
                   lease_owner = %s, lease_token = %s,
                   lease_expires_at_ms = %s,
                   last_error_code = NULL,
                   updated_at_ms = %s, completed_at_ms = NULL
             WHERE run_id = %s
            """,
            (
                str(selection["selection_fingerprint"]),
                failure_count,
                owner,
                token,
                now_ms + BRIEF_LEASE_MS,
                now_ms,
                run_id,
            ),
        )
    conn.execute(
        """
        UPDATE news_brief_current
           SET latest_run_id = %s,
               pending_first_dirty_at_ms = NULL,
               pending_due_at_ms = NULL,
               updated_at_ms = %s
         WHERE singleton_key = true AND target_fingerprint = %s
           AND (
             latest_run_id IS DISTINCT FROM %s
             OR pending_first_dirty_at_ms IS NOT NULL
             OR pending_due_at_ms IS NOT NULL
           )
        """,
        (run_id, now_ms, target_fingerprint_value, run_id),
    )
    return {
        "completed_without_model": False,
        "claim": {
            "run_id": run_id,
            "target_fingerprint": target_fingerprint_value,
            "selection_fingerprint": str(selection["selection_fingerprint"]),
            "lease_owner": owner,
            "lease_token": token,
            "release_due_at_ms": now_ms,
        },
        "selection": selection,
        "top_stories": stories,
    }


def start_brief_model(
    repository: Any,
    *,
    run_id: str,
    lease_owner: str,
    lease_token: str,
    now_ms: int,
) -> bool:
    cursor = repository.conn.execute(
        """
        UPDATE news_brief_runs
           SET last_attempt_at_ms = %s, updated_at_ms = %s
         WHERE run_id = %s AND status = 'running'
           AND lease_owner = %s AND lease_token = %s
           AND lease_expires_at_ms > %s
        """,
        (now_ms, now_ms, run_id, lease_owner, lease_token, now_ms),
    )
    return int(cursor.rowcount or 0) == 1


def release_brief_claim(
    repository: Any,
    *,
    run_id: str,
    lease_owner: str,
    lease_token: str,
    due_at_ms: int,
    now_ms: int,
) -> bool:
    cursor = repository.conn.execute(
        """
        UPDATE news_brief_runs
           SET status = 'retry_wait', model_outcome = 'none',
               pointer_action = 'none', next_due_at_ms = %s,
               lease_owner = NULL, lease_token = NULL,
               lease_expires_at_ms = NULL, updated_at_ms = %s
         WHERE run_id = %s AND status = 'running'
           AND lease_owner = %s AND lease_token = %s
        """,
        (due_at_ms, now_ms, run_id, lease_owner, lease_token),
    )
    return int(cursor.rowcount or 0) == 1


def publish_brief(
    repository: Any,
    *,
    claim: Mapping[str, Any],
    selection: Mapping[str, Any],
    result: NewsBriefSynthesisResult,
    now_ms: int,
) -> str | None:
    conn = repository.conn
    active_selection = conn.execute(
        """
        SELECT selection_fingerprint
          FROM news_brief_selection_current
         WHERE singleton_key = true
         FOR SHARE
        """
    ).fetchone()
    if (
        active_selection is None
        or str(active_selection["selection_fingerprint"]) != str(claim["selection_fingerprint"])
        or target_fingerprint(str(active_selection["selection_fingerprint"])) != str(claim["target_fingerprint"])
    ):
        return None
    run = conn.execute(
        """
        SELECT * FROM news_brief_runs
         WHERE run_id = %s AND target_fingerprint = %s
           AND selection_fingerprint = %s AND status = 'running'
           AND lease_owner = %s AND lease_token = %s
           AND lease_expires_at_ms > %s
         FOR UPDATE
        """,
        (
            str(claim["run_id"]),
            str(claim["target_fingerprint"]),
            str(claim["selection_fingerprint"]),
            str(claim["lease_owner"]),
            str(claim["lease_token"]),
            now_ms,
        ),
    ).fetchone()
    current = conn.execute("SELECT * FROM news_brief_current WHERE singleton_key = true FOR UPDATE").fetchone()
    if run is None or current is None or current["target_fingerprint"] != claim["target_fingerprint"]:
        return None
    if str(selection["selection_fingerprint"]) != str(claim["selection_fingerprint"]):
        return None
    return _finish_model_result(
        conn,
        selection=selection,
        target_fingerprint_value=str(claim["target_fingerprint"]),
        result=result,
        run=run,
        now_ms=now_ms,
    )


def fail_brief_run(
    repository: Any,
    *,
    claim: Mapping[str, Any],
    error_code: str = INSIGHTS_SYNTHESIS_PROVIDER,
    now_ms: int,
) -> str | None:
    conn = repository.conn
    run = conn.execute(
        """
        SELECT * FROM news_brief_runs
         WHERE run_id = %s AND status = 'running'
           AND lease_owner = %s AND lease_token = %s
         FOR UPDATE
        """,
        (str(claim["run_id"]), str(claim["lease_owner"]), str(claim["lease_token"])),
    ).fetchone()
    if run is None:
        return None
    healthy = _healthy_current_publication(conn)
    action = "preserve_lkg" if healthy is not None else "none"
    conn.execute(
        """
        UPDATE news_brief_runs
           SET status = 'retry_wait', model_outcome = 'none',
               pointer_action = %s,
               failure_count = LEAST(100, failure_count + 1),
               next_due_at_ms = %s,
               lease_owner = NULL, lease_token = NULL,
               lease_expires_at_ms = NULL,
               last_error_code = %s,
               completed_at_ms = %s, updated_at_ms = %s
         WHERE run_id = %s
        """,
        (action, now_ms + BRIEF_RETRY_MS, _safe_error_code(error_code), now_ms, now_ms, str(run["run_id"])),
    )
    return action


def get_brief(repository: Any, *, now_ms: int) -> dict[str, Any]:
    del now_ms  # State is durable; reads never reinterpret leases into a new product state.
    conn = repository.conn
    current = conn.execute("SELECT * FROM news_brief_current WHERE singleton_key = true").fetchone()
    if current is None:
        raise RuntimeError("news_brief_current_missing")
    publication = None
    if current["publication_id"] is not None:
        row = conn.execute(
            "SELECT * FROM news_brief_publications WHERE publication_id = %s",
            (str(current["publication_id"]),),
        ).fetchone()
        publication = _publication_payload(row) if row is not None else None
    run = None
    if current["latest_run_id"] is not None:
        row = conn.execute(
            "SELECT * FROM news_brief_runs WHERE run_id = %s",
            (str(current["latest_run_id"]),),
        ).fetchone()
        run = _run_payload(row) if row is not None else None
    if publication is None:
        state = "unavailable"
    elif publication["quality"] == "ok" and publication["target_fingerprint"] != current["target_fingerprint"]:
        state = "last_known_good"
    elif publication["quality"] == "ok":
        state = "current"
    else:
        state = "degraded"
    return {
        "state": state,
        "target_fingerprint": (
            str(current["target_fingerprint"]) if current["target_fingerprint"] is not None else None
        ),
        "pending_due_at_ms": current["pending_due_at_ms"],
        "publication": publication,
        "latest_run": run,
    }


def _selection(conn: Any) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM news_brief_selection_current WHERE singleton_key = true").fetchone()
    return dict(row) if row is not None else None


def _run_is_due(run: Mapping[str, Any] | None, *, current: Mapping[str, Any], now_ms: int) -> bool:
    if run is None:
        due = current["pending_due_at_ms"]
        return due is not None and int(due) <= now_ms
    status = str(run["status"])
    if status in {"published", "waiting_input"}:
        return False
    if status == "retry_wait":
        return int(run["next_due_at_ms"]) <= now_ms
    return status == "running" and int(run["lease_expires_at_ms"]) <= now_ms


def _lead_eligible(story: Mapping[str, Any]) -> bool:
    return int(story.get("unique_source_count") or 0) >= 2 or story.get("entity_corroboration") is True


def _run_id(target: str) -> str:
    return f"brief_run_{target[:32]}"


def _activate_empty_target(
    conn: Any,
    *,
    current: Mapping[str, Any],
    target_fingerprint_value: str,
    now_ms: int,
) -> None:
    if current["target_fingerprint"] != target_fingerprint_value and current["latest_run_id"] is not None:
        conn.execute(
            """
            UPDATE news_brief_runs
               SET status = 'retry_wait', model_outcome = 'none',
                   pointer_action = 'none', next_due_at_ms = %s,
                   lease_owner = NULL, lease_token = NULL,
                   lease_expires_at_ms = NULL,
                   completed_at_ms = %s, updated_at_ms = %s
             WHERE run_id = %s AND status = 'running'
            """,
            (
                now_ms + BRIEF_RETRY_MS,
                now_ms,
                now_ms,
                str(current["latest_run_id"]),
            ),
        )
    healthy = _healthy_current_publication(conn)
    publication_id = str(healthy["publication_id"]) if healthy is not None else None
    conn.execute(
        """
        UPDATE news_brief_current
           SET publication_id = %s,
               target_fingerprint = %s,
               latest_run_id = NULL,
               pending_first_dirty_at_ms = NULL,
               pending_due_at_ms = NULL,
               updated_at_ms = %s
         WHERE singleton_key = true
           AND (
             publication_id IS DISTINCT FROM %s
             OR target_fingerprint IS DISTINCT FROM %s
             OR latest_run_id IS NOT NULL
             OR pending_first_dirty_at_ms IS NOT NULL
             OR pending_due_at_ms IS NOT NULL
           )
        """,
        (
            publication_id,
            target_fingerprint_value,
            now_ms,
            publication_id,
            target_fingerprint_value,
        ),
    )


def _finish_without_model(
    conn: Any,
    *,
    selection: Mapping[str, Any],
    target_fingerprint_value: str,
    result: NewsBriefSynthesisResult,
    existing_run: Mapping[str, Any] | None,
    now_ms: int,
) -> None:
    run_id = str(existing_run["run_id"]) if existing_run is not None else _run_id(target_fingerprint_value)
    healthy = _healthy_current_publication(conn)
    action = "preserve_lkg" if healthy is not None else "advance_degraded"
    payload = _sealed_payload(
        selection=selection,
        target_fingerprint_value=target_fingerprint_value,
        result=result,
        now_ms=now_ms,
    )
    served_publication_id = (
        str(healthy["publication_id"]) if healthy is not None else _insert_publication(conn, payload)
    )
    conn.execute(
        """
        INSERT INTO news_brief_runs (
          run_id, target_fingerprint, selection_fingerprint,
          status, model_outcome, pointer_action, failure_count,
          next_due_at_ms, lease_owner, lease_token, lease_expires_at_ms,
          last_error_code, created_at_ms, updated_at_ms,
          last_attempt_at_ms, completed_at_ms
        )
        VALUES (%s, %s, %s, 'waiting_input', 'none', %s, 0,
                NULL, NULL, NULL, NULL, %s, %s, %s, NULL, %s)
        ON CONFLICT (target_fingerprint) DO UPDATE SET
          selection_fingerprint = EXCLUDED.selection_fingerprint,
          status = 'waiting_input', model_outcome = 'none',
          pointer_action = EXCLUDED.pointer_action,
          next_due_at_ms = NULL,
          lease_owner = NULL, lease_token = NULL, lease_expires_at_ms = NULL,
          last_error_code = EXCLUDED.last_error_code,
          updated_at_ms = EXCLUDED.updated_at_ms,
          completed_at_ms = EXCLUDED.completed_at_ms
        """,
        (
            run_id,
            target_fingerprint_value,
            str(selection["selection_fingerprint"]),
            action,
            INSIGHTS_SYNTHESIS_MISSING_CLUSTER,
            now_ms,
            now_ms,
            now_ms,
        ),
    )
    conn.execute(
        """
        UPDATE news_brief_current
           SET publication_id = %s,
               latest_run_id = %s,
               pending_first_dirty_at_ms = NULL,
               pending_due_at_ms = NULL,
               updated_at_ms = %s
         WHERE singleton_key = true AND target_fingerprint = %s
           AND (
             publication_id IS DISTINCT FROM %s
             OR latest_run_id IS DISTINCT FROM %s
             OR pending_first_dirty_at_ms IS NOT NULL
             OR pending_due_at_ms IS NOT NULL
           )
        """,
        (
            served_publication_id,
            run_id,
            now_ms,
            target_fingerprint_value,
            served_publication_id,
            run_id,
        ),
    )


def _finish_model_result(
    conn: Any,
    *,
    selection: Mapping[str, Any],
    target_fingerprint_value: str,
    result: NewsBriefSynthesisResult,
    run: Mapping[str, Any],
    now_ms: int,
) -> str:
    healthy = _healthy_current_publication(conn)
    payload = _sealed_payload(
        selection=selection,
        target_fingerprint_value=target_fingerprint_value,
        result=result,
        now_ms=now_ms,
    )
    if result.quality == "degraded" and healthy is not None:
        action = "preserve_lkg"
        served_publication_id = str(healthy["publication_id"])
    else:
        action = "advance_ok" if result.quality == "ok" else "advance_degraded"
        served_publication_id = _insert_publication(conn, payload)
    successful = result.brief_kind == "l1"
    conn.execute(
        """
        UPDATE news_brief_runs
           SET status = %s, model_outcome = %s, pointer_action = %s,
               failure_count = %s, next_due_at_ms = %s,
               lease_owner = NULL, lease_token = NULL, lease_expires_at_ms = NULL,
               last_error_code = %s,
               completed_at_ms = %s, updated_at_ms = %s
         WHERE run_id = %s
        """,
        (
            "published" if successful else "retry_wait",
            "ok" if successful else result.brief_kind,
            action,
            0 if successful else min(BRIEF_MAX_FAILURES, int(run["failure_count"] or 0) + 1),
            None if successful else now_ms + BRIEF_RETRY_MS,
            None if successful else _safe_error_code(result.validation.get("failure_code")),
            now_ms,
            now_ms,
            str(run["run_id"]),
        ),
    )
    conn.execute(
        """
        UPDATE news_brief_current
           SET publication_id = %s,
               latest_run_id = %s,
               pending_first_dirty_at_ms = NULL,
               pending_due_at_ms = NULL,
               updated_at_ms = %s
         WHERE singleton_key = true AND target_fingerprint = %s
           AND (
             publication_id IS DISTINCT FROM %s
             OR latest_run_id IS DISTINCT FROM %s
             OR pending_first_dirty_at_ms IS NOT NULL
             OR pending_due_at_ms IS NOT NULL
           )
        """,
        (
            served_publication_id,
            str(run["run_id"]),
            now_ms,
            target_fingerprint_value,
            served_publication_id,
            str(run["run_id"]),
        ),
    )
    return served_publication_id


def _sealed_payload(
    *,
    selection: Mapping[str, Any],
    target_fingerprint_value: str,
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
    payload: dict[str, Any] = {
        "selection_fingerprint": str(selection["selection_fingerprint"]),
        "target_fingerprint": target_fingerprint_value,
        "quality": result.quality,
        "brief_kind": result.brief_kind,
        "world_brief": result.world_brief,
        "brief_story_lines": [line.model_dump(mode="json") for line in result.brief_story_lines],
        "top_stories": top_stories,
        "selected_story_ids": [str(story["story_id"]) for story in top_stories],
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
        "created_at_ms": now_ms,
    }
    payload["publication_id"] = content_publication_id(payload)
    return payload


def _insert_publication(conn: Any, payload: Mapping[str, Any]) -> str:
    conn.execute(
        """
        INSERT INTO news_brief_publications (
          publication_id, selection_fingerprint, target_fingerprint,
          quality, brief_kind, world_brief, brief_story_lines,
          top_stories, selected_story_ids, sources, source_age_range,
          provider, model, prompt_version, workflow_version,
          composer_version, schema_version, selector_version,
          identity_version, locale, validation, provenance,
          published_at_ms, created_at_ms
        ) VALUES (
          %(publication_id)s, %(selection_fingerprint)s, %(target_fingerprint)s,
          %(quality)s, %(brief_kind)s, %(world_brief)s, %(brief_story_lines)s,
          %(top_stories)s, %(selected_story_ids)s, %(sources)s, %(source_age_range)s,
          %(provider)s, %(model)s, %(prompt_version)s, %(workflow_version)s,
          %(composer_version)s, %(schema_version)s, %(selector_version)s,
          %(identity_version)s, %(locale)s, %(validation)s, %(provenance)s,
          %(published_at_ms)s, %(created_at_ms)s
        )
        ON CONFLICT (publication_id) DO NOTHING
        """,
        {
            **dict(payload),
            "brief_story_lines": Jsonb(payload["brief_story_lines"]),
            "top_stories": Jsonb(payload["top_stories"]),
            "selected_story_ids": Jsonb(payload["selected_story_ids"]),
            "sources": Jsonb(payload["sources"]),
            "source_age_range": Jsonb(payload["source_age_range"]),
            "validation": Jsonb(payload["validation"]),
            "provenance": Jsonb(payload["provenance"]),
        },
    )
    return str(payload["publication_id"])


def _healthy_current_publication(conn: Any) -> Mapping[str, Any] | None:
    return cast(
        Mapping[str, Any] | None,
        conn.execute(
            """
            SELECT publication.*
              FROM news_brief_current current
              JOIN news_brief_publications publication
                ON publication.publication_id = current.publication_id
             WHERE current.singleton_key = true AND publication.quality = 'ok'
            """
        ).fetchone(),
    )


def _safe_error_code(value: object) -> str:
    normalized = str(value or "").strip()
    allowed = {
        INSIGHTS_SYNTHESIS_GATE,
        INSIGHTS_SYNTHESIS_MISSING_CLUSTER,
        INSIGHTS_SYNTHESIS_PARSE,
        INSIGHTS_SYNTHESIS_PROVIDER,
    }
    return normalized if normalized in allowed else INSIGHTS_SYNTHESIS_PROVIDER


def _publication_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    list_fields = {"brief_story_lines", "top_stories", "selected_story_ids", "sources"}
    object_fields = {"source_age_range", "validation", "provenance"}
    return {
        key: list(row[key]) if key in list_fields else dict(row[key]) if key in object_fields else row[key]
        for key in (
            "publication_id",
            "selection_fingerprint",
            "target_fingerprint",
            "quality",
            "brief_kind",
            "world_brief",
            "brief_story_lines",
            "top_stories",
            "selected_story_ids",
            "sources",
            "source_age_range",
            "provider",
            "model",
            "prompt_version",
            "workflow_version",
            "composer_version",
            "schema_version",
            "selector_version",
            "identity_version",
            "locale",
            "validation",
            "provenance",
            "published_at_ms",
            "created_at_ms",
        )
    }


def _run_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: row[key]
        for key in (
            "run_id",
            "target_fingerprint",
            "selection_fingerprint",
            "status",
            "model_outcome",
            "pointer_action",
            "failure_count",
            "next_due_at_ms",
            "lease_expires_at_ms",
            "last_error_code",
            "created_at_ms",
            "updated_at_ms",
            "last_attempt_at_ms",
            "completed_at_ms",
        )
    }


__all__ = [
    "BRIEF_DEBOUNCE_MS",
    "BRIEF_LEASE_MS",
    "BRIEF_MAX_FAILURES",
    "BRIEF_RETRY_MS",
    "fail_brief_run",
    "get_brief",
    "peek_brief_candidate",
    "prepare_brief_run",
    "publish_brief",
    "release_brief_claim",
    "start_brief_model",
]
