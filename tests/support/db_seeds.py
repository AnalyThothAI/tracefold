from __future__ import annotations

from typing import Any

HOT_PATH_COUNT_QUERIES: dict[str, str] = {
    "raw_frames": "SELECT count(*) AS value FROM raw_frames WHERE source = 'gmgn'",
    "events": "SELECT count(*) AS value FROM events WHERE event_id = %(event_id)s",
    "token_intents": "SELECT count(*) AS value FROM token_intents WHERE event_id = %(event_id)s",
    "token_intent_resolutions": (
        "SELECT count(*) AS value FROM token_intent_resolutions WHERE event_id = %(event_id)s"
    ),
    "enriched_events": "SELECT count(*) AS value FROM enriched_events WHERE event_id = %(event_id)s",
    "ready_enriched_events": (
        "SELECT count(*) AS value FROM enriched_events WHERE event_id = %(event_id)s AND tick_id IS NOT NULL"
    ),
    "event_anchor_jobs": "SELECT count(*) AS value FROM event_anchor_backfill_jobs WHERE event_id = %(event_id)s",
    "market_ticks": "SELECT count(*) AS value FROM market_ticks",
}


def scalar(conn: Any, sql: str, params: dict[str, Any] | tuple[Any, ...] | None = None) -> Any:
    row = conn.execute(sql, params or {}).fetchone()
    if row is None:
        return None
    return row["value"] if isinstance(row, dict) else row[0]


def hot_path_counts(conn: Any, *, event_id: str) -> dict[str, int]:
    params = {"event_id": event_id}
    return {name: int(scalar(conn, sql, params) or 0) for name, sql in HOT_PATH_COUNT_QUERIES.items()}


def first_row(conn: Any, sql: str, params: dict[str, Any] | tuple[Any, ...] | None = None) -> dict[str, Any]:
    row = conn.execute(sql, params or {}).fetchone()
    assert row is not None
    return dict(row)


def assert_count_at_least(counts: dict[str, int], name: str, minimum: int = 1) -> None:
    actual = counts.get(name, 0)
    assert actual >= minimum, f"expected {name} >= {minimum}, got {actual}; counts={counts}"
