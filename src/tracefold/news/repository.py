from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from math import isfinite
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from psycopg.types.json import Jsonb

from . import brief_store, query_specs
from .models import (
    CLASSIFIER_VERSION,
    IMPORTANCE_VERSION,
    STORY_IDENTITY_VERSION,
    NewsBriefSynthesisResult,
    NewsFeedFetch,
    NewsSourceDefinition,
)
from .notification import evaluate_news_push_eligibility
from .opennews import OpenNewsEvent

_ACTIVE_WINDOW_MS = 96 * 60 * 60 * 1000
_STORY_ACTIVE_WINDOW_MS = 12 * 60 * 60 * 1000
_NEWS_STALL_AFTER_MS = 120_000
_PUSH_SUMMARY_BUCKET = {
    "suppressed": "suppressed_count",
    "pending_translation": "pending_count",
    "pending_delivery": "pending_count",
    "retry_wait": "retry_count",
    "sent": "sent_count",
    "terminal": "terminal_count",
}
_TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "gclid",
        "fbclid",
    }
)


def deterministic_id(namespace: str, *parts: object) -> str:
    payload = "\x1f".join([namespace, *(str(part) for part in parts)])
    return f"{namespace}_{hashlib.sha256(payload.encode()).hexdigest()[:32]}"


def _sha256_json(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _canonical_url(value: str) -> str:
    parsed = urlsplit(str(value or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    query = urlencode(
        sorted(
            (key, item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            if key.lower() not in _TRACKING_PARAMS and not key.lower().startswith("utm_")
        )
    )
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, query, ""))


def _news_item_content_fingerprint(
    *,
    title: str,
    description: str,
    canonical_url: str | None,
    reporting_origin: str,
    published_at_ms: int,
    language: str,
) -> str:
    return _sha256_json(
        {
            "title": title,
            "description": description,
            "canonical_url": canonical_url,
            "reporting_origin": reporting_origin,
            "published_at_ms": published_at_ms,
            "language": language,
        }
    )


def _rss_source_item_key(
    *,
    guid: str | None,
    canonical_url: str | None,
    title: str,
    published_at_ms: int,
) -> str:
    normalized_guid = str(guid or "").strip()
    if normalized_guid:
        return f"guid:{normalized_guid}"
    if canonical_url:
        return f"url:{canonical_url}"
    return "entry:" + _sha256_json(
        {
            "title": title,
            "published_at_ms": int(published_at_ms),
        }
    )


def _numeric_provider_score(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and isfinite(float(value))


def _cursor_encode(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(dict(payload), sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _cursor_decode(value: str) -> dict[str, Any]:
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (ValueError, TypeError, json.JSONDecodeError, binascii.Error) as exc:
        raise ValueError("news_feed_cursor_invalid") from exc
    if not isinstance(decoded, dict):
        raise ValueError("news_feed_cursor_invalid")
    return decoded


def _percentile_cont_95_ms(values: Sequence[int]) -> int | None:
    """Match PostgreSQL percentile_cont(0.95), rounded up to milliseconds."""

    if not values:
        return None
    ordered = sorted(int(value) for value in values)
    scaled_position = (len(ordered) - 1) * 95
    lower, remainder = divmod(scaled_position, 100)
    upper = min(lower + 1, len(ordered) - 1)
    numerator = ordered[lower] * (100 - remainder) + ordered[upper] * remainder
    return (numerator + 99) // 100


def _incomplete_translation_sample() -> dict[str, Any]:
    return {
        "attempted": 0,
        "succeeded": 0,
        "success_ratio": None,
        "latency_p95_ms": None,
        "failure_counts": {},
        "slo_met": None,
        "sample_complete": False,
    }


def _incomplete_delivery_sample() -> dict[str, Any]:
    return {
        "completed": 0,
        "latency_p95_ms": None,
        "over_120s": 0,
        "slo_met": None,
        "sample_complete": False,
    }


class NewsRepository:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    @staticmethod
    def stable_json_hash(value: object) -> str:
        return _sha256_json(value)

    # Source inventory and acquisition -----------------------------------------

    def sync_sources(self, sources: Sequence[NewsSourceDefinition], *, now_ms: int) -> int:
        """Reconcile the complete code-owned physical source inventory."""

        source_ids = [source.source_id for source in sources]
        writes = 0
        for source in sources:
            is_rss = source.source_kind == "rss"
            cursor = self.conn.execute(
                """
                INSERT INTO news_sources (
                  source_id, name, tier, lang, enabled, source_kind,
                  feed_url, refresh_interval_seconds, next_fetch_at_ms,
                  created_at_ms, updated_at_ms
                )
                VALUES (
                  %(source_id)s, %(name)s, %(tier)s, %(lang)s,
                  %(enabled)s, %(source_kind)s, %(feed_url)s,
                  %(refresh_interval_seconds)s, %(next_fetch_at_ms)s,
                  %(now_ms)s, %(now_ms)s
                )
                ON CONFLICT (source_id) DO UPDATE SET
                  name = EXCLUDED.name,
                  tier = EXCLUDED.tier,
                  lang = EXCLUDED.lang,
                  enabled = EXCLUDED.enabled,
                  source_kind = EXCLUDED.source_kind,
                  feed_url = EXCLUDED.feed_url,
                  refresh_interval_seconds = EXCLUDED.refresh_interval_seconds,
                  next_fetch_at_ms = CASE
                    WHEN news_sources.feed_url IS DISTINCT FROM EXCLUDED.feed_url
                      OR news_sources.enabled IS DISTINCT FROM EXCLUDED.enabled
                      THEN EXCLUDED.next_fetch_at_ms
                    ELSE news_sources.next_fetch_at_ms
                  END,
                  etag = CASE
                    WHEN news_sources.feed_url IS DISTINCT FROM EXCLUDED.feed_url
                      THEN NULL
                    ELSE news_sources.etag
                  END,
                  last_modified = CASE
                    WHEN news_sources.feed_url IS DISTINCT FROM EXCLUDED.feed_url
                      THEN NULL
                    ELSE news_sources.last_modified
                  END,
                  claim_token = CASE
                    WHEN news_sources.feed_url IS DISTINCT FROM EXCLUDED.feed_url
                      THEN NULL
                    ELSE news_sources.claim_token
                  END,
                  claim_lease_expires_at_ms = CASE
                    WHEN news_sources.feed_url IS DISTINCT FROM EXCLUDED.feed_url
                      THEN NULL
                    ELSE news_sources.claim_lease_expires_at_ms
                  END,
                  updated_at_ms = EXCLUDED.updated_at_ms
                WHERE (
                  news_sources.name,
                  news_sources.tier,
                  news_sources.lang,
                  news_sources.enabled,
                  news_sources.source_kind,
                  news_sources.feed_url,
                  news_sources.refresh_interval_seconds
                ) IS DISTINCT FROM (
                  EXCLUDED.name,
                  EXCLUDED.tier,
                  EXCLUDED.lang,
                  EXCLUDED.enabled,
                  EXCLUDED.source_kind,
                  EXCLUDED.feed_url,
                  EXCLUDED.refresh_interval_seconds
                )
                """,
                {
                    "source_id": source.source_id,
                    "name": source.name,
                    "tier": source.tier,
                    "lang": source.lang,
                    "enabled": source.enabled,
                    "source_kind": source.source_kind,
                    "feed_url": source.feed_url if is_rss else None,
                    "refresh_interval_seconds": source.refresh_interval_seconds if is_rss else None,
                    "next_fetch_at_ms": int(now_ms) if is_rss else None,
                    "now_ms": int(now_ms),
                },
            )
            writes += int(cursor.rowcount or 0)
        if source_ids:
            cursor = self.conn.execute(
                """
                UPDATE news_sources
                   SET enabled = false,
                       claim_token = NULL,
                       claim_lease_expires_at_ms = NULL,
                       updated_at_ms = %s
                 WHERE enabled AND NOT (source_id = ANY(%s))
                """,
                (int(now_ms), source_ids),
            )
        else:
            cursor = self.conn.execute(
                """
                UPDATE news_sources
                   SET enabled = false,
                       claim_token = NULL,
                       claim_lease_expires_at_ms = NULL,
                       updated_at_ms = %s
                 WHERE enabled
                """,
                (int(now_ms),),
            )
        return writes + int(cursor.rowcount or 0)

    def claim_due_rss_source(
        self,
        *,
        now_ms: int,
        claim_token: str,
        lease_expires_at_ms: int,
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            WITH candidate AS (
              SELECT source_id
                FROM news_sources
               WHERE enabled
                 AND source_kind = 'rss'
                 AND next_fetch_at_ms <= %(now_ms)s
                 AND (
                   claim_token IS NULL
                   OR claim_lease_expires_at_ms <= %(now_ms)s
                 )
               ORDER BY next_fetch_at_ms, source_id
               LIMIT 1
               FOR UPDATE SKIP LOCKED
            )
            UPDATE news_sources AS source
               SET claim_token = %(claim_token)s,
                   claim_lease_expires_at_ms = %(lease_expires_at_ms)s,
                   last_fetch_started_at_ms = %(now_ms)s,
                   updated_at_ms = %(now_ms)s
              FROM candidate
             WHERE source.source_id = candidate.source_id
            RETURNING source.source_id, source.name, source.tier, source.lang,
                      source.feed_url, source.refresh_interval_seconds,
                      source.etag, source.last_modified,
                      source.claim_token::text AS claim_token,
                      source.claim_lease_expires_at_ms
            """,
            {
                "now_ms": int(now_ms),
                "claim_token": claim_token,
                "lease_expires_at_ms": int(lease_expires_at_ms),
            },
        ).fetchone()
        return dict(row) if row is not None else None

    def record_rss_fetch(
        self,
        *,
        source: NewsSourceDefinition,
        claim_token: str,
        fetch: NewsFeedFetch,
        finished_at_ms: int,
    ) -> dict[str, int] | None:
        """Publish one successful current feed snapshot behind its claim fence."""

        if source.source_kind != "rss":
            raise ValueError("rss_source_required")
        claimed = self.conn.execute(
            """
            SELECT refresh_interval_seconds
              FROM news_sources
             WHERE source_id = %s
               AND source_kind = 'rss'
               AND claim_token = %s
               AND claim_lease_expires_at_ms > %s
             FOR UPDATE
            """,
            (source.source_id, claim_token, int(finished_at_ms)),
        ).fetchone()
        if claimed is None:
            return None

        gate_counts = Counter({str(key): int(value) for key, value in fetch.gate_counts.items()})
        refresh_interval_seconds = int(claimed["refresh_interval_seconds"])
        next_fetch_at_ms = int(finished_at_ms) + refresh_interval_seconds * 1_000
        if fetch.not_modified:
            self.conn.execute(
                """
                UPDATE news_sources
                   SET etag = COALESCE(%s, etag),
                       last_modified = COALESCE(%s, last_modified),
                       last_fetch_finished_at_ms = %s,
                       last_success_at_ms = %s,
                       last_http_status = %s,
                       consecutive_failures = 0,
                       last_error = NULL,
                       last_outcome = 'not_modified',
                       last_rejection_counts = %s,
                       last_items_seen = %s,
                       last_items_accepted = 0,
                       next_fetch_at_ms = %s,
                       claim_token = NULL,
                       claim_lease_expires_at_ms = NULL,
                       updated_at_ms = %s
                 WHERE source_id = %s AND claim_token = %s
                """,
                (
                    fetch.etag,
                    fetch.last_modified,
                    int(finished_at_ms),
                    int(finished_at_ms),
                    int(fetch.status_code),
                    Jsonb(dict(gate_counts)),
                    int(fetch.entries_seen),
                    next_fetch_at_ms,
                    int(finished_at_ms),
                    source.source_id,
                    claim_token,
                ),
            )
            return {"items_inserted": 0, "items_updated": 0, "items_deactivated": 0}

        accepted: dict[str, dict[str, Any]] = {}
        cutoff_ms = int(finished_at_ms) - _ACTIVE_WINDOW_MS
        for source_position, entry in enumerate(fetch.entries):
            title = str(entry.title or "").strip()
            published_at_ms = entry.published_at_ms
            if not title or published_at_ms is None:
                raise RuntimeError("rss_parser_acceptance_invariant")
            if int(published_at_ms) < cutoff_ms:
                gate_counts["stale_age"] += 1
                continue
            canonical_url = _canonical_url(str(entry.link or "")) or None
            source_item_key = _rss_source_item_key(
                guid=entry.guid,
                canonical_url=canonical_url,
                title=title,
                published_at_ms=int(published_at_ms),
            )
            if source_item_key in accepted:
                gate_counts["duplicate_item_key"] += 1
                continue
            description = str(entry.description or "").strip()
            language = str(entry.language or source.lang).strip() or source.lang
            accepted[source_item_key] = {
                "item_id": deterministic_id("news_item", source.source_id, source_item_key),
                "source_id": source.source_id,
                "source_item_key": source_item_key,
                "source_position": source_position,
                "canonical_url": canonical_url,
                "reporting_origin": source.name,
                "title": title,
                "description": description,
                "lang": language,
                "published_at_ms": int(published_at_ms),
                "observed_at_ms": int(finished_at_ms),
                "content_fingerprint": _news_item_content_fingerprint(
                    title=title,
                    description=description,
                    canonical_url=canonical_url,
                    reporting_origin=source.name,
                    published_at_ms=int(published_at_ms),
                    language=language,
                ),
            }

        inserted = 0
        updated = 0
        for values in accepted.values():
            outcome = self.conn.execute(
                """
                INSERT INTO news_items AS current_item (
                  item_id, source_id, source_item_key, source_position, canonical_url,
                  reporting_origin, title, description, lang,
                  published_at_ms, first_observed_at_ms, last_observed_at_ms,
                  content_fingerprint, active,
                  created_at_ms, updated_at_ms
                ) VALUES (
                  %(item_id)s, %(source_id)s, %(source_item_key)s,
                  %(source_position)s,
                  %(canonical_url)s, %(reporting_origin)s, %(title)s,
                  %(description)s, %(lang)s, %(published_at_ms)s,
                  %(observed_at_ms)s, %(observed_at_ms)s,
                  %(content_fingerprint)s, true, %(observed_at_ms)s,
                  %(observed_at_ms)s
                )
                ON CONFLICT (source_id, source_item_key) DO UPDATE SET
                  source_position = EXCLUDED.source_position,
                  canonical_url = EXCLUDED.canonical_url,
                  reporting_origin = EXCLUDED.reporting_origin,
                  title = EXCLUDED.title,
                  description = EXCLUDED.description,
                  lang = EXCLUDED.lang,
                  published_at_ms = EXCLUDED.published_at_ms,
                  last_observed_at_ms = EXCLUDED.last_observed_at_ms,
                  content_fingerprint = EXCLUDED.content_fingerprint,
                  level = CASE
                    WHEN current_item.content_fingerprint
                           IS DISTINCT FROM EXCLUDED.content_fingerprint
                      THEN NULL ELSE current_item.level END,
                  category = CASE
                    WHEN current_item.content_fingerprint
                           IS DISTINCT FROM EXCLUDED.content_fingerprint
                      THEN NULL ELSE current_item.category END,
                  classification_source = CASE
                    WHEN current_item.content_fingerprint
                           IS DISTINCT FROM EXCLUDED.content_fingerprint
                      THEN NULL ELSE current_item.classification_source END,
                  classification_confidence = CASE
                    WHEN current_item.content_fingerprint
                           IS DISTINCT FROM EXCLUDED.content_fingerprint
                      THEN NULL ELSE current_item.classification_confidence END,
                  importance_score = CASE
                    WHEN current_item.content_fingerprint
                           IS DISTINCT FROM EXCLUDED.content_fingerprint
                      THEN 0 ELSE current_item.importance_score END,
                  importance_factors = CASE
                    WHEN current_item.content_fingerprint
                           IS DISTINCT FROM EXCLUDED.content_fingerprint
                      THEN '{}'::jsonb ELSE current_item.importance_factors END,
                  active = true,
                  updated_at_ms = EXCLUDED.updated_at_ms
                WHERE current_item.content_fingerprint
                        IS DISTINCT FROM EXCLUDED.content_fingerprint
                   OR current_item.source_position
                        IS DISTINCT FROM EXCLUDED.source_position
                   OR NOT current_item.active
                RETURNING (xmax = 0) AS inserted
                """,
                values,
            ).fetchone()
            if outcome is None:
                continue
            if bool(outcome["inserted"]):
                inserted += 1
            else:
                updated += 1

        accepted_item_ids = [str(value["item_id"]) for value in accepted.values()]
        if accepted_item_ids:
            deactivated = self.conn.execute(
                """
                UPDATE news_items
                   SET active = false, updated_at_ms = %s
                 WHERE source_id = %s
                   AND active
                   AND NOT (item_id = ANY(%s))
                """,
                (int(finished_at_ms), source.source_id, accepted_item_ids),
            )
        else:
            deactivated = self.conn.execute(
                """
                UPDATE news_items
                   SET active = false, updated_at_ms = %s
                 WHERE source_id = %s AND active
                """,
                (int(finished_at_ms), source.source_id),
            )

        self.conn.execute(
            """
            UPDATE news_sources
               SET etag = COALESCE(%s, etag),
                   last_modified = COALESCE(%s, last_modified),
                   last_fetch_finished_at_ms = %s,
                   last_success_at_ms = %s,
                   last_http_status = %s,
                   consecutive_failures = 0,
                   last_error = NULL,
                   last_outcome = 'success',
                   last_rejection_counts = %s,
                   last_items_seen = %s,
                   last_items_accepted = %s,
                   next_fetch_at_ms = %s,
                   claim_token = NULL,
                   claim_lease_expires_at_ms = NULL,
                   updated_at_ms = %s
             WHERE source_id = %s AND claim_token = %s
            """,
            (
                fetch.etag,
                fetch.last_modified,
                int(finished_at_ms),
                int(finished_at_ms),
                int(fetch.status_code),
                Jsonb(dict(gate_counts)),
                int(fetch.entries_seen),
                len(accepted),
                next_fetch_at_ms,
                int(finished_at_ms),
                source.source_id,
                claim_token,
            ),
        )
        return {
            "items_inserted": inserted,
            "items_updated": updated,
            "items_deactivated": int(deactivated.rowcount or 0),
        }

    def record_rss_failure(
        self,
        *,
        source_id: str,
        claim_token: str,
        finished_at_ms: int,
        error_code: str,
        status_code: int | None,
    ) -> bool:
        cursor = self.conn.execute(
            """
            UPDATE news_sources
               SET last_fetch_finished_at_ms = %s,
                   last_http_status = %s,
                   consecutive_failures = consecutive_failures + 1,
                   last_error = %s,
                   last_outcome = 'failed',
                   next_fetch_at_ms = %s + refresh_interval_seconds * 1000,
                   claim_token = NULL,
                   claim_lease_expires_at_ms = NULL,
                   updated_at_ms = %s
             WHERE source_id = %s
               AND source_kind = 'rss'
               AND claim_token = %s
            """,
            (
                int(finished_at_ms),
                status_code,
                str(error_code)[:500],
                int(finished_at_ms),
                int(finished_at_ms),
                source_id,
                claim_token,
            ),
        )
        return bool(cursor.rowcount)

    def expire_items(self, *, now_ms: int) -> int:
        cursor = self.conn.execute(
            """
            UPDATE news_items AS item
               SET active = false, updated_at_ms = %s
              FROM news_sources AS source
             WHERE source.source_id = item.source_id
               AND item.active
               AND (
                 (
                   source.source_kind = 'rss'
                   AND item.published_at_ms < %s
                 )
                 OR (
                   source.source_kind = 'opennews'
                   AND item.published_at_ms < %s
                 )
               )
            """,
            (
                int(now_ms),
                int(now_ms) - _ACTIVE_WINDOW_MS,
                int(now_ms) - _STORY_ACTIVE_WINDOW_MS,
            ),
        )
        return int(cursor.rowcount or 0)

    def opennews_last_recovery_attempt(self, *, source_id: str) -> int | None:
        row = self.conn.execute(
            """
            SELECT GREATEST(
                     COALESCE(last_fetch_started_at_ms, 0),
                     COALESCE(last_fetch_finished_at_ms, 0),
                     COALESCE(last_recovery_at_ms, 0)
                   ) AS last_attempt_at_ms
              FROM news_sources
             WHERE source_id = %s AND source_kind = 'opennews'
            """,
            (source_id,),
        ).fetchone()
        if row is None or int(row["last_attempt_at_ms"] or 0) <= 0:
            return None
        return int(row["last_attempt_at_ms"])

    def mark_opennews_recovery_attempt(self, *, source_id: str, started_at_ms: int) -> None:
        self.conn.execute(
            """
            UPDATE news_sources
               SET last_fetch_started_at_ms = %s,
                   last_outcome = 'recovery_running',
                   updated_at_ms = %s
             WHERE source_id = %s AND source_kind = 'opennews'
            """,
            (int(started_at_ms), int(started_at_ms), source_id),
        )

    def record_opennews_recovery_page(
        self,
        *,
        source: NewsSourceDefinition,
        events: Sequence[OpenNewsEvent],
        observed_at_ms: int,
        recovery_started_at_ms: int,
    ) -> dict[str, Any]:
        report_ids = [event.provider_record_id for event in events if event.observation_kind == "report"]
        stable_existing_ids: set[str] = set()
        if report_ids:
            stable_existing_ids = {
                str(row["provider_record_id"])
                for row in self.conn.execute(
                    """
                    SELECT provider_record_id
                      FROM news_items
                     WHERE source_id = %s
                       AND provider_record_id = ANY(%s)
                       AND first_observed_at_ms < %s
                    """,
                    (source.source_id, report_ids, int(recovery_started_at_ms)),
                ).fetchall()
            }
        prefix: list[OpenNewsEvent] = []
        stop_reason: str | None = None
        cutoff_ms = int(observed_at_ms) - _STORY_ACTIVE_WINDOW_MS
        for event in events:
            if event.observation_kind == "report":
                if event.provider_record_id in stable_existing_ids:
                    prefix.append(event)
                    stop_reason = "existing_provider_record"
                    break
                if (
                    event.entry is not None
                    and event.entry.published_at_ms is not None
                    and int(event.entry.published_at_ms) < cutoff_ms
                ):
                    stop_reason = "twelve_hour_cutoff"
                    break
            prefix.append(event)
        outcome = self.record_opennews_events(
            source=source,
            events=prefix,
            observed_at_ms=observed_at_ms,
            recovery_started_at_ms=recovery_started_at_ms,
        )
        return {
            **outcome,
            "events_seen": len(events),
            "overlap_complete": stop_reason is not None,
            "stop_reason": stop_reason,
        }

    def record_opennews_events(
        self,
        *,
        source: NewsSourceDefinition,
        events: Sequence[OpenNewsEvent],
        observed_at_ms: int,
        recovery_started_at_ms: int | None = None,
    ) -> dict[str, int]:
        """Upsert bounded OpenNews current facts without an audit-history lane."""

        if source.source_kind != "opennews":
            raise ValueError("opennews_source_required")
        inserted = 0
        updated = 0
        metadata_updated = 0
        rejections: Counter[str] = Counter()
        for event in events:
            if event.observation_kind == "translation":
                rejections["translation"] += 1
                continue
            incoming_metadata = {
                key: value for key, value in event.provider_metadata.items() if value not in (None, "", [], {})
            }
            if event.observation_kind == "provider_annotation":
                metadata = Jsonb(incoming_metadata)
                cursor = self.conn.execute(
                    """
                    UPDATE news_items
                       SET push_eligibility_updated_at_ms = CASE
                             WHEN (
                               %(metadata)s ? 'score'
                               AND provider_metadata -> 'score' IS DISTINCT FROM
                                   %(metadata)s -> 'score'
                             ) OR (
                               %(metadata)s ? 'coins'
                               AND provider_metadata -> 'coins' IS DISTINCT FROM
                                   %(metadata)s -> 'coins'
                             )
                               THEN %(observed_at_ms)s
                             ELSE push_eligibility_updated_at_ms
                           END,
                           provider_score_updated_at_ms = CASE
                             WHEN jsonb_typeof(%(metadata)s -> 'score') = 'number'
                              AND provider_metadata -> 'score' IS DISTINCT FROM
                                  %(metadata)s -> 'score'
                               THEN %(observed_at_ms)s
                             ELSE provider_score_updated_at_ms
                           END,
                           provider_metadata = provider_metadata || %(metadata)s,
                           last_observed_at_ms = %(observed_at_ms)s,
                           updated_at_ms = %(observed_at_ms)s
                     WHERE source_id = %(source_id)s
                       AND provider_record_id = %(provider_record_id)s
                       AND provider_metadata IS DISTINCT FROM
                           provider_metadata || %(metadata)s
                    """,
                    {
                        "metadata": metadata,
                        "observed_at_ms": int(observed_at_ms),
                        "source_id": source.source_id,
                        "provider_record_id": event.provider_record_id,
                    },
                )
                writes = int(cursor.rowcount or 0)
                metadata_updated += writes
                if writes == 0:
                    exists = self.conn.execute(
                        """
                        SELECT 1
                          FROM news_items
                         WHERE source_id = %s AND provider_record_id = %s
                        """,
                        (source.source_id, event.provider_record_id),
                    ).fetchone()
                    rejections["duplicate" if exists is not None else "annotation_before_report"] += 1
                continue

            entry = event.entry
            title = str(entry.title or "").strip() if entry is not None else ""
            canonical_url = _canonical_url(str(entry.link or "")) or None if entry is not None else None
            published_at_ms = entry.published_at_ms if entry is not None else None
            rejection = self._opennews_rejection_reason(
                title=title,
                published_at_ms=published_at_ms,
                now_ms=observed_at_ms,
            )
            if rejection is not None:
                rejections[rejection] += 1
                continue
            if entry is None or published_at_ms is None:
                raise RuntimeError("opennews_report_invariant")
            if published_at_ms < observed_at_ms - _STORY_ACTIVE_WINDOW_MS:
                rejections["stale_age"] += 1
                continue

            reporting_origin = str(entry.reporting_origin or source.name).strip().lower()
            description = str(entry.description or "").strip()
            language = entry.language or source.lang
            content_fingerprint = _news_item_content_fingerprint(
                title=title,
                description=description,
                canonical_url=canonical_url,
                reporting_origin=reporting_origin,
                published_at_ms=published_at_ms,
                language=language,
            )
            item_id = deterministic_id("news_item", source.source_id, event.provider_record_id)
            values = {
                "item_id": item_id,
                "source_id": source.source_id,
                "source_item_key": event.provider_record_id,
                "provider_record_id": event.provider_record_id,
                "provider_metadata": Jsonb(incoming_metadata),
                "provider_score_updated_at_ms": (
                    observed_at_ms if _numeric_provider_score(incoming_metadata.get("score")) else None
                ),
                "push_eligibility_updated_at_ms": (
                    observed_at_ms if {"score", "coins"}.intersection(incoming_metadata) else None
                ),
                "canonical_url": canonical_url,
                "reporting_origin": reporting_origin,
                "title": title,
                "description": description,
                "lang": language,
                "published_at_ms": published_at_ms,
                "observed_at_ms": observed_at_ms,
                "content_fingerprint": content_fingerprint,
            }
            cursor = self.conn.execute(
                """
                INSERT INTO news_items AS current_item (
                  item_id, source_id, source_item_key, provider_record_id,
                  provider_metadata, provider_score_updated_at_ms,
                  push_eligibility_updated_at_ms,
                  canonical_url, reporting_origin, title,
                  description, lang, published_at_ms,
                  first_observed_at_ms, last_observed_at_ms,
                  content_fingerprint, active, created_at_ms, updated_at_ms
                ) VALUES (
                  %(item_id)s, %(source_id)s, %(source_item_key)s,
                  %(provider_record_id)s, %(provider_metadata)s,
                  %(provider_score_updated_at_ms)s,
                  %(push_eligibility_updated_at_ms)s,
                  %(canonical_url)s, %(reporting_origin)s, %(title)s,
                  %(description)s, %(lang)s,
                  %(published_at_ms)s, %(observed_at_ms)s,
                  %(observed_at_ms)s, %(content_fingerprint)s,
                  true, %(observed_at_ms)s,
                  %(observed_at_ms)s
                )
                ON CONFLICT (source_id, provider_record_id)
                  WHERE provider_record_id IS NOT NULL
                DO UPDATE SET
                  source_item_key = EXCLUDED.source_item_key,
                  push_eligibility_updated_at_ms = CASE
                    WHEN (
                      EXCLUDED.provider_metadata ? 'score'
                      AND current_item.provider_metadata -> 'score' IS DISTINCT FROM
                          EXCLUDED.provider_metadata -> 'score'
                    ) OR (
                      EXCLUDED.provider_metadata ? 'coins'
                      AND current_item.provider_metadata -> 'coins' IS DISTINCT FROM
                          EXCLUDED.provider_metadata -> 'coins'
                    )
                      THEN EXCLUDED.push_eligibility_updated_at_ms
                    ELSE current_item.push_eligibility_updated_at_ms
                  END,
                  provider_score_updated_at_ms = CASE
                    WHEN jsonb_typeof(EXCLUDED.provider_metadata -> 'score') = 'number'
                     AND current_item.provider_metadata -> 'score' IS DISTINCT FROM
                         EXCLUDED.provider_metadata -> 'score'
                      THEN EXCLUDED.provider_score_updated_at_ms
                    ELSE current_item.provider_score_updated_at_ms
                  END,
                  provider_metadata = current_item.provider_metadata || EXCLUDED.provider_metadata,
                  canonical_url = EXCLUDED.canonical_url,
                  reporting_origin = EXCLUDED.reporting_origin,
                  title = EXCLUDED.title,
                  description = EXCLUDED.description,
                  lang = EXCLUDED.lang,
                  published_at_ms = EXCLUDED.published_at_ms,
                  last_observed_at_ms = EXCLUDED.last_observed_at_ms,
                  content_fingerprint = EXCLUDED.content_fingerprint,
                  level = CASE
                    WHEN current_item.content_fingerprint
                           IS DISTINCT FROM EXCLUDED.content_fingerprint
                      THEN NULL ELSE current_item.level END,
                  category = CASE
                    WHEN current_item.content_fingerprint
                           IS DISTINCT FROM EXCLUDED.content_fingerprint
                      THEN NULL ELSE current_item.category END,
                  classification_source = CASE
                    WHEN current_item.content_fingerprint
                           IS DISTINCT FROM EXCLUDED.content_fingerprint
                      THEN NULL ELSE current_item.classification_source END,
                  classification_confidence = CASE
                    WHEN current_item.content_fingerprint
                           IS DISTINCT FROM EXCLUDED.content_fingerprint
                      THEN NULL ELSE current_item.classification_confidence END,
                  importance_score = CASE
                    WHEN current_item.content_fingerprint
                           IS DISTINCT FROM EXCLUDED.content_fingerprint
                      THEN 0 ELSE current_item.importance_score END,
                  importance_factors = CASE
                    WHEN current_item.content_fingerprint
                           IS DISTINCT FROM EXCLUDED.content_fingerprint
                      THEN '{}'::jsonb ELSE current_item.importance_factors END,
                  active = true,
                  updated_at_ms = EXCLUDED.updated_at_ms
                WHERE current_item.content_fingerprint IS DISTINCT FROM EXCLUDED.content_fingerprint
                   OR current_item.provider_metadata IS DISTINCT FROM (
                        current_item.provider_metadata || EXCLUDED.provider_metadata
                      )
                   OR NOT current_item.active
                RETURNING (xmax = 0) AS inserted
                """,
                values,
            )
            outcome = cursor.fetchone()
            if outcome is None:
                rejections["duplicate"] += 1
                continue
            if bool(outcome["inserted"]):
                inserted += 1
            else:
                updated += 1

        if recovery_started_at_ms is None and events:
            self.conn.execute(
                """
                UPDATE news_sources
                   SET last_live_at_ms = %s,
                       last_success_at_ms = %s,
                       last_outcome = CASE
                         WHEN last_outcome IN (
                           'recovery_running', 'recovery_failed',
                           'recovery_window_exhausted'
                         ) THEN last_outcome
                         ELSE 'live_success'
                       END,
                       last_rejection_counts = %s,
                       last_items_seen = %s,
                       last_items_accepted = %s,
                       updated_at_ms = %s
                 WHERE source_id = %s
                """,
                (
                    int(observed_at_ms),
                    int(observed_at_ms),
                    Jsonb(dict(rejections)),
                    len(events),
                    inserted + updated,
                    int(observed_at_ms),
                    source.source_id,
                ),
            )
        return {
            "events_seen": len(events),
            "items_inserted": inserted,
            "items_updated": updated,
            "metadata_updated": metadata_updated,
            "rejected": sum(rejections.values()),
        }

    @staticmethod
    def _opennews_rejection_reason(
        *,
        title: str,
        published_at_ms: int | None,
        now_ms: int,
    ) -> str | None:
        if not title:
            return "missing_title"
        if published_at_ms is None:
            return "missing_date"
        if published_at_ms > now_ms + 3_600_000:
            return "future_date"
        return None

    def update_opennews_live_status(
        self,
        *,
        source_id: str,
        connected: bool,
        now_ms: int,
        error_code: str | None,
    ) -> bool:
        cursor = self.conn.execute(
            """
            UPDATE news_sources
               SET live_connected = %(connected)s,
                   last_live_at_ms = CASE
                     WHEN %(connected)s THEN %(now_ms)s
                     ELSE last_live_at_ms
                   END,
                   last_outcome = CASE
                     WHEN %(error_code)s::text IS NOT NULL THEN 'live_failed'
                     ELSE last_outcome
                   END,
                   last_error = CASE
                     WHEN %(error_code)s::text IS NOT NULL
                       THEN %(error_code)s::text
                     ELSE last_error
                   END,
                   updated_at_ms = %(now_ms)s
             WHERE source_id = %(source_id)s
               AND source_kind = 'opennews'
            """,
            {
                "connected": connected,
                "now_ms": int(now_ms),
                "error_code": str(error_code)[:500] if error_code else None,
                "source_id": source_id,
            },
        )
        return bool(cursor.rowcount)

    def complete_opennews_recovery(
        self,
        *,
        source_id: str,
        started_at_ms: int,
        finished_at_ms: int,
        window_exhausted: bool,
        items_seen: int,
        items_accepted: int,
        rejection_counts: Mapping[str, int],
    ) -> bool:
        outcome = "recovery_window_exhausted" if window_exhausted else "recovery_success"
        error_code = "opennews_recovery_window_exhausted" if window_exhausted else None
        cursor = self.conn.execute(
            """
            UPDATE news_sources
               SET last_fetch_finished_at_ms = %s,
                   last_recovery_at_ms = %s,
                   last_success_at_ms = CASE
                     WHEN %s THEN last_success_at_ms
                     ELSE %s
                   END,
                   last_http_status = 200,
                   consecutive_failures = CASE
                     WHEN %s THEN consecutive_failures + 1
                     ELSE 0
                   END,
                   last_outcome = %s,
                   last_error = %s,
                   last_rejection_counts = %s,
                   last_items_seen = %s,
                   last_items_accepted = %s,
                   updated_at_ms = %s
             WHERE source_id = %s
               AND source_kind = 'opennews'
               AND last_fetch_started_at_ms = %s
            """,
            (
                int(finished_at_ms),
                int(finished_at_ms),
                bool(window_exhausted),
                int(finished_at_ms),
                bool(window_exhausted),
                outcome,
                error_code,
                Jsonb(dict(rejection_counts)),
                int(items_seen),
                int(items_accepted),
                int(finished_at_ms),
                source_id,
                int(started_at_ms),
            ),
        )
        return bool(cursor.rowcount)

    def record_opennews_recovery_failure(
        self,
        *,
        source_id: str,
        started_at_ms: int,
        finished_at_ms: int,
        error_code: str,
        status_code: int | None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE news_sources
               SET last_fetch_started_at_ms = %s,
                   last_fetch_finished_at_ms = %s,
                   last_http_status = %s,
                   consecutive_failures = consecutive_failures + 1,
                   last_error = %s,
                   last_outcome = 'recovery_failed',
                   updated_at_ms = %s
             WHERE source_id = %s AND source_kind = 'opennews'
            """,
            (
                started_at_ms,
                finished_at_ms,
                status_code,
                str(error_code)[:500],
                finished_at_ms,
                source_id,
            ),
        )

    # Persistent Story projection ----------------------------------------------

    def load_story_projection(self, *, now_ms: int) -> dict[str, Any]:
        from .story_store import load_story_projection

        return load_story_projection(self, now_ms=now_ms)

    def publish_story_projection(
        self,
        *,
        snapshot: Any,
        projection: Mapping[str, Any],
        now_ms: int,
    ) -> dict[str, Any]:
        from .story_store import publish_story_projection

        return publish_story_projection(
            self,
            snapshot=snapshot,
            projection=projection,
            now_ms=now_ms,
        )

    def record_story_projection_failure(
        self,
        *,
        now_ms: int,
        error_code: str,
    ) -> None:
        from .story_store import record_story_projection_failure

        record_story_projection_failure(
            self,
            now_ms=now_ms,
            error_code=error_code,
        )

    def _story_invariant_counts(
        self,
        *,
        item_ids: Sequence[str] | None = None,
    ) -> dict[str, int]:
        """Check one published item snapshot while retaining global Story checks."""

        scoped_item_ids = sorted({str(item_id) for item_id in item_ids}) if item_ids is not None else None
        row = self.conn.execute(
            """
            WITH current_owners AS (
              SELECT i.item_id, count(m.story_id) AS owner_count
                FROM news_items i
                LEFT JOIN news_story_members m
                  ON m.item_id = i.item_id
               WHERE i.active
                 AND (
                   %(item_ids)s::text[] IS NULL
                   OR i.item_id = ANY(%(item_ids)s::text[])
                 )
               GROUP BY i.item_id
            ),
            story_aggregates AS (
              SELECT st.story_id,
                     st.item_count AS stored_item_count,
                     st.source_count AS stored_source_count,
                     st.first_published_at_ms AS stored_first_at_ms,
                     st.last_published_at_ms AS stored_last_at_ms,
                     st.facet_facts AS stored_facet_facts,
                     count(m.item_id) AS actual_item_count,
                     count(DISTINCT i.reporting_origin) AS actual_source_count,
                     min(i.published_at_ms) AS actual_first_at_ms,
                     max(i.published_at_ms) AS actual_last_at_ms,
                     jsonb_build_object(
                       'source_ids', coalesce(
                         jsonb_agg(
                           DISTINCT (i.source_id COLLATE "C")
                           ORDER BY (i.source_id COLLATE "C")
                         )
                           FILTER (WHERE i.source_id IS NOT NULL),
                         '[]'::jsonb
                       ),
                       'reporting_origins', coalesce(
                         jsonb_agg(
                           DISTINCT (btrim(i.reporting_origin) COLLATE "C")
                           ORDER BY (btrim(i.reporting_origin) COLLATE "C")
                         ) FILTER (
                           WHERE nullif(btrim(i.reporting_origin), '') IS NOT NULL
                         ),
                         '[]'::jsonb
                       )
                     ) AS actual_facet_facts,
                     bool_or(m.item_id = st.representative_item_id)
                       AS representative_is_member,
                     bool_or(m.item_id = st.scoring_item_id)
                       AS scoring_item_is_member
                FROM news_stories st
                LEFT JOIN news_story_members m
                  ON m.story_id = st.story_id
                LEFT JOIN news_items i ON i.item_id = m.item_id
               GROUP BY st.story_id
            ),
            invalid_stories AS (
              SELECT story_id
                FROM story_aggregates
               WHERE stored_item_count <> actual_item_count
                  OR stored_source_count <> actual_source_count
                  OR stored_first_at_ms IS DISTINCT FROM actual_first_at_ms
                  OR stored_last_at_ms IS DISTINCT FROM actual_last_at_ms
                  OR stored_facet_facts IS DISTINCT FROM actual_facet_facts
                  OR representative_is_member IS DISTINCT FROM true
                  OR scoring_item_is_member IS DISTINCT FROM true
            )
            SELECT count(*) FILTER (WHERE owner_count <> 1)
                     AS invalid_owner_count,
                   (SELECT count(*) FROM invalid_stories)
                     AS invalid_story_aggregate_count
              FROM current_owners
            """,
            {"item_ids": scoped_item_ids},
        ).fetchone()
        invalid_owners = int(row["invalid_owner_count"] or 0)
        invalid_aggregates = int(row["invalid_story_aggregate_count"] or 0)
        return {
            "invalid_owner_count": invalid_owners,
            "invalid_story_aggregate_count": invalid_aggregates,
            "total": invalid_owners + invalid_aggregates,
        }

    # Read contract ------------------------------------------------------------

    def list_feed(
        self,
        *,
        push_enabled: bool,
        now_ms: int,
        category: str | None = None,
        level: str | None = None,
        source_id: str | None = None,
        reporting_origin: str | None = None,
        provider_score_gt: float | None = None,
        q: str | None = None,
        sort: str = "importance",
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        if sort not in {"importance", "latest"}:
            raise ValueError("news_feed_sort_invalid")
        if limit < 1 or limit > 100:
            raise ValueError("news_feed_limit_invalid")
        normalized_category = str(category or "").strip().lower() or None
        normalized_level = str(level or "").strip().lower() or None
        normalized_source_id = str(source_id or "").strip() or None
        normalized_reporting_origin = str(reporting_origin or "").strip().lower() or None
        normalized_query = " ".join(str(q or "").split()).lower() or None
        if normalized_reporting_origin is not None and len(normalized_reporting_origin) > 128:
            raise ValueError("news_feed_reporting_origin_invalid")
        if normalized_query is not None and len(normalized_query) > 200:
            raise ValueError("news_feed_query_invalid")
        normalized_provider_score_gt = None
        if provider_score_gt is not None:
            if not _numeric_provider_score(provider_score_gt):
                raise ValueError("news_feed_provider_score_gt_invalid")
            normalized_provider_score_gt = float(provider_score_gt)
        filters: dict[str, str | float | None] = {
            "category": normalized_category,
            "level": normalized_level,
            "source_id": normalized_source_id,
            "reporting_origin": normalized_reporting_origin,
            "provider_score_gt": normalized_provider_score_gt,
            "q": normalized_query,
        }
        feed_cursor: tuple[int, int, str] | None = None
        if cursor:
            decoded = _cursor_decode(cursor)
            if decoded.get("v") != 1 or decoded.get("sort") != sort:
                raise ValueError("news_feed_cursor_invalid")
            cursor_filters = decoded.get("filters")
            if cursor_filters != filters:
                raise ValueError("news_feed_cursor_filter_mismatch")
            try:
                last_ms = int(decoded["last_published_at_ms"])
                score = int(decoded["importance_score"])
                story_id_value = str(decoded["story_id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("news_feed_cursor_invalid") from exc
            feed_cursor = (last_ms, score, story_id_value)
        rows_query = query_specs.feed_rows_query(
            category=normalized_category,
            level=normalized_level,
            source_id=normalized_source_id,
            reporting_origin=normalized_reporting_origin,
            provider_score_gt=normalized_provider_score_gt,
            q=normalized_query,
            sort=sort,
            limit=limit,
            cursor=feed_cursor,
        )
        rows = self.conn.execute(
            rows_query.sql,
            rows_query.params,
        ).fetchall()
        has_more = len(rows) > limit
        page = rows[:limit]
        page_story_ids = [str(row["story_id"]) for row in page]
        provider_evidence = self.story_push_contexts(story_ids=page_story_ids)
        next_cursor = None
        if has_more and page:
            last = page[-1]
            next_cursor = _cursor_encode(
                {
                    "v": 1,
                    "sort": sort,
                    "filters": filters,
                    "last_published_at_ms": int(last["last_published_at_ms"]),
                    "importance_score": int(last["importance_score"]),
                    "story_id": str(last["story_id"]),
                }
            )
        stories = []
        for row in page:
            story = _story_summary(row)
            selected = provider_evidence.get(str(row["story_id"]))
            story["provider_evidence"] = _public_provider_evidence(selected)
            story["notification"] = _public_notification(
                selected,
                push_enabled=push_enabled,
                now_ms=now_ms,
            )
            stories.append(story)
        return {
            "sort": sort,
            "filters": filters,
            "stories": stories,
            "next_cursor": next_cursor,
            "has_more": has_more,
            "facets": self._feed_facets(
                category=normalized_category,
                level=normalized_level,
                source_id=normalized_source_id,
                reporting_origin=normalized_reporting_origin,
                provider_score_gt=normalized_provider_score_gt,
                q=normalized_query,
            ),
        }

    def _feed_facets(
        self,
        *,
        category: str | None,
        level: str | None,
        source_id: str | None,
        reporting_origin: str | None,
        provider_score_gt: float | None,
        q: str | None,
    ) -> dict[str, Any]:
        facets_query = query_specs.feed_facets_query(
            category=category,
            level=level,
            source_id=source_id,
            reporting_origin=reporting_origin,
            provider_score_gt=provider_score_gt,
            q=q,
        )
        rows = self.conn.execute(facets_query.sql, facets_query.params).fetchall()
        facets: dict[str, list[dict[str, Any]]] = {
            "category": [],
            "level": [],
            "source": [],
            "reporting_origin": [],
        }
        has_more = {facet_type: False for facet_type in facets}
        for row in rows:
            facet_type = str(row["facet_type"])
            if int(row["position"]) > query_specs.PUBLIC_LIST_LIMIT:
                has_more[facet_type] = True
                continue
            facet = {
                "value": str(row["value"]),
                "count": int(row["count"]),
            }
            if facet_type in {"source", "reporting_origin"}:
                facet["label"] = str(row["label"])
            facets[facet_type].append(facet)
        return {
            "categories": facets["category"],
            "levels": facets["level"],
            "sources": facets["source"],
            "reporting_origins": facets["reporting_origin"],
            "page": {
                "categories_has_more": has_more["category"],
                "levels_has_more": has_more["level"],
                "sources_has_more": has_more["source"],
                "reporting_origins_has_more": has_more["reporting_origin"],
            },
        }

    def get_story(
        self,
        *,
        story_id: str,
        push_enabled: bool,
        now_ms: int,
        members_limit: int = 100,
        members_cursor: str | None = None,
    ) -> dict[str, Any] | None:
        if members_limit < 1 or members_limit > 100:
            raise ValueError("news_story_members_limit_invalid")
        story_query = query_specs.story_query(story_id=story_id)
        row = self.conn.execute(story_query.sql, story_query.params).fetchone()
        if row is None:
            return None
        selected = self.story_push_contexts(story_ids=(story_id,)).get(story_id)
        provider_evidence = _public_provider_evidence(selected)
        member_cursor: tuple[int, str] | None = None
        if members_cursor:
            decoded = _cursor_decode(members_cursor)
            if decoded.get("v") != 1 or decoded.get("kind") != "story_members":
                raise ValueError("news_story_members_cursor_invalid")
            if decoded.get("story_id") != story_id:
                raise ValueError("news_story_members_cursor_story_mismatch")
            try:
                published_at_ms = int(decoded["published_at_ms"])
                item_id = str(decoded["item_id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("news_story_members_cursor_invalid") from exc
            member_cursor = (published_at_ms, item_id)
        members_query = query_specs.story_members_query(
            story_id=story_id,
            limit=members_limit,
            cursor=member_cursor,
        )
        members = self.conn.execute(
            members_query.sql,
            members_query.params,
        ).fetchall()
        has_more = len(members) > members_limit
        page = members[:members_limit]
        next_cursor = None
        if has_more and page:
            last = page[-1]
            next_cursor = _cursor_encode(
                {
                    "v": 1,
                    "kind": "story_members",
                    "story_id": story_id,
                    "published_at_ms": int(last["published_at_ms"]),
                    "item_id": str(last["item_id"]),
                }
            )
        story = _story_summary(row)
        return {
            **story,
            "provider_evidence": provider_evidence,
            "notification": _public_notification(
                selected,
                push_enabled=push_enabled,
                now_ms=now_ms,
            ),
            "canonical_title": str(row["canonical_title"]),
            "members": [_item_payload(member) for member in page],
            "members_page": {
                "returned_count": len(page),
                "has_more": has_more,
                "next_cursor": next_cursor,
            },
        }

    def list_sources(
        self,
        *,
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        if limit < 1 or limit > 100:
            raise ValueError("news_sources_limit_invalid")
        source_cursor: tuple[int, int, str, str] | None = None
        if cursor:
            decoded = _cursor_decode(cursor)
            if decoded.get("v") != 2 or decoded.get("kind") != "sources":
                raise ValueError("news_sources_cursor_invalid")
            try:
                role_order = int(decoded["role_order"])
                tier = int(decoded["tier"])
                name = str(decoded["name"])
                source_id = str(decoded["source_id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("news_sources_cursor_invalid") from exc
            if role_order not in {0, 1}:
                raise ValueError("news_sources_cursor_invalid")
            source_cursor = (role_order, tier, name, source_id)
        sources_query = query_specs.sources_query(limit=limit, cursor=source_cursor)
        rows = self.conn.execute(
            sources_query.sql,
            sources_query.params,
        ).fetchall()
        has_more = len(rows) > limit
        page = rows[:limit]
        next_cursor = None
        if has_more and page:
            last = page[-1]
            next_cursor = _cursor_encode(
                {
                    "v": 2,
                    "kind": "sources",
                    "role_order": 0 if str(last["source_kind"]) == "opennews" else 1,
                    "tier": int(last["tier"]),
                    "name": str(last["name"]),
                    "source_id": str(last["source_id"]),
                }
            )
        return {
            "items": [dict(row) for row in page],
            "page": {
                "returned_count": len(page),
                "has_more": has_more,
                "next_cursor": next_cursor,
            },
        }

    # World Brief ---------------------------------------------------------------

    def peek_brief_candidate(self, *, now_ms: int) -> dict[str, Any] | None:
        return brief_store.peek_brief_candidate(self, now_ms=now_ms)

    def prepare_brief_run(
        self,
        *,
        slot_at_ms: int,
        lease_owner: str,
        lease_token: str,
        now_ms: int,
    ) -> dict[str, Any] | None:
        return brief_store.prepare_brief_run(
            self,
            slot_at_ms=slot_at_ms,
            lease_owner=lease_owner,
            lease_token=lease_token,
            now_ms=now_ms,
        )

    def start_brief_model(
        self,
        *,
        slot_at_ms: int,
        lease_owner: str,
        lease_token: str,
        now_ms: int,
    ) -> bool:
        return brief_store.start_brief_model(
            self,
            slot_at_ms=slot_at_ms,
            lease_owner=lease_owner,
            lease_token=lease_token,
            now_ms=now_ms,
        )

    def release_brief_claim(
        self,
        *,
        slot_at_ms: int,
        lease_owner: str,
        lease_token: str,
        now_ms: int,
    ) -> bool:
        return brief_store.release_brief_claim(
            self,
            slot_at_ms=slot_at_ms,
            lease_owner=lease_owner,
            lease_token=lease_token,
            now_ms=now_ms,
        )

    def publish_brief(
        self,
        *,
        claim: Mapping[str, Any],
        result: NewsBriefSynthesisResult,
        now_ms: int,
    ) -> str | None:
        return brief_store.publish_brief(
            self,
            claim=claim,
            result=result,
            now_ms=now_ms,
        )

    def get_brief(self, *, now_ms: int) -> dict[str, Any]:
        return brief_store.get_brief(self, now_ms=now_ms)

    # News Story push ----------------------------------------------------------

    def story_push_contexts(
        self,
        *,
        story_ids: Sequence[str],
    ) -> dict[str, dict[str, Any]]:
        """Return selected provider evidence and durable push state per Story.

        Selection is deterministic: numeric score descending, then newest
        publication, then item identity. Requested Story IDs remain present
        without numeric evidence so Feed/detail can still expose a durable
        Story-scoped delivery fact. Callers must provide their already-bounded
        serving scope; push reconciliation owns a separate cursor-paged read.
        """

        if not story_ids:
            return {}
        contexts_query = query_specs.story_push_contexts_query(story_ids=story_ids)
        rows = self.conn.execute(
            contexts_query.sql,
            contexts_query.params,
        ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            story_id = str(row["story_id"])
            result[story_id] = _push_context_payload(row, story_id=story_id)
        return result

    def story_push_reconcile_page(self) -> tuple[dict[str, dict[str, Any]], str | None]:
        """Return one durable cursor page of exact push-discovery evidence."""

        page_query = query_specs.story_push_reconcile_page_query()
        rows = self.conn.execute(page_query.sql, page_query.params).fetchall()
        candidates: dict[str, dict[str, Any]] = {}
        for row in rows:
            if row["item_id"] is None:
                continue
            story_id = str(row["page_story_id"])
            candidates[story_id] = _push_context_payload(row, story_id=story_id)
        next_cursor = str(rows[-1]["page_story_id"]) if rows and bool(rows[0]["has_more"]) else None
        return candidates, next_cursor

    def push_reconcile_page_schedule(
        self,
        *,
        now_ms: int,
        minimum_cycle_ms: int,
    ) -> tuple[bool, int | None]:
        """Admit one page and return a new cycle clock when the ring wraps."""

        row = self.conn.execute(
            """
            SELECT reconcile_cursor_story_id, reconcile_cycle_started_at_ms
              FROM news_push_state
             WHERE singleton_key = 'current'
            """
        ).fetchone()
        if row is None:
            raise RuntimeError("news_push_state_missing")
        if row["reconcile_cursor_story_id"] is not None:
            return True, None
        cycle_started_at_ms = row["reconcile_cycle_started_at_ms"]
        if cycle_started_at_ms is not None and int(now_ms) < (int(cycle_started_at_ms) + int(minimum_cycle_ms)):
            return False, None
        return True, int(now_ms)

    def advance_push_reconcile_cursor(
        self,
        *,
        story_id: str | None,
        cycle_started_at_ms: int | None,
        now_ms: int,
    ) -> None:
        self.conn.execute(
            """
            UPDATE news_push_state
               SET reconcile_cursor_story_id = %s,
                   reconcile_cycle_started_at_ms = coalesce(
                     %s::bigint,
                     reconcile_cycle_started_at_ms
                   ),
                   updated_at_ms = greatest(updated_at_ms, %s)
             WHERE singleton_key = 'current'
               AND (
                 reconcile_cursor_story_id IS DISTINCT FROM %s
                 OR (
                   %s::bigint IS NOT NULL
                   AND reconcile_cycle_started_at_ms IS DISTINCT FROM %s::bigint
                 )
               )
            """,
            (
                story_id,
                cycle_started_at_ms,
                int(now_ms),
                story_id,
                cycle_started_at_ms,
                cycle_started_at_ms,
            ),
        )

    def initialize_push_baseline(self, *, now_ms: int) -> tuple[int, bool]:
        row = self.conn.execute(
            """
            SELECT baseline_at_ms
              FROM news_push_state
             WHERE singleton_key = 'current'
            """
        ).fetchone()
        if row is None:
            raise RuntimeError("news_push_state_missing")
        baseline_at_ms = row["baseline_at_ms"]
        if baseline_at_ms is not None:
            return int(baseline_at_ms), False
        initialized = self.conn.execute(
            """
            UPDATE news_push_state
               SET baseline_at_ms = %s, updated_at_ms = %s
             WHERE singleton_key = 'current' AND baseline_at_ms IS NULL
            RETURNING baseline_at_ms
            """,
            (int(now_ms), int(now_ms)),
        ).fetchone()
        if initialized is not None:
            return int(initialized["baseline_at_ms"]), True
        current = self.conn.execute(
            """
            SELECT baseline_at_ms
              FROM news_push_state
             WHERE singleton_key = 'current'
            """
        ).fetchone()
        if current is None or current["baseline_at_ms"] is None:
            raise RuntimeError("news_push_state_missing")
        return int(current["baseline_at_ms"]), False

    def current_push_eligibility_evidence(
        self,
        *,
        selected_item_id: str,
    ) -> dict[str, Any] | None:
        """Return the durable selected Item's currently persisted provider fact."""

        row = self.conn.execute(
            """
            SELECT item_id, canonical_url, provider_metadata,
                   reporting_origin, title, description, lang,
                   published_at_ms,
                   push_eligibility_updated_at_ms AS eligibility_observed_at_ms,
                   coalesce(provider_score_updated_at_ms, updated_at_ms)
                     AS threshold_observed_at_ms,
                   (provider_metadata ->> 'score')::numeric AS provider_score
              FROM news_items
             WHERE item_id = %s
               AND jsonb_typeof(provider_metadata -> 'score') = 'number'
            """,
            (str(selected_item_id),),
        ).fetchone()
        if row is None:
            return None
        return {
            "item_id": str(row["item_id"]),
            "url": str(row["canonical_url"]) if row["canonical_url"] else None,
            "provider_metadata": dict(row["provider_metadata"] or {}),
            "reporting_origin": str(row["reporting_origin"]),
            "title": str(row["title"]),
            "description": str(row["description"]),
            "lang": str(row["lang"]),
            "published_at_ms": int(row["published_at_ms"]),
            "eligibility_observed_at_ms": (
                int(row["eligibility_observed_at_ms"]) if row["eligibility_observed_at_ms"] is not None else None
            ),
            "threshold_observed_at_ms": int(row["threshold_observed_at_ms"]),
            "provider_score": float(row["provider_score"]),
        }

    def _lock_push_summary(self) -> None:
        row = self.conn.execute(
            """
            SELECT singleton_key
              FROM news_push_state
             WHERE singleton_key = 'current'
             FOR UPDATE
            """
        ).fetchone()
        if row is None:
            raise RuntimeError("news_push_state_missing")

    def _apply_push_summary_change(
        self,
        *,
        previous_status_counts: Mapping[str, int] | None = None,
        current_status_counts: Mapping[str, int] | None = None,
        total_delta: int = 0,
        latest_sent_at_ms: int | None = None,
        latest_error: str | None = None,
        latest_error_at_ms: int | None = None,
        now_ms: int,
    ) -> None:
        bucket_deltas: Counter[str] = Counter()
        for status, count in (previous_status_counts or {}).items():
            bucket_deltas[_PUSH_SUMMARY_BUCKET[str(status)]] -= int(count)
        for status, count in (current_status_counts or {}).items():
            bucket_deltas[_PUSH_SUMMARY_BUCKET[str(status)]] += int(count)
        public_error = _public_push_error(latest_error) if latest_error is not None else None
        self.conn.execute(
            """
            UPDATE news_push_state
               SET total_count = total_count + %s,
                   suppressed_count = suppressed_count + %s,
                   pending_count = pending_count + %s,
                   retry_count = retry_count + %s,
                   sent_count = sent_count + %s,
                   terminal_count = terminal_count + %s,
                   latest_sent_at_ms = CASE
                     WHEN %s::bigint IS NULL THEN latest_sent_at_ms
                     ELSE greatest(coalesce(latest_sent_at_ms, 0), %s::bigint)
                   END,
                   latest_error = CASE
                     WHEN %s::bigint IS NULL THEN latest_error
                     WHEN latest_error_at_ms IS NULL
                       OR %s::bigint >= latest_error_at_ms
                       THEN %s::text
                     ELSE latest_error
                   END,
                   latest_error_at_ms = CASE
                     WHEN %s::bigint IS NULL THEN latest_error_at_ms
                     ELSE greatest(coalesce(latest_error_at_ms, 0), %s::bigint)
                   END,
                   updated_at_ms = greatest(updated_at_ms, %s)
             WHERE singleton_key = 'current'
            """,
            (
                int(total_delta),
                int(bucket_deltas["suppressed_count"]),
                int(bucket_deltas["pending_count"]),
                int(bucket_deltas["retry_count"]),
                int(bucket_deltas["sent_count"]),
                int(bucket_deltas["terminal_count"]),
                latest_sent_at_ms,
                latest_sent_at_ms,
                latest_error_at_ms,
                latest_error_at_ms,
                public_error,
                latest_error_at_ms,
                latest_error_at_ms,
                int(now_ms),
            ),
        )

    def insert_push_candidate(
        self,
        *,
        story_id: str,
        selected_item_id: str,
        provider_score: float,
        threshold_observed_at_ms: int,
        source_payload: Mapping[str, Any],
        suppressed: bool,
        now_ms: int,
    ) -> bool:
        status = "suppressed" if suppressed else "pending_translation"
        translation_status = "not_requested" if suppressed else "pending"
        self._lock_push_summary()
        row = self.conn.execute(
            """
            INSERT INTO news_push_deliveries (
              story_id, selected_item_id, provider_score,
              threshold_observed_at_ms, source_payload, delivery_payload,
              payload_fingerprint, translation_status, status,
              delivery_attempts, next_attempt_at_ms, lease_owner,
              lease_token, lease_expires_at_ms, receipt, last_error,
              sent_at_ms, created_at_ms, updated_at_ms
            ) SELECT
              %s, %s, %s, %s, %s, NULL, NULL, %s, %s,
              0, %s, NULL, NULL, NULL, NULL, NULL, NULL, %s, %s
             WHERE NOT EXISTS (
               SELECT 1
                 FROM news_push_deliveries existing
                WHERE existing.selected_item_id = %s
             )
            ON CONFLICT (story_id) DO NOTHING
            RETURNING status
            """,
            (
                story_id,
                selected_item_id,
                float(provider_score),
                int(threshold_observed_at_ms),
                Jsonb(dict(source_payload)),
                translation_status,
                status,
                None if suppressed else int(now_ms),
                int(now_ms),
                int(now_ms),
                selected_item_id,
            ),
        ).fetchone()
        if row is None:
            return False
        self._apply_push_summary_change(
            current_status_counts={str(row["status"]): 1},
            total_delta=1,
            now_ms=now_ms,
        )
        return True

    def terminalize_exhausted_push_deliveries(
        self,
        *,
        now_ms: int,
        max_attempts: int,
    ) -> int:
        self._lock_push_summary()
        rows = self.conn.execute(
            """
            WITH candidates AS MATERIALIZED (
              SELECT story_id, status AS previous_status
                FROM news_push_deliveries
               WHERE status IN ('pending_delivery', 'retry_wait')
                 AND delivery_attempts >= %s
                 AND (lease_expires_at_ms IS NULL OR lease_expires_at_ms <= %s)
               FOR UPDATE
            ), changed AS (
              UPDATE news_push_deliveries delivery
               SET status = 'terminal',
                   next_attempt_at_ms = NULL,
                   lease_owner = NULL,
                   lease_token = NULL,
                   lease_expires_at_ms = NULL,
                   last_error = COALESCE(last_error, 'delivery_attempt_limit_exhausted'),
                   updated_at_ms = %s
                FROM candidates
               WHERE delivery.story_id = candidates.story_id
              RETURNING candidates.previous_status,
                        delivery.status, delivery.story_id, delivery.last_error
            )
            SELECT previous_status, status, story_id, last_error
              FROM changed
             ORDER BY story_id
            """,
            (int(max_attempts), int(now_ms), int(now_ms)),
        ).fetchall()
        if not rows:
            return 0
        previous = Counter(str(row["previous_status"]) for row in rows)
        current = Counter(str(row["status"]) for row in rows)
        latest = rows[0]
        self._apply_push_summary_change(
            previous_status_counts=previous,
            current_status_counts=current,
            latest_error=str(latest["last_error"]),
            latest_error_at_ms=now_ms,
            now_ms=now_ms,
        )
        return len(rows)

    def release_interrupted_push_translation_claims(
        self,
        *,
        active_lease_owner: str,
        now_ms: int,
    ) -> int:
        cursor = self.conn.execute(
            """
            UPDATE news_push_deliveries
               SET next_attempt_at_ms = %s,
                   lease_owner = NULL,
                   lease_token = NULL,
                   lease_expires_at_ms = NULL
             WHERE status = 'pending_translation'
               AND translation_status = 'attempted'
               AND delivery_attempts = 0
               AND delivery_payload IS NULL
               AND payload_fingerprint IS NULL
               AND lease_owner IS NOT NULL
               AND lease_owner <> %s
            """,
            (int(now_ms), active_lease_owner),
        )
        return int(cursor.rowcount or 0)

    def peek_push_delivery(
        self,
        *,
        now_ms: int,
        max_attempts: int,
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT story_id, next_attempt_at_ms
              FROM news_push_deliveries
             WHERE status IN (
                     'pending_translation', 'pending_delivery', 'retry_wait'
                   )
               AND next_attempt_at_ms <= %s
               AND delivery_attempts < %s
               AND (lease_expires_at_ms IS NULL OR lease_expires_at_ms <= %s)
             ORDER BY next_attempt_at_ms, threshold_observed_at_ms, story_id
             LIMIT 1
            """,
            (int(now_ms), int(max_attempts), int(now_ms)),
        ).fetchone()
        return dict(row) if row is not None else None

    def claim_push_delivery(
        self,
        *,
        story_id: str,
        now_ms: int,
        max_attempts: int,
        lease_owner: str,
        lease_token: str,
        lease_expires_at_ms: int,
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            WITH claimed AS (
              UPDATE news_push_deliveries
                 SET lease_owner = %s,
                     lease_token = %s,
                     lease_expires_at_ms = %s,
                     updated_at_ms = CASE
                       WHEN translation_status = 'attempted'
                         AND delivery_payload IS NULL
                         THEN updated_at_ms
                       ELSE %s
                     END
               WHERE story_id = %s
                 AND status IN (
                       'pending_translation', 'pending_delivery', 'retry_wait'
                     )
                 AND next_attempt_at_ms <= %s
                 AND delivery_attempts < %s
                 AND (lease_expires_at_ms IS NULL OR lease_expires_at_ms <= %s)
              RETURNING *
            )
            SELECT claimed.*, state.baseline_at_ms AS push_baseline_at_ms
              FROM claimed
              LEFT JOIN news_push_state state
                ON state.singleton_key = 'current'
            """,
            (
                lease_owner,
                lease_token,
                int(lease_expires_at_ms),
                int(now_ms),
                story_id,
                int(now_ms),
                int(max_attempts),
                int(now_ms),
            ),
        ).fetchone()
        return dict(row) if row is not None else None

    def mark_push_translation_attempted(
        self,
        *,
        story_id: str,
        lease_token: str,
        attempted_at_ms: int,
    ) -> bool:
        cursor = self.conn.execute(
            """
            UPDATE news_push_deliveries
               SET translation_status = 'attempted',
                   updated_at_ms = %s
             WHERE story_id = %s
               AND lease_token = %s
               AND status = 'pending_translation'
               AND translation_status = 'pending'
               AND delivery_attempts = 0
               AND delivery_payload IS NULL
               AND payload_fingerprint IS NULL
            """,
            (int(attempted_at_ms), story_id, lease_token),
        )
        return bool(cursor.rowcount)

    def freeze_push_delivery_payload(
        self,
        *,
        story_id: str,
        lease_token: str,
        translation_status: str,
        delivery_payload: Mapping[str, Any],
        payload_fingerprint: str,
        now_ms: int,
    ) -> dict[str, Any] | None:
        if translation_status not in {"translated", "not_needed", "unavailable"}:
            raise ValueError("news_push_translation_status_invalid")
        prompt_version, attempted_at_ms, duration_ms, fallback_code = _push_translation_telemetry(delivery_payload)
        self._lock_push_summary()
        row = self.conn.execute(
            """
            UPDATE news_push_deliveries
               SET translation_status = %s,
                   delivery_payload = %s,
                   payload_fingerprint = %s,
                   translation_prompt_version = %s,
                   translation_attempted_at_ms = %s,
                   translation_duration_ms = %s,
                   translation_fallback_code = %s,
                   status = 'pending_delivery',
                   next_attempt_at_ms = %s,
                   updated_at_ms = %s
             WHERE story_id = %s
               AND lease_token = %s
               AND status = 'pending_translation'
               AND delivery_payload IS NULL
            RETURNING *
            """,
            (
                translation_status,
                Jsonb(dict(delivery_payload)),
                payload_fingerprint,
                prompt_version,
                attempted_at_ms,
                duration_ms,
                fallback_code,
                int(now_ms),
                int(now_ms),
                story_id,
                lease_token,
            ),
        ).fetchone()
        if row is None:
            return None
        self._apply_push_summary_change(
            previous_status_counts={"pending_translation": 1},
            current_status_counts={str(row["status"]): 1},
            now_ms=now_ms,
        )
        return dict(row)

    def suppress_claimed_push_delivery(
        self,
        *,
        story_id: str,
        lease_token: str,
        now_ms: int,
    ) -> bool:
        self._lock_push_summary()
        row = self.conn.execute(
            """
            UPDATE news_push_deliveries
               SET translation_status = 'not_requested',
                   status = 'suppressed',
                   next_attempt_at_ms = NULL,
                   lease_owner = NULL,
                   lease_token = NULL,
                   lease_expires_at_ms = NULL,
                   updated_at_ms = %s
             WHERE story_id = %s
               AND lease_token = %s
               AND status = 'pending_translation'
               AND delivery_attempts = 0
               AND delivery_payload IS NULL
               AND payload_fingerprint IS NULL
            RETURNING status
            """,
            (int(now_ms), story_id, lease_token),
        ).fetchone()
        if row is None:
            return False
        self._apply_push_summary_change(
            previous_status_counts={"pending_translation": 1},
            current_status_counts={str(row["status"]): 1},
            now_ms=now_ms,
        )
        return True

    def suppress_prepared_push_delivery(
        self,
        *,
        story_id: str,
        lease_token: str,
        translation_status: str,
        delivery_payload: Mapping[str, Any],
        payload_fingerprint: str,
        now_ms: int,
    ) -> bool:
        if translation_status not in {"translated", "not_needed", "unavailable"}:
            raise ValueError("news_push_translation_status_invalid")
        prompt_version, attempted_at_ms, duration_ms, fallback_code = _push_translation_telemetry(delivery_payload)
        self._lock_push_summary()
        row = self.conn.execute(
            """
            UPDATE news_push_deliveries
               SET translation_status = %s,
                   delivery_payload = %s,
                   payload_fingerprint = %s,
                   translation_prompt_version = %s,
                   translation_attempted_at_ms = %s,
                   translation_duration_ms = %s,
                   translation_fallback_code = %s,
                   status = 'suppressed',
                   next_attempt_at_ms = NULL,
                   lease_owner = NULL,
                   lease_token = NULL,
                   lease_expires_at_ms = NULL,
                   updated_at_ms = %s
             WHERE story_id = %s
               AND lease_token = %s
               AND status = 'pending_translation'
               AND translation_status = 'attempted'
               AND delivery_attempts = 0
               AND delivery_payload IS NULL
               AND payload_fingerprint IS NULL
            RETURNING status
            """,
            (
                translation_status,
                Jsonb(dict(delivery_payload)),
                payload_fingerprint,
                prompt_version,
                attempted_at_ms,
                duration_ms,
                fallback_code,
                int(now_ms),
                story_id,
                lease_token,
            ),
        ).fetchone()
        if row is None:
            return False
        self._apply_push_summary_change(
            previous_status_counts={"pending_translation": 1},
            current_status_counts={str(row["status"]): 1},
            now_ms=now_ms,
        )
        return True

    def release_push_preparation_claim(
        self,
        *,
        story_id: str,
        lease_token: str,
        now_ms: int,
    ) -> bool:
        cursor = self.conn.execute(
            """
            UPDATE news_push_deliveries
               SET next_attempt_at_ms = %s,
                   lease_owner = NULL,
                   lease_token = NULL,
                   lease_expires_at_ms = NULL,
                   updated_at_ms = CASE
                     WHEN translation_status = 'attempted'
                       THEN updated_at_ms
                     ELSE %s
                   END
             WHERE story_id = %s
               AND lease_token = %s
               AND status = 'pending_translation'
               AND translation_status IN ('pending', 'attempted')
               AND delivery_attempts = 0
               AND delivery_payload IS NULL
               AND payload_fingerprint IS NULL
            """,
            (int(now_ms), int(now_ms), story_id, lease_token),
        )
        return bool(cursor.rowcount)

    def record_push_render_failure(
        self,
        *,
        story_id: str,
        lease_token: str,
        translation_status: str,
        delivery_payload: Mapping[str, Any],
        payload_fingerprint: str,
        error_code: str,
        now_ms: int,
    ) -> bool:
        if translation_status not in {"translated", "not_needed", "unavailable"}:
            raise ValueError("news_push_translation_status_invalid")
        prompt_version, attempted_at_ms, duration_ms, fallback_code = _push_translation_telemetry(delivery_payload)
        self._lock_push_summary()
        row = self.conn.execute(
            """
            UPDATE news_push_deliveries
               SET translation_status = %s,
                   delivery_payload = %s,
                   payload_fingerprint = %s,
                   translation_prompt_version = %s,
                   translation_attempted_at_ms = %s,
                   translation_duration_ms = %s,
                   translation_fallback_code = %s,
                   status = 'terminal',
                   next_attempt_at_ms = NULL,
                   lease_owner = NULL,
                   lease_token = NULL,
                   lease_expires_at_ms = NULL,
                   last_error = %s,
                   updated_at_ms = %s
             WHERE story_id = %s
               AND lease_token = %s
               AND status = 'pending_translation'
               AND delivery_payload IS NULL
            RETURNING status, last_error
            """,
            (
                translation_status,
                Jsonb(dict(delivery_payload)),
                payload_fingerprint,
                prompt_version,
                attempted_at_ms,
                duration_ms,
                fallback_code,
                str(error_code)[:500],
                int(now_ms),
                story_id,
                lease_token,
            ),
        ).fetchone()
        if row is None:
            return False
        self._apply_push_summary_change(
            previous_status_counts={"pending_translation": 1},
            current_status_counts={str(row["status"]): 1},
            latest_error=str(row["last_error"]),
            latest_error_at_ms=now_ms,
            now_ms=now_ms,
        )
        return True

    def start_push_delivery_attempt(
        self,
        *,
        story_id: str,
        lease_token: str,
        now_ms: int,
    ) -> dict[str, Any] | None:
        self._lock_push_summary()
        row = self.conn.execute(
            """
            WITH candidate AS MATERIALIZED (
              SELECT story_id, status AS previous_status
                FROM news_push_deliveries
               WHERE story_id = %s
                 AND lease_token = %s
                 AND status IN ('pending_delivery', 'retry_wait')
                 AND delivery_payload IS NOT NULL
                 AND payload_fingerprint IS NOT NULL
               FOR UPDATE
            ), changed AS (
              UPDATE news_push_deliveries delivery
               SET status = 'pending_delivery',
                   delivery_attempts = delivery_attempts + 1,
                   updated_at_ms = %s
                FROM candidate
               WHERE delivery.story_id = candidate.story_id
              RETURNING delivery.*, candidate.previous_status
            )
            SELECT * FROM changed
            """,
            (story_id, lease_token, int(now_ms)),
        ).fetchone()
        if row is None:
            return None
        self._apply_push_summary_change(
            previous_status_counts={str(row["previous_status"]): 1},
            current_status_counts={str(row["status"]): 1},
            now_ms=now_ms,
        )
        return dict(row)

    def suppress_unsubmitted_push_delivery(
        self,
        *,
        story_id: str,
        lease_token: str,
        now_ms: int,
    ) -> bool:
        self._lock_push_summary()
        row = self.conn.execute(
            """
            UPDATE news_push_deliveries
               SET status = 'suppressed',
                   delivery_attempts = greatest(delivery_attempts - 1, 0),
                   next_attempt_at_ms = NULL,
                   lease_owner = NULL,
                   lease_token = NULL,
                   lease_expires_at_ms = NULL,
                   last_error = NULL,
                   updated_at_ms = %s
             WHERE story_id = %s
               AND lease_token = %s
               AND status = 'pending_delivery'
               AND delivery_payload IS NOT NULL
               AND payload_fingerprint IS NOT NULL
            RETURNING status
            """,
            (int(now_ms), story_id, lease_token),
        ).fetchone()
        if row is None:
            return False
        self._apply_push_summary_change(
            previous_status_counts={"pending_delivery": 1},
            current_status_counts={str(row["status"]): 1},
            now_ms=now_ms,
        )
        return True

    def complete_push_delivery(
        self,
        *,
        story_id: str,
        lease_token: str,
        receipt: Mapping[str, Any],
        now_ms: int,
    ) -> bool:
        self._lock_push_summary()
        row = self.conn.execute(
            """
            UPDATE news_push_deliveries
               SET status = 'sent',
                   next_attempt_at_ms = NULL,
                   lease_owner = NULL,
                   lease_token = NULL,
                   lease_expires_at_ms = NULL,
                   receipt = %s,
                   last_error = NULL,
                   sent_at_ms = %s,
                   updated_at_ms = %s
             WHERE story_id = %s
               AND lease_token = %s
               AND status = 'pending_delivery'
            RETURNING status, sent_at_ms
            """,
            (Jsonb(dict(receipt)), int(now_ms), int(now_ms), story_id, lease_token),
        ).fetchone()
        if row is None:
            return False
        self._apply_push_summary_change(
            previous_status_counts={"pending_delivery": 1},
            current_status_counts={str(row["status"]): 1},
            latest_sent_at_ms=int(row["sent_at_ms"]),
            now_ms=now_ms,
        )
        return True

    def fail_push_delivery(
        self,
        *,
        story_id: str,
        lease_token: str,
        error_code: str,
        retryable: bool,
        next_attempt_at_ms: int,
        max_attempts: int,
        now_ms: int,
    ) -> str | None:
        self._lock_push_summary()
        row = self.conn.execute(
            """
            UPDATE news_push_deliveries
               SET status = CASE
                     WHEN %s AND delivery_attempts < %s
                       THEN 'retry_wait'
                     ELSE 'terminal'
                   END,
                   next_attempt_at_ms = CASE
                     WHEN %s AND delivery_attempts < %s
                       THEN %s
                     ELSE NULL
                   END,
                   lease_owner = NULL,
                   lease_token = NULL,
                   lease_expires_at_ms = NULL,
                   last_error = %s,
                   updated_at_ms = %s
             WHERE story_id = %s
               AND lease_token = %s
               AND status = 'pending_delivery'
            RETURNING status, last_error
            """,
            (
                bool(retryable),
                int(max_attempts),
                bool(retryable),
                int(max_attempts),
                int(next_attempt_at_ms),
                str(error_code)[:500],
                int(now_ms),
                story_id,
                lease_token,
            ),
        ).fetchone()
        if row is None:
            return None
        self._apply_push_summary_change(
            previous_status_counts={"pending_delivery": 1},
            current_status_counts={str(row["status"]): 1},
            latest_error=str(row["last_error"]),
            latest_error_at_ms=now_ms,
            now_ms=now_ms,
        )
        return str(row["status"])

    def release_push_delivery_claim(
        self,
        *,
        story_id: str,
        lease_token: str,
        now_ms: int,
    ) -> bool:
        cursor = self.conn.execute(
            """
            UPDATE news_push_deliveries
               SET lease_owner = NULL,
                   lease_token = NULL,
                   lease_expires_at_ms = NULL,
                   delivery_attempts = greatest(delivery_attempts - 1, 0),
                   next_attempt_at_ms = %s,
                   updated_at_ms = %s
             WHERE story_id = %s
               AND lease_token = %s
               AND status = 'pending_delivery'
            """,
            (int(now_ms), int(now_ms), story_id, lease_token),
        )
        return bool(cursor.rowcount)

    def push_health_snapshot(self, *, now_ms: int) -> dict[str, Any]:
        state_query = query_specs.push_state_query()
        state = self.conn.execute(state_query.sql, state_query.params).fetchone()
        if state is None:
            raise RuntimeError("news_push_state_missing")
        oldest_due_query = query_specs.push_oldest_due_query()
        oldest_due = self.conn.execute(
            oldest_due_query.sql,
            oldest_due_query.params,
        ).fetchone()
        oldest_waiting_query = query_specs.push_oldest_waiting_query()
        oldest_waiting = self.conn.execute(
            oldest_waiting_query.sql,
            oldest_waiting_query.params,
        ).fetchone()
        baseline_at_ms = state["baseline_at_ms"]
        retry_count = int(state["retry_count"] or 0)
        terminal_count = int(state["terminal_count"] or 0)
        translation_24h = self._push_translation_24h_snapshot(now_ms=now_ms)
        delivery_24h = self._push_delivery_24h_snapshot(now_ms=now_ms)
        slo_breached = (
            translation_24h["slo_met"] is False
            or delivery_24h["slo_met"] is False
            or not translation_24h["sample_complete"]
            or not delivery_24h["sample_complete"]
        )
        status = (
            "warming"
            if baseline_at_ms is None
            else "degraded"
            if retry_count or terminal_count or slo_breached
            else "ready"
        )
        return {
            "status": status,
            "initialized": baseline_at_ms is not None,
            "baseline_at_ms": int(baseline_at_ms) if baseline_at_ms is not None else None,
            "total_count": int(state["total_count"] or 0),
            "suppressed_count": int(state["suppressed_count"] or 0),
            "pending_count": int(state["pending_count"] or 0),
            "retry_count": retry_count,
            "sent_count": int(state["sent_count"] or 0),
            "terminal_count": terminal_count,
            "oldest_due_at_ms": (oldest_due["next_attempt_at_ms"] if oldest_due is not None else None),
            "_oldest_waiting_since_at_ms": (
                oldest_waiting["threshold_observed_at_ms"] if oldest_waiting is not None else None
            ),
            "latest_sent_at_ms": state["latest_sent_at_ms"],
            "latest_error": (
                _public_push_error(str(state["latest_error"])) if state["latest_error"] is not None else None
            ),
            "latest_error_at_ms": state["latest_error_at_ms"],
            "translation_24h": translation_24h,
            "delivery_24h": delivery_24h,
            "measured_at_ms": int(now_ms),
        }

    def _push_translation_24h_snapshot(self, *, now_ms: int) -> dict[str, Any]:
        samples_query = query_specs.push_translation_samples_query(now_ms=now_ms)
        rows = self.conn.execute(samples_query.sql, samples_query.params).fetchall()
        if len(rows) > query_specs.SLO_SAMPLE_LIMIT:
            return _incomplete_translation_sample()
        attempted = len(rows)
        succeeded = sum(str(row["translation_status"]) == "translated" for row in rows)
        latency_p95_ms = _percentile_cont_95_ms(
            [int(row["duration_ms"]) for row in rows if row["duration_ms"] is not None and int(row["duration_ms"]) >= 0]
        )
        success_ratio = succeeded / attempted if attempted else None
        failure_counts: Counter[str] = Counter()
        for row in rows:
            if str(row["translation_status"]) == "translated":
                continue
            failure_code = row["fallback_code"] or "news_push_translation_unknown_failure"
            failure_counts[_public_push_error(str(failure_code))] += 1
        slo_met = (
            success_ratio >= 0.95 and latency_p95_ms is not None and latency_p95_ms <= 3_000
            if success_ratio is not None
            else None
        )
        return {
            "attempted": attempted,
            "succeeded": succeeded,
            "success_ratio": success_ratio,
            "latency_p95_ms": latency_p95_ms,
            "failure_counts": dict(sorted(failure_counts.items())),
            "slo_met": slo_met,
            "sample_complete": True,
        }

    def _push_delivery_24h_snapshot(self, *, now_ms: int) -> dict[str, Any]:
        samples_query = query_specs.push_delivery_samples_query(now_ms=now_ms)
        rows = self.conn.execute(samples_query.sql, samples_query.params).fetchall()
        if len(rows) > query_specs.SLO_SAMPLE_LIMIT:
            return _incomplete_delivery_sample()
        latencies = [
            int(row["latency_ms"]) for row in rows if row["latency_ms"] is not None and int(row["latency_ms"]) >= 0
        ]
        completed = len(latencies)
        latency_p95_ms = _percentile_cont_95_ms(latencies)
        return {
            "completed": completed,
            "latency_p95_ms": latency_p95_ms,
            "over_120s": sum(latency > _NEWS_STALL_AFTER_MS for latency in latencies),
            "slo_met": latency_p95_ms <= 90_000 if latency_p95_ms is not None else None,
            "sample_complete": True,
        }

    # Health -------------------------------------------------------------------

    def health_snapshot(
        self,
        *,
        now_ms: int,
        rss_enabled: bool,
        push_enabled: bool = False,
        feishu_webhook_url_configured: bool = False,
        feishu_signing_secret_configured: bool = False,
        workers_state: str | None = None,
        workers_reason: str | None = None,
    ) -> dict[str, Any]:
        if workers_state not in {None, "running", "recovering", "stalled"}:
            raise ValueError("news_workers_state_invalid")
        opennews_query = query_specs.status_opennews_query()
        opennews = self.conn.execute(
            opennews_query.sql,
            opennews_query.params,
        ).fetchone()
        rss_query = query_specs.status_rss_query()
        rss_rows = self.conn.execute(rss_query.sql, rss_query.params).fetchall()
        projection_query = query_specs.status_projection_query()
        story = self.conn.execute(
            projection_query.sql,
            projection_query.params,
        ).fetchone()
        brief = self.get_brief(now_ms=now_ms)
        ingest_reasons: list[str] = []
        opennews_payload = dict(opennews) if opennews is not None else None
        next_due_values = [int(row["next_fetch_at_ms"]) for row in rss_rows if row["next_fetch_at_ms"] is not None]
        success_values = [int(row["last_success_at_ms"]) for row in rss_rows if row["last_success_at_ms"] is not None]
        rss_payload = {
            "enabled": bool(rss_enabled),
            "source_count": len(rss_rows),
            "successful_source_count": sum(row["last_success_at_ms"] is not None for row in rss_rows),
            "failed_source_count": sum(row["last_error"] is not None for row in rss_rows),
            "claimed_source_count": sum(row["claim_token"] is not None for row in rss_rows),
            "next_due_at_ms": min(next_due_values, default=None),
            "latest_success_at_ms": max(success_values, default=None),
        }
        if opennews_payload is None:
            ingest_status = "degraded"
            ingest_reasons.append("opennews_primary_missing")
        elif opennews_payload["last_error"] is not None:
            ingest_status = "degraded"
            ingest_reasons.append("opennews_primary_error")
        elif opennews_payload["last_success_at_ms"] is None:
            ingest_status = "warming"
            ingest_reasons.append("opennews_primary_no_success_yet")
        elif not bool(opennews_payload["live_connected"]):
            ingest_status = "degraded"
            ingest_reasons.append("opennews_primary_disconnected")
        else:
            ingest_status = "ready"

        if rss_enabled:
            if rss_payload["source_count"] == 0:
                ingest_reasons.append("public_rss_corroboration_catalog_empty")
            elif rss_payload["successful_source_count"] == 0:
                ingest_reasons.append(
                    "public_rss_corroboration_unavailable"
                    if rss_payload["failed_source_count"]
                    else "public_rss_corroboration_warming"
                )
            elif rss_payload["failed_source_count"]:
                ingest_reasons.append("public_rss_corroboration_partial_failures")

        invalid_owners = int(story["invalid_owner_count"] or 0)
        invalid_aggregates = int(story["invalid_story_aggregate_count"] or 0)
        active_stories = int(story["active_count"] or 0)
        story_last_success_at_ms = int(story["last_success_at_ms"] or 0) or None
        story_reasons: list[str] = []
        if invalid_owners:
            story_reasons.append("current_item_owner_invalid")
        if invalid_aggregates:
            story_reasons.append("story_aggregate_invalid")
        if story["last_error"] is not None:
            story_reasons.append(str(story["last_error"]))

        runtime_stalled = workers_state == "stalled"
        runtime_recovering = workers_state == "recovering"
        if workers_reason is not None and workers_state in {"recovering", "stalled"}:
            story_reasons.append(str(workers_reason))

        if runtime_stalled or invalid_owners or invalid_aggregates or story["last_error"] is not None:
            story_status = "degraded"
        elif runtime_recovering or active_stories == 0:
            story_status = "warming"
            if active_stories == 0:
                story_reasons.append("no_active_stories_yet")
        else:
            story_status = "ready"

        brief_status = (
            "ready"
            if brief["state"] == "current"
            else "degraded"
            if brief["state"] in {"degraded", "last_known_good"}
            else "warming"
        )
        brief_reasons = [] if brief_status == "ready" else [f"public_brief_{brief['state']}"]
        push_snapshot = self.push_health_snapshot(now_ms=now_ms)
        oldest_waiting_since_at_ms = push_snapshot.pop("_oldest_waiting_since_at_ms")
        push_stalled = bool(
            push_enabled
            and oldest_waiting_since_at_ms is not None
            and int(now_ms) - int(oldest_waiting_since_at_ms) > _NEWS_STALL_AFTER_MS
        )
        translation_24h = push_snapshot["translation_24h"]
        delivery_24h = push_snapshot["delivery_24h"]
        translation_success_breached = bool(
            translation_24h["success_ratio"] is not None and float(translation_24h["success_ratio"]) < 0.95
        )
        translation_latency_breached = bool(
            translation_24h["latency_p95_ms"] is not None and int(translation_24h["latency_p95_ms"]) > 3_000
        )
        delivery_latency_breached = bool(
            delivery_24h["latency_p95_ms"] is not None and int(delivery_24h["latency_p95_ms"]) > 90_000
        )
        translation_sample_overflow = not bool(translation_24h["sample_complete"])
        delivery_sample_overflow = not bool(delivery_24h["sample_complete"])
        if push_enabled:
            push_reasons: list[str] = []
            if not feishu_webhook_url_configured:
                push_reasons.append("feishu_webhook_url_not_configured")
            if not push_snapshot["initialized"]:
                push_reasons.append("push_baseline_uninitialized")
            if push_snapshot["retry_count"]:
                push_reasons.append("push_delivery_retry_wait")
            if push_snapshot["terminal_count"]:
                push_reasons.append("push_delivery_terminal")
            if push_stalled:
                push_reasons.append("push_delivery_stalled")
            if translation_success_breached:
                push_reasons.append("push_translation_success_slo_breached")
            if translation_latency_breached:
                push_reasons.append("push_translation_latency_slo_breached")
            if delivery_latency_breached:
                push_reasons.append("push_delivery_latency_slo_breached")
            if translation_sample_overflow:
                push_reasons.append("push_translation_sample_overflow")
            if delivery_sample_overflow:
                push_reasons.append("push_delivery_sample_overflow")
            push_status = (
                "degraded"
                if (
                    not feishu_webhook_url_configured
                    or push_snapshot["retry_count"]
                    or push_snapshot["terminal_count"]
                    or push_stalled
                    or translation_success_breached
                    or translation_latency_breached
                    or delivery_latency_breached
                    or translation_sample_overflow
                    or delivery_sample_overflow
                )
                else "warming"
                if push_snapshot["pending_count"]
                else str(push_snapshot["status"])
            )
            push_payload = {
                **push_snapshot,
                "status": push_status,
                "reasons": push_reasons,
                "enabled": True,
                "feishu_webhook_url_configured": feishu_webhook_url_configured,
                "feishu_signing_secret_configured": feishu_signing_secret_configured,
            }
        else:
            push_status = "disabled"
            push_payload = {
                **push_snapshot,
                "status": push_status,
                "reasons": [],
                "enabled": False,
                "feishu_webhook_url_configured": feishu_webhook_url_configured,
                "feishu_signing_secret_configured": feishu_signing_secret_configured,
            }

        layers = {
            "ingest": {
                "status": ingest_status,
                "reasons": ingest_reasons,
                "rss": rss_payload,
                "opennews": opennews_payload,
            },
            "story": {
                "status": story_status,
                "reasons": story_reasons,
                "active_items": int(story["active_item_count"] or 0),
                "active_stories": active_stories,
                "newest_item_at_ms": story["newest_item_at_ms"],
                "newest_story_at_ms": story["newest_story_at_ms"],
                "last_material_change_at_ms": story["last_material_change_at_ms"],
                "invalid_owner_count": invalid_owners,
                "invalid_story_aggregate_count": invalid_aggregates,
                "invariant_error_count": invalid_owners + invalid_aggregates,
                "identity_version": STORY_IDENTITY_VERSION,
                "classifier_version": CLASSIFIER_VERSION,
                "importance_version": IMPORTANCE_VERSION,
                "last_attempt_at_ms": story["last_attempt_at_ms"],
                "last_success_at_ms": story_last_success_at_ms,
                "last_error": story["last_error"],
            },
            "brief": {
                "status": brief_status,
                "reasons": brief_reasons,
                "public_state": brief["state"],
                "slot_at_ms": brief["slot_at_ms"],
                "next_due_at_ms": brief["next_due_at_ms"],
                "publication_id": (brief["publication"]["publication_id"] if brief["publication"] else None),
                "latest_run": brief["latest_run"],
            },
            "push": push_payload,
        }
        statuses = [
            ingest_status,
            story_status,
            brief_status,
        ]
        if push_enabled:
            statuses.append(push_status)
        overall = "degraded" if "degraded" in statuses else "warming" if "warming" in statuses else "ready"
        operating_state = (
            "stalled"
            if runtime_stalled or push_stalled
            else "recovering"
            if overall != "ready" or (push_enabled and (push_snapshot["pending_count"] or push_snapshot["retry_count"]))
            else "live"
        )
        return {
            "status": overall,
            "operating_state": operating_state,
            "last_success_at_ms": story_last_success_at_ms,
            "reasons": [f"{name}:{reason}" for name, details in layers.items() for reason in details["reasons"]],
            "layers": layers,
            "measured_at_ms": now_ms,
        }


def _story_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "story_id": str(row["story_id"]),
        "title": str(row["representative_title"]),
        "description": str(row["representative_description"]),
        "url": str(row["representative_url"]) if row["representative_url"] else None,
        "source_id": str(row["representative_source_id"]),
        "source_name": str(row["representative_source_name"]),
        "representative_item_id": str(row["representative_item_id"]),
        "scoring_item_id": str(row["scoring_item_id"]),
        "level": str(row["level"]),
        "category": str(row["category"]),
        "importance_score": int(row["importance_score"]),
        "importance_factors": dict(row["importance_factors"]),
        "item_count": int(row["item_count"]),
        "source_count": int(row["source_count"]),
        "first_published_at_ms": int(row["first_published_at_ms"]),
        "last_published_at_ms": int(row["last_published_at_ms"]),
    }


def _push_context_payload(
    row: Mapping[str, Any],
    *,
    story_id: str,
) -> dict[str, Any]:
    selected: dict[str, Any] = {
        "story_id": story_id,
        "push_delivery_status": row["push_delivery_status"],
        "push_baseline_at_ms": (int(row["push_baseline_at_ms"]) if row["push_baseline_at_ms"] is not None else None),
        "provider_evidence": None,
    }
    if row["item_id"] is None:
        return selected
    selected.update(
        {
            "importance_score": int(row["importance_score"]),
            "item_count": int(row["item_count"]),
            "source_count": int(row["source_count"]),
            "first_published_at_ms": int(row["first_published_at_ms"]),
            "last_published_at_ms": int(row["last_published_at_ms"]),
        }
    )
    selected["provider_evidence"] = {
        "item_id": str(row["item_id"]),
        "url": str(row["canonical_url"]) if row["canonical_url"] else None,
        "provider_metadata": dict(row["provider_metadata"] or {}),
        "reporting_origin": str(row["reporting_origin"]),
        "title": str(row["title"]),
        "description": str(row["description"]),
        "lang": str(row["lang"]),
        "published_at_ms": int(row["published_at_ms"]),
        "eligibility_observed_at_ms": (
            int(row["eligibility_observed_at_ms"]) if row["eligibility_observed_at_ms"] is not None else None
        ),
        "threshold_observed_at_ms": int(row["threshold_observed_at_ms"]),
        "provider_score": float(row["provider_score"]),
    }
    return selected


def _public_provider_metadata(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    public = {key: value[key] for key in ("score", "source", "signal", "grade") if key in value}
    coins = value.get("coins")
    if isinstance(coins, list):
        assets = [
            {key: coin[key] for key in ("symbol", "market_type", "match", "score", "signal", "grade") if key in coin}
            for coin in coins
            if isinstance(coin, Mapping)
            and isinstance(coin.get("symbol"), str)
            and str(coin["symbol"]).strip()
            and isinstance(coin.get("market_type"), str)
            and str(coin["market_type"]).strip()
        ]
        if assets:
            public["assets"] = assets
    return public


def _public_provider_evidence(selected: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if selected is None:
        return None
    evidence = selected.get("provider_evidence")
    if not isinstance(evidence, Mapping):
        return None
    return {
        "item_id": str(evidence["item_id"]),
        "url": str(evidence["url"]) if evidence.get("url") else None,
        "provider_metadata": _public_provider_metadata(evidence.get("provider_metadata")),
    }


def _public_notification(
    selected: Mapping[str, Any] | None,
    *,
    push_enabled: bool,
    now_ms: int,
) -> dict[str, Any]:
    evidence = selected.get("provider_evidence") if selected is not None else None
    eligibility = evaluate_news_push_eligibility(
        evidence if isinstance(evidence, Mapping) else None,
        enabled=push_enabled,
        baseline_at_ms=(
            int(selected["push_baseline_at_ms"])
            if selected is not None and selected.get("push_baseline_at_ms") is not None
            else None
        ),
        now_ms=now_ms,
    )
    status = selected.get("push_delivery_status") if selected is not None else None
    if status in {"pending_translation", "pending_delivery", "retry_wait"}:
        delivery_state = "pending"
    elif status == "sent":
        delivery_state = "sent"
    elif status == "suppressed":
        delivery_state = "suppressed"
    elif status == "terminal":
        delivery_state = "failed"
    else:
        delivery_state = "not_created"
    return {
        "eligible": eligibility.eligible,
        "ineligible_reason": eligibility.ineligible_reason,
        "delivery_state": delivery_state,
    }


def _public_push_error(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if re.fullmatch(r"[a-z0-9_]{1,120}", normalized):
        return normalized
    return "news_story_push_delivery_error"


def _push_translation_telemetry(
    delivery_payload: Mapping[str, Any],
) -> tuple[str | None, int | None, int | None, str | None]:
    presentation = delivery_payload.get("presentation")
    if not isinstance(presentation, Mapping):
        return None, None, None, None
    prompt_version = str(presentation.get("prompt_version") or "").strip() or None

    def nonnegative_int(value: Any) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return int(value)

    attempted_at_ms = nonnegative_int(presentation.get("translation_attempted_at_ms"))
    duration_ms = nonnegative_int(presentation.get("translation_duration_ms"))
    if attempted_at_ms is None:
        duration_ms = None
    fallback_code = str(presentation.get("fallback_code") or "").strip() or None
    if prompt_version is None:
        return None, None, None, None
    return prompt_version, attempted_at_ms, duration_ms, fallback_code


def _item_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "item_id": str(row["item_id"]),
        "provider_record_id": (str(row["provider_record_id"]) if row.get("provider_record_id") is not None else None),
        "provider_metadata": _public_provider_metadata(row.get("provider_metadata")),
        "source_id": str(row["source_id"]),
        "source_name": str(row["source_name"]),
        "reporting_origin": str(row["reporting_origin"]),
        "tier": int(row["tier"]),
        "title": str(row["title"]),
        "description": str(row["description"]),
        "url": str(row["canonical_url"]) if row["canonical_url"] else None,
        "lang": str(row["lang"]),
        "published_at_ms": int(row["published_at_ms"]),
        "last_observed_at_ms": int(row["last_observed_at_ms"]),
        "level": str(row["level"]),
        "category": str(row["category"]),
        "importance_score": int(row["importance_score"]),
        "importance_factors": dict(row["importance_factors"]),
    }


__all__ = ["NewsRepository", "deterministic_id"]
