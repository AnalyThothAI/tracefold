from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from dataclasses import asdict
from typing import Any, TypedDict

from psycopg.types.json import Jsonb

from tracefold.market.capture.entity import EVM_QUERY_CHAINS, normalize_ca
from tracefold.market.capture.tweet_identity import canonical_tweet_url, logical_dedup_key
from tracefold.market.capture.tweet_text import build_text_projection
from tracefold.market.capture.twitter_event import TwitterEvent
from tracefold.platform.postgres.postgres_client import require_transaction
from tracefold.platform.postgres.write_contract import mutation_count
from tracefold.platform.validation import require_nonnegative_int, require_positive_int


class EventRead(TypedDict):
    event_id: str
    logical_dedup_key: str
    canonical_url: str | None
    source_provider: str
    source_transport: str
    coverage: str
    channel: str
    action: str
    original_action: str | None
    tweet_id: str | None
    internal_id: str | None
    timestamp_ms: int
    received_at_ms: int
    author_handle: str | None
    author_name: str | None
    author_avatar: str | None
    author_followers: int | None
    author_tags: list[str]
    text: str | None
    text_raw: str | None
    text_clean: str | None
    search_text: str | None
    urls: list[str]
    cashtags: list[str]
    hashtags: list[str]
    mentions: list[str]
    media: list[dict[str, Any]]
    reference: dict[str, Any] | None
    raw: dict[str, Any] | None
    unfollow_target: dict[str, Any] | None
    avatar_change: dict[str, Any] | None
    bio_change: dict[str, Any] | None
    token_snapshot: dict[str, Any] | None


class EvidenceRepository:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def require_transaction(self, *, operation: str) -> None:
        require_transaction(self.conn, operation=operation)

    def insert_raw_frame(
        self,
        *,
        source: str,
        channel: str,
        received_at_ms: int,
        raw_payload_json: str,
    ) -> bool:
        require_transaction(self.conn, operation="insert_raw_frame")
        sanitized_payload = _sanitize_postgres_value(raw_payload_json)
        payload_hash = hashlib.sha256(sanitized_payload.encode("utf-8")).hexdigest()
        frame_id = f"{source}:{channel}:{payload_hash}"
        cursor = self.conn.execute(
            """
            INSERT INTO raw_frames(
              frame_id, source, channel, received_at_ms, payload_hash, raw_payload_json, created_at_ms
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(frame_id) DO NOTHING
            """,
            (frame_id, source, channel, received_at_ms, payload_hash, sanitized_payload, _now_ms()),
        )
        rowcount = mutation_count(cursor, error_code="evidence_repository_rowcount_invalid")
        if rowcount not in (0, 1):
            raise TypeError("evidence_repository_rowcount_invalid")
        return rowcount == 1

    def insert_event(self, event: TwitterEvent) -> bool:
        require_transaction(self.conn, operation="insert_event")
        now_ms = _now_ms()
        row = event_to_row(event, now_ms=now_ms)
        return self.insert_event_row(row)

    def insert_event_row(self, row: dict[str, Any]) -> bool:
        require_transaction(self.conn, operation="insert_event_row")
        cursor = self.conn.execute(
            """
            INSERT INTO events(
              event_id, logical_dedup_key, canonical_url, source_provider, source_transport,
              coverage, channel, action, original_action, tweet_id, internal_id, timestamp_ms,
              received_at_ms, author_handle, author_name, author_avatar, author_followers,
              author_tags_json, text, text_raw, text_clean, search_text, urls_json, cashtags_json,
              hashtags_json, mentions_json, media_json, reference_json, raw_json, event_json,
              created_at_ms, updated_at_ms
            )
            VALUES (
              %(event_id)s, %(logical_dedup_key)s, %(canonical_url)s, %(source_provider)s, %(source_transport)s,
              %(coverage)s, %(channel)s, %(action)s, %(original_action)s, %(tweet_id)s, %(internal_id)s,
              %(timestamp_ms)s,
              %(received_at_ms)s, %(author_handle)s, %(author_name)s, %(author_avatar)s, %(author_followers)s,
              %(author_tags_json)s, %(text)s, %(text_raw)s, %(text_clean)s, %(search_text)s, %(urls_json)s,
              %(cashtags_json)s,
              %(hashtags_json)s, %(mentions_json)s, %(media_json)s, %(reference_json)s,
              %(raw_json)s, %(event_json)s, %(created_at_ms)s, %(updated_at_ms)s
            )
            ON CONFLICT DO NOTHING
            """,
            row,
        )
        rowcount = mutation_count(cursor, error_code="evidence_repository_rowcount_invalid")
        if rowcount not in (0, 1):
            raise TypeError("evidence_repository_rowcount_invalid")
        return rowcount == 1

    def event_exists(self, *, event_id: str, logical_dedup_key: str) -> bool:
        row = self.conn.execute(
            """
            SELECT 1 AS found
            FROM events
            WHERE event_id = %s OR logical_dedup_key = %s
            LIMIT 1
            """,
            (event_id, logical_dedup_key),
        ).fetchone()
        return bool(row)

    def recent_events(
        self,
        *,
        limit: int,
        handles: set[str] | None = None,
        ca: str | None = None,
        chain: str | None = None,
        symbol: str | None = None,
        since_ms: int | None = None,
        before: tuple[int, str] | None = None,
    ) -> list[EventRead]:
        parsed_limit = require_nonnegative_int(limit, error_code="evidence_recent_events_limit_required")
        if parsed_limit == 0:
            return []
        clauses: list[str] = []
        params: list[Any] = []
        if since_ms is not None:
            clauses.append("e.received_at_ms >= %s")
            params.append(int(since_ms))
        if before is not None:
            clauses.append("(e.received_at_ms, e.event_id) < (%s, %s)")
            params.extend([int(before[0]), str(before[1])])
        normalized_handles = {item.strip().lstrip("@").lower() for item in handles or set() if item.strip()}
        if normalized_handles:
            placeholders = ",".join("%s" for _ in normalized_handles)
            clauses.append(f"e.author_handle IN ({placeholders})")
            params.extend(sorted(normalized_handles))
        join = ""
        if ca:
            normalized_chain, normalized_ca = normalize_ca(ca, chain=chain)
            join = "JOIN event_entities ee ON ee.event_id = e.event_id"
            clauses.extend(["ee.entity_type = 'ca'", "ee.normalized_value = %s"])
            params.append(normalized_ca)
            if normalized_chain == "evm_unknown":
                placeholders = ",".join("%s" for _ in EVM_QUERY_CHAINS)
                clauses.append(f"ee.chain IN ({placeholders})")
                params.extend(sorted(EVM_QUERY_CHAINS))
            else:
                clauses.append("ee.chain = %s")
                params.append(normalized_chain)
        elif symbol:
            join = "JOIN event_entities ee ON ee.event_id = e.event_id"
            clauses.extend(["ee.entity_type = 'symbol'", "ee.normalized_value = %s"])
            params.append(symbol.strip().lstrip("$").upper())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"""
            SELECT e.*
            FROM events e
            {join}
            {where}
            ORDER BY e.received_at_ms DESC, e.event_id DESC
            LIMIT %s
            """,
            (*params, parsed_limit),
        ).fetchall()
        return [decode_event_row(row) for row in rows]

    def recent_events_for_token_filters(
        self,
        *,
        limit: int,
        per_filter_limit: int,
        cas: set[tuple[str, str]] | None = None,
        symbols: set[str] | None = None,
        since_ms: int | None = None,
    ) -> list[EventRead]:
        parsed_limit = require_positive_int(limit, error_code="evidence_token_filter_limit_required")
        parsed_per_filter_limit = require_positive_int(
            per_filter_limit,
            error_code="evidence_token_filter_per_filter_limit_required",
        )

        filter_kinds, filter_chains, filter_values = _token_filter_keysets(cas=cas, symbols=symbols)
        if not filter_kinds:
            return []

        clauses: list[str] = []
        params: list[Any] = [filter_kinds, filter_chains, filter_values, sorted(EVM_QUERY_CHAINS)]
        if since_ms is not None:
            clauses.append("e.received_at_ms >= %s")
            params.append(int(since_ms))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"""
            WITH input_filters AS (
              SELECT filter_kind, filter_chain, filter_value, ordinality
              FROM unnest(%s::text[], %s::text[], %s::text[]) WITH ORDINALITY
                AS filters(filter_kind, filter_chain, filter_value, ordinality)
            ),
            distinct_filters AS (
              SELECT DISTINCT ON (filter_kind, filter_chain, filter_value)
                     filter_kind, filter_chain, filter_value, ordinality
              FROM input_filters
              ORDER BY filter_kind, filter_chain, filter_value, ordinality
            ),
            ranked_events AS (
              SELECT e.*,
                     ROW_NUMBER() OVER (
                       PARTITION BY filters.filter_kind, filters.filter_chain, filters.filter_value
                       ORDER BY e.received_at_ms DESC, e.event_id DESC
                     ) AS event_rank
              FROM distinct_filters filters
              JOIN event_entities ee
                ON (
                  filters.filter_kind = 'symbol'
                  AND ee.entity_type = 'symbol'
                  AND ee.normalized_value = filters.filter_value
                )
                OR (
                  filters.filter_kind = 'ca'
                  AND ee.entity_type = 'ca'
                  AND ee.normalized_value = filters.filter_value
                  AND (
                    (filters.filter_chain = 'evm_unknown' AND ee.chain = ANY(%s::text[]))
                    OR ee.chain = filters.filter_chain
                  )
                )
              JOIN events e ON e.event_id = ee.event_id
              {where}
            ),
            bounded_events AS (
              SELECT *
              FROM ranked_events
              WHERE event_rank <= %s
            ),
            deduped_events AS (
              SELECT DISTINCT ON (event_id) *
              FROM bounded_events
              ORDER BY event_id, received_at_ms DESC
            )
            SELECT *
            FROM deduped_events
            ORDER BY received_at_ms DESC, event_id DESC
            LIMIT %s
            """,
            (*params, parsed_per_filter_limit, parsed_limit),
        ).fetchall()
        return [decode_event_row(row) for row in rows]

    def events_by_ids(self, event_ids: list[str]) -> dict[str, EventRead]:
        if not event_ids:
            return {}
        placeholders = ",".join("%s" for _ in event_ids)
        rows = self.conn.execute(f"SELECT * FROM events WHERE event_id IN ({placeholders})", event_ids).fetchall()
        return {str(row["event_id"]): decode_event_row(row) for row in rows}

    def counts(self, *, since_ms: int | None = None) -> dict[str, int]:
        suffix = " WHERE received_at_ms >= %s" if since_ms is not None else ""
        params = (since_ms,) if since_ms is not None else ()
        return {
            "raw_frames": int(
                self.conn.execute(f"SELECT COUNT(*) AS count FROM raw_frames{suffix}", params).fetchone()["count"]
            ),
            "events": int(
                self.conn.execute(f"SELECT COUNT(*) AS count FROM events{suffix}", params).fetchone()["count"]
            ),
        }

    def close(self) -> None:
        self.conn.close()


def _token_filter_keysets(
    *,
    cas: set[tuple[str, str]] | None,
    symbols: set[str] | None,
) -> tuple[list[str], list[str], list[str]]:
    filters: list[tuple[str, str, str]] = []
    for chain, ca in sorted(cas or set()):
        normalized_chain, normalized_ca = normalize_ca(ca, chain=chain)
        filters.append(("ca", normalized_chain, normalized_ca))
    for symbol in sorted(symbols or set()):
        normalized_symbol = str(symbol).strip().lstrip("$").upper()
        if normalized_symbol:
            filters.append(("symbol", "", normalized_symbol))

    if not filters:
        return [], [], []
    return [item[0] for item in filters], [item[1] for item in filters], [item[2] for item in filters]


def materialize_event(
    event: TwitterEvent,
    *,
    now_ms: int,
) -> tuple[dict[str, Any], EventRead]:
    event_dict = event.to_dict()
    reference_text = event.reference.text if event.reference else None
    projection = build_text_projection(event.content.text, reference_text=reference_text)
    event_read: EventRead = _sanitize_postgres_value(
        {
            "event_id": event.event_id,
            "logical_dedup_key": logical_dedup_key(event),
            "canonical_url": canonical_tweet_url(event),
            "source_provider": event.source.provider,
            "source_transport": event.source.transport,
            "coverage": event.source.coverage,
            "channel": event.source.channel,
            "action": event.action,
            "original_action": event.original_action,
            "tweet_id": event.tweet_id,
            "internal_id": event.internal_id,
            "timestamp_ms": event.timestamp * 1000 if event.timestamp < 10_000_000_000 else event.timestamp,
            "received_at_ms": event.received_at_ms,
            "author_handle": event.author.handle.lower() if event.author.handle else None,
            "author_name": event.author.name,
            "author_avatar": event.author.avatar,
            "author_followers": event.author.followers,
            "author_tags": list(event.author.tags),
            "text": event.content.text,
            "text_raw": projection.text_raw,
            "text_clean": projection.text_clean,
            "search_text": projection.search_text,
            "urls": list(projection.urls),
            "cashtags": list(projection.cashtags),
            "hashtags": list(projection.hashtags),
            "mentions": list(projection.mentions),
            "media": [asdict(item) for item in event.content.media],
            "reference": event_dict["reference"],
            "raw": event.raw,
            "unfollow_target": event_dict["unfollow_target"],
            "avatar_change": event_dict["avatar_change"],
            "bio_change": event_dict["bio_change"],
            "token_snapshot": event_dict["token_snapshot"],
        }
    )
    event_read_values: Mapping[str, object] = event_read
    sanitized: dict[str, Any] = _sanitize_postgres_value(
        {
            **{
                key: event_read_values[key]
                for key in (
                    "event_id",
                    "logical_dedup_key",
                    "canonical_url",
                    "source_provider",
                    "source_transport",
                    "coverage",
                    "channel",
                    "action",
                    "original_action",
                    "tweet_id",
                    "internal_id",
                    "timestamp_ms",
                    "received_at_ms",
                    "author_handle",
                    "author_name",
                    "author_avatar",
                    "author_followers",
                    "text",
                    "text_raw",
                    "text_clean",
                    "search_text",
                )
            },
            "author_tags_json": _json(event_read["author_tags"]),
            "urls_json": _json(event_read["urls"]),
            "cashtags_json": _json(event_read["cashtags"]),
            "hashtags_json": _json(event_read["hashtags"]),
            "mentions_json": _json(event_read["mentions"]),
            "media_json": _json(event_read["media"]),
            "reference_json": _json(event_read["reference"]),
            "raw_json": _json(event_read["raw"]),
            "event_json": _json(event_dict),
            "created_at_ms": now_ms,
            "updated_at_ms": now_ms,
        }
    )
    return sanitized, event_read


def event_to_row(event: TwitterEvent, *, now_ms: int) -> dict[str, Any]:
    row, _event_read = materialize_event(event, now_ms=now_ms)
    return row


def decode_event_row(row: Mapping[str, Any]) -> EventRead:
    data = dict(row)
    event_payload = _required_json_object(data["event_json"], field="event_json")
    return {
        "event_id": _required_text(data["event_id"], field="event_id"),
        "logical_dedup_key": _required_text(data["logical_dedup_key"], field="logical_dedup_key"),
        "canonical_url": _optional_text(data["canonical_url"], field="canonical_url"),
        "source_provider": _required_text(data["source_provider"], field="source_provider"),
        "source_transport": _required_text(data["source_transport"], field="source_transport"),
        "coverage": _required_text(data["coverage"], field="coverage"),
        "channel": _required_text(data["channel"], field="channel"),
        "action": _required_text(data["action"], field="action"),
        "original_action": _optional_text(data["original_action"], field="original_action"),
        "tweet_id": _optional_text(data["tweet_id"], field="tweet_id"),
        "internal_id": _optional_text(data["internal_id"], field="internal_id"),
        "timestamp_ms": _required_positive_int(data["timestamp_ms"], field="timestamp_ms"),
        "received_at_ms": _required_positive_int(data["received_at_ms"], field="received_at_ms"),
        "author_handle": _optional_text(data["author_handle"], field="author_handle"),
        "author_name": _optional_text(data["author_name"], field="author_name"),
        "author_avatar": _optional_text(data["author_avatar"], field="author_avatar"),
        "author_followers": _optional_nonnegative_int(data["author_followers"], field="author_followers"),
        "author_tags": _required_string_list(data["author_tags_json"], field="author_tags_json"),
        "text": _optional_text(data["text"], field="text"),
        "text_raw": _optional_text(data["text_raw"], field="text_raw"),
        "text_clean": _optional_text(data["text_clean"], field="text_clean"),
        "search_text": _optional_text(data["search_text"], field="search_text"),
        "urls": _required_string_list(data["urls_json"], field="urls_json"),
        "cashtags": _required_string_list(data["cashtags_json"], field="cashtags_json"),
        "hashtags": _required_string_list(data["hashtags_json"], field="hashtags_json"),
        "mentions": _required_string_list(data["mentions_json"], field="mentions_json"),
        "media": _required_object_list(data["media_json"], field="media_json"),
        "reference": _optional_json_object(data["reference_json"], field="reference_json"),
        "raw": _optional_json_object(data["raw_json"], field="raw_json"),
        "unfollow_target": _optional_json_object(event_payload["unfollow_target"], field="unfollow_target"),
        "avatar_change": _optional_json_object(event_payload["avatar_change"], field="avatar_change"),
        "bio_change": _optional_json_object(event_payload["bio_change"], field="bio_change"),
        "token_snapshot": _optional_json_object(event_payload["token_snapshot"], field="token_snapshot"),
    }


def _json(value: Any) -> Jsonb:
    return Jsonb(
        _sanitize_postgres_value(value),
        dumps=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True),
    )


def _sanitize_postgres_value(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {_sanitize_postgres_value(key): _sanitize_postgres_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_postgres_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_postgres_value(item) for item in value)
    return value


def _required_json_object(value: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"event_read_{field}_mapping_required")
    return dict(value)


def _optional_json_object(value: Any, *, field: str) -> dict[str, Any] | None:
    if value is None:
        return None
    return _required_json_object(value, field=field)


def _required_string_list(value: Any, *, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise TypeError(f"event_read_{field}_string_list_required")
    return list(value)


def _required_object_list(value: Any, *, field: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise TypeError(f"event_read_{field}_object_list_required")
    return [dict(item) for item in value]


def _optional_text(value: Any, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"event_read_{field}_text_required")
    return value


def _required_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"event_read_{field}_text_required")
    return value


def _required_positive_int(value: Any, *, field: str) -> int:
    parsed = _required_nonnegative_int(value, field=field)
    if parsed == 0:
        raise TypeError(f"event_read_{field}_positive_int_required")
    return parsed


def _required_nonnegative_int(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TypeError(f"event_read_{field}_nonnegative_int_required")
    return value


def _optional_nonnegative_int(value: Any, *, field: str) -> int | None:
    if value is None:
        return None
    return _required_nonnegative_int(value, field=field)


def _required_bool(value: Any, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"event_read_{field}_bool_required")
    return value


def _now_ms() -> int:
    return int(time.time() * 1000)
