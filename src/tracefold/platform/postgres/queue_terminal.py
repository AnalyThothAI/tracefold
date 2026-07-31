from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from typing import Any

from psycopg.types.json import Jsonb

from tracefold.platform.postgres.json_safety import postgres_safe_json
from tracefold.platform.postgres.write_contract import mutation_count, returning_mutation_count

TERMINAL_ACTIONS = frozenset(("retry", "archive", "quarantine"))
_RESOLVED_RETENTION_MS = 30 * 24 * 60 * 60 * 1_000
_RESOLVED_PRUNE_LIMIT = 500

RetryTransitionMap = Mapping[tuple[str, str], Callable[..., dict[str, Any]]]


def terminalize_source_row(
    conn: Any,
    *,
    owner_key: str,
    source_table: str,
    target_key: str,
    source_row: Mapping[str, Any],
    final_status: str,
    final_reason: str,
    final_reason_bucket: str | None = None,
    now_ms: int,
    attempt_count: int | None = None,
    payload_hash: str | None = None,
    first_seen_at_ms: int | None = None,
    last_attempted_at_ms: int | None = None,
) -> dict[str, Any]:
    normalized_row = _normalized_source_row(source_row, payload_hash=payload_hash)
    normalized_payload_hash = _payload_hash(normalized_row.get("payload_hash"))
    source_row_hash = _stable_json_hash(normalized_row)
    row_attempt_count = _terminal_attempt_count(
        normalized_row,
        explicit_attempt_count=attempt_count,
    )
    terminal_generation = _next_terminal_generation(
        conn,
        owner_key=owner_key,
        source_table=source_table,
        target_key=target_key,
        source_row_hash=source_row_hash,
    )
    terminal_id = _terminal_id(
        owner_key=owner_key,
        source_table=source_table,
        target_key=target_key,
        source_row_hash=source_row_hash,
        terminal_generation=terminal_generation,
    )
    row_first_seen_at_ms = (
        first_seen_at_ms
        if first_seen_at_ms is not None
        else _optional_int(normalized_row.get("first_dirty_at_ms") or normalized_row.get("created_at_ms"))
    )
    row_last_attempted_at_ms = (
        last_attempted_at_ms
        if last_attempted_at_ms is not None
        else _optional_int(normalized_row.get("updated_at_ms") or normalized_row.get("last_attempted_at_ms"))
    )
    params = {
        "terminal_id": terminal_id,
        "owner_key": _required_text(owner_key, "owner_key"),
        "source_table": _required_text(source_table, "source_table"),
        "target_key": _required_text(target_key, "target_key"),
        "source_row_json": _jsonb(normalized_row),
        "source_row_hash": source_row_hash,
        "final_status": _required_text(final_status, "final_status"),
        "final_reason": _required_text(final_reason, "final_reason"),
        "final_reason_bucket": _reason_bucket(
            final_reason_bucket,
            final_reason=final_reason,
        ),
        "attempt_count": row_attempt_count,
        "payload_hash": normalized_payload_hash,
        "first_seen_at_ms": row_first_seen_at_ms,
        "last_attempted_at_ms": row_last_attempted_at_ms,
        "terminalized_at_ms": int(now_ms),
        "terminal_generation": terminal_generation,
    }
    cursor = conn.execute(
        """
        INSERT INTO queue_terminal_events(
          terminal_id,
          owner_key,
          source_table,
          target_key,
          source_row_json,
          source_row_hash,
          final_status,
          final_reason,
          final_reason_bucket,
          attempt_count,
          payload_hash,
          first_seen_at_ms,
          last_attempted_at_ms,
          terminalized_at_ms,
          terminal_generation
        )
        VALUES (
          %(terminal_id)s,
          %(owner_key)s,
          %(source_table)s,
          %(target_key)s,
          %(source_row_json)s,
          %(source_row_hash)s,
          %(final_status)s,
          %(final_reason)s,
          %(final_reason_bucket)s,
          %(attempt_count)s,
          %(payload_hash)s,
          %(first_seen_at_ms)s,
          %(last_attempted_at_ms)s,
          %(terminalized_at_ms)s,
          %(terminal_generation)s
        )
        ON CONFLICT(owner_key, source_table, target_key)
          WHERE operator_action IS NULL
        DO UPDATE SET
          terminal_id = EXCLUDED.terminal_id,
          source_row_json = EXCLUDED.source_row_json,
          source_row_hash = EXCLUDED.source_row_hash,
          final_status = EXCLUDED.final_status,
          final_reason = EXCLUDED.final_reason,
          final_reason_bucket = CASE
            WHEN queue_terminal_events.final_reason IS DISTINCT FROM EXCLUDED.final_reason
            THEN EXCLUDED.final_reason_bucket
            ELSE queue_terminal_events.final_reason_bucket
          END,
          attempt_count = GREATEST(queue_terminal_events.attempt_count, EXCLUDED.attempt_count),
          payload_hash = EXCLUDED.payload_hash,
          terminalized_at_ms = EXCLUDED.terminalized_at_ms,
          last_attempted_at_ms = COALESCE(
            EXCLUDED.last_attempted_at_ms,
            queue_terminal_events.last_attempted_at_ms
          )
        RETURNING *
        """,
        params,
    )
    row = cursor.fetchone()
    returning_mutation_count(cursor, row, error_code="queue_terminal_rowcount_invalid")
    return _row_dict(row)


def inspect_terminal_events(
    conn: Any,
    *,
    owner_key: str | None = None,
    source_table: str | None = None,
    reason_bucket: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    parsed_limit = max(1, min(500, int(limit)))
    where = ["operator_action IS NULL"]
    params: dict[str, Any] = {"limit": parsed_limit}
    if owner_key:
        where.append("owner_key = %(owner_key)s")
        params["owner_key"] = str(owner_key)
    if source_table:
        where.append("source_table = %(source_table)s")
        params["source_table"] = str(source_table)
    if reason_bucket:
        where.append("final_reason_bucket = %(reason_bucket)s")
        params["reason_bucket"] = _required_text(reason_bucket, "reason_bucket")
    rows = conn.execute(
        f"""
        SELECT *
        FROM queue_terminal_events
        WHERE {" AND ".join(where)}
        ORDER BY terminalized_at_ms DESC, terminal_id ASC
        LIMIT %(limit)s
        """,
        params,
    ).fetchall()
    items = [_row_dict(row) for row in rows]
    return {
        "status": "terminal",
        "owner": owner_key or None,
        "source_table": source_table or None,
        "reason_bucket": reason_bucket or None,
        "limit": parsed_limit,
        "count": len(items),
        "items": items,
    }


def list_terminal_event_ids(
    conn: Any,
    *,
    owner_key: str,
    source_table: str,
    reason_bucket: str,
    limit: int = 100,
) -> list[str]:
    parsed_limit = max(1, min(500, int(limit)))
    rows = conn.execute(
        """
        SELECT terminal_id
        FROM queue_terminal_events
        WHERE operator_action IS NULL
          AND owner_key = %(owner_key)s
          AND source_table = %(source_table)s
          AND final_reason_bucket = %(reason_bucket)s
        ORDER BY terminalized_at_ms ASC, terminal_id ASC
        LIMIT %(limit)s
        """,
        {
            "owner_key": _required_text(owner_key, "owner_key"),
            "source_table": _required_text(source_table, "source_table"),
            "reason_bucket": _required_text(reason_bucket, "reason_bucket"),
            "limit": parsed_limit,
        },
    ).fetchall()
    return [str(row["terminal_id"]) for row in rows]


def terminal_reason_bucket(final_reason: str | None) -> str:
    reason = str(final_reason or "").lower()
    if "retry_budget_exhausted" in reason or "failed_exhausted" in reason or "max_attempt" in reason:
        return "retry_budget_exhausted"
    if "provider_no_quote" in reason:
        return "provider_no_quote"
    if "provider_unavailable" in reason or "transport" in reason or "connection" in reason:
        return "provider_unavailable"
    if "provider_error" in reason:
        return "provider_error"
    if "no_market_data" in reason:
        return "no_market_data"
    if "stale" in reason:
        return "stale_window_ttl"
    if "timeout" in reason:
        return "timeout"
    if "not_found" in reason:
        return "not_found"
    return "other"


def resolve_terminal_event(
    conn: Any,
    *,
    terminal_id: str,
    action: str,
    reason: str,
    now_ms: int,
    retry_transitions: RetryTransitionMap | None = None,
) -> dict[str, Any]:
    normalized_action = _action(action)
    normalized_reason = _required_text(reason, "reason")
    terminal_id = _required_text(terminal_id, "terminal_id")
    current = _fetch_terminal_event(conn, terminal_id=terminal_id, for_update=True)
    if current is None:
        raise ValueError("terminal_event_not_found")
    transition = None
    if normalized_action == "retry":
        transition = _retry_transition_for(current, retry_transitions)
    cursor = conn.execute(
        """
        UPDATE queue_terminal_events
        SET operator_action = %(operator_action)s,
            operator_reason = %(operator_reason)s,
            operator_action_at_ms = %(operator_action_at_ms)s
        WHERE terminal_id = %(terminal_id)s
        RETURNING *
        """,
        {
            "terminal_id": terminal_id,
            "operator_action": normalized_action,
            "operator_reason": normalized_reason,
            "operator_action_at_ms": int(now_ms),
        },
    )
    row = cursor.fetchone()
    returning_mutation_count(cursor, row, error_code="queue_terminal_rowcount_invalid")
    resolved = _row_dict(row)
    if transition is not None:
        resolved["transition"] = transition(dict(resolved), now_ms=int(now_ms), reason=normalized_reason)
    _prune_resolved_terminal_events(conn, now_ms=int(now_ms))
    return resolved


def _prune_resolved_terminal_events(conn: Any, *, now_ms: int) -> None:
    cursor = conn.execute(
        """
        WITH resolved_to_prune AS (
          SELECT terminal_id
          FROM queue_terminal_events
          WHERE operator_action IS NOT NULL
            AND COALESCE(operator_action_at_ms, terminalized_at_ms) < %(cutoff_ms)s
          ORDER BY COALESCE(operator_action_at_ms, terminalized_at_ms) ASC, terminal_id ASC
          LIMIT %(limit)s
          FOR UPDATE SKIP LOCKED
        )
        DELETE FROM queue_terminal_events AS terminal
        USING resolved_to_prune AS expired
        WHERE terminal.terminal_id = expired.terminal_id
        """,
        {
            "cutoff_ms": max(0, int(now_ms) - _RESOLVED_RETENTION_MS),
            "limit": _RESOLVED_PRUNE_LIMIT,
        },
    )
    count = mutation_count(cursor, error_code="queue_terminal_rowcount_invalid")
    if count > _RESOLVED_PRUNE_LIMIT:
        raise TypeError("queue_terminal_rowcount_invalid")


def _fetch_terminal_event(conn: Any, *, terminal_id: str, for_update: bool = False) -> dict[str, Any] | None:
    suffix = "FOR UPDATE" if for_update else ""
    row = conn.execute(
        f"""
        SELECT *
        FROM queue_terminal_events
        WHERE terminal_id = %(terminal_id)s
        {suffix}
        """,
        {"terminal_id": terminal_id},
    ).fetchone()
    return _row_dict(row) if row is not None else None


def _retry_transition_for(
    event: Mapping[str, Any],
    retry_transitions: RetryTransitionMap | None,
) -> Callable[..., dict[str, Any]]:
    key = (str(event.get("owner_key") or ""), str(event.get("source_table") or ""))
    transition = (retry_transitions or {}).get(key)
    if transition is None:
        raise ValueError(f"retry_transition_unregistered:{key[0]}:{key[1]}")
    return transition


def _normalized_source_row(source_row: Mapping[str, Any], *, payload_hash: str | None) -> dict[str, Any]:
    row = dict(postgres_safe_json(dict(source_row)))
    row["payload_hash"] = _payload_hash(payload_hash if payload_hash is not None else row.get("payload_hash"))
    return row


def _stable_json_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        postgres_safe_json(dict(value)),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _terminal_id(
    *,
    owner_key: str,
    source_table: str,
    target_key: str,
    source_row_hash: str,
    terminal_generation: int,
) -> str:
    encoded = "|".join(
        (
            _required_text(owner_key, "owner_key"),
            _required_text(source_table, "source_table"),
            _required_text(target_key, "target_key"),
            source_row_hash,
            str(_required_positive_int(terminal_generation, error_code="queue_terminal_generation_contract_required")),
        )
    )
    return "wqte_" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _next_terminal_generation(
    conn: Any,
    *,
    owner_key: str,
    source_table: str,
    target_key: str,
    source_row_hash: str,
) -> int:
    unresolved = conn.execute(
        """
        SELECT terminal_generation
        FROM queue_terminal_events
        WHERE owner_key = %(owner_key)s
          AND source_table = %(source_table)s
          AND target_key = %(target_key)s
          AND operator_action IS NULL
        LIMIT 1
        """,
        {
            "owner_key": _required_text(owner_key, "owner_key"),
            "source_table": _required_text(source_table, "source_table"),
            "target_key": _required_text(target_key, "target_key"),
        },
    ).fetchone()
    if unresolved is not None:
        return _terminal_generation_from_row(unresolved)
    row = conn.execute(
        """
        SELECT COALESCE(MAX(terminal_generation), 0) + 1 AS terminal_generation
        FROM queue_terminal_events
        WHERE owner_key = %(owner_key)s
          AND source_table = %(source_table)s
          AND target_key = %(target_key)s
          AND source_row_hash = %(source_row_hash)s
        """,
        {
            "owner_key": _required_text(owner_key, "owner_key"),
            "source_table": _required_text(source_table, "source_table"),
            "target_key": _required_text(target_key, "target_key"),
            "source_row_hash": source_row_hash,
        },
    ).fetchone()
    return _terminal_generation_from_row(row)


def _jsonb(value: Mapping[str, Any]) -> Jsonb:
    return Jsonb(
        dict(value),
        dumps=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )


def _row_dict(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    out = dict(row)
    source_row = out.get("source_row_json")
    if isinstance(source_row, Jsonb):
        out["source_row_json"] = source_row.obj
    return out


def _payload_hash(value: Any) -> str:
    return str(value or "").strip()


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _terminal_attempt_count(
    source_row: Mapping[str, Any],
    *,
    explicit_attempt_count: int | None,
) -> int:
    if explicit_attempt_count is not None:
        return _required_non_negative_int(
            explicit_attempt_count,
            error_code="queue_terminal_attempt_contract_required",
        )
    try:
        value = source_row["attempt_count"]
    except KeyError as exc:
        raise RuntimeError("queue_terminal_attempt_contract_required") from exc
    return _required_non_negative_int(value, error_code="queue_terminal_attempt_contract_required")


def _terminal_generation_from_row(row: Mapping[str, Any] | None) -> int:
    if row is None:
        raise RuntimeError("queue_terminal_generation_contract_required")
    try:
        value = row["terminal_generation"]
    except KeyError as exc:
        raise RuntimeError("queue_terminal_generation_contract_required") from exc
    return _required_positive_int(value, error_code="queue_terminal_generation_contract_required")


def _required_non_negative_int(value: Any, *, error_code: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(error_code) from exc
    if parsed < 0:
        raise RuntimeError(error_code)
    return parsed


def _required_positive_int(value: Any, *, error_code: str) -> int:
    parsed = _required_non_negative_int(value, error_code=error_code)
    if parsed < 1:
        raise RuntimeError(error_code)
    return parsed


def _required_text(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name}_required")
    return text


def _reason_bucket(value: str | None, *, final_reason: str | None) -> str:
    text = str(value or "").strip()
    if text:
        return text
    return terminal_reason_bucket(final_reason)


def _action(value: str) -> str:
    action = str(value or "").strip()
    if action not in TERMINAL_ACTIONS:
        raise ValueError(f"unsupported_terminal_action:{action}")
    return action
