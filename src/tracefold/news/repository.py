from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from psycopg.types.json import Jsonb

from tracefold.platform.postgres.projection_frontier import (
    NEWS_FRONTIER,
    ProjectionFrontierRepository,
)

from .brief import brief_fingerprint
from .classification import SEVERITY_VALUES, classify_by_keyword
from .identity import cluster_texts, normalize_story_text
from .models import (
    BRIEF_PROMPT_VERSION,
    BRIEF_SCHEMA_VERSION,
    BRIEF_WORKFLOW_VERSION,
    CLASSIFIER_VERSION,
    IMPORTANCE_VERSION,
    NEWS_LOCALE,
    STORY_IDENTITY_VERSION,
    EventCategory,
    NewsBriefDraft,
    NewsFeedEntry,
    NewsSourceDefinition,
    ThreatLevel,
)
from .ranking import (
    diplomacy_entity_keys,
    importance_factors,
    is_delayed_brief_excluded,
    promote_diplomacy_severity,
    select_top_stories,
)

_PIPELINE_LOCK_KEY = 727_301_984
_BRIEF_LOCK_KEY = 727_301_985
_ACTIVE_WINDOW_MS = 96 * 60 * 60 * 1000
_ALIAS_TTL_MS = 7 * 24 * 60 * 60 * 1000
_SCORING_EPOCH_MS = 60 * 60 * 1000
_OPERATIONS_RETENTION_MS = 30 * 24 * 60 * 60 * 1000
_BRIEF_LEASE_MS = 120_000
_PUBLIC_LIST_LIMIT = 100
_STORY_PROJECTION_KEY = "current"
_STORY_PROJECTION_VERSION = f"{STORY_IDENTITY_VERSION}:{CLASSIFIER_VERSION}:{IMPORTANCE_VERSION}:incremental-v1"
_NEWS_PUBLIC_DEADLINE_MS = 60_000
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
_CATEGORY_ORDER: tuple[EventCategory, ...] = (
    "conflict",
    "protest",
    "disaster",
    "diplomatic",
    "economic",
    "terrorism",
    "cyber",
    "health",
    "environmental",
    "military",
    "crime",
    "infrastructure",
    "tech",
    "general",
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


def _source_item_key(entry: NewsFeedEntry, canonical_url: str) -> str:
    return str(entry.guid or "").strip() or canonical_url


def _content_fingerprint(*, title: str, description: str, canonical_url: str) -> str:
    return _sha256_json(
        {
            "title": title,
            "description": description,
            "canonical_url": canonical_url,
        }
    )


def _alias_key(normalized_title: str) -> str:
    return hashlib.sha256(normalized_title.encode()).hexdigest()


def _mode(values: Sequence[str], order: Sequence[str]) -> str:
    counts = Counter(values)
    highest = max(counts.values())
    index = {value: position for position, value in enumerate(order)}
    return min(
        (value for value, count in counts.items() if count == highest),
        key=lambda value: (index.get(value, len(index)), value),
    )


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


@dataclass(frozen=True, slots=True)
class _StoryProjectionInput:
    fingerprint: str
    cutoff_ms: int
    scoring_epoch_ms: int
    item_count: int


@dataclass(frozen=True, slots=True)
class StoryProjectionPreparation:
    input: _StoryProjectionInput
    item_ids: tuple[str, ...]
    temporary_clusters: tuple[tuple[int, ...], ...]
    current_story_count: int | None = None

    @property
    def requires_rebuild(self) -> bool:
        return self.current_story_count is None


class NewsRepository:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    # Source inventory and acquisition -----------------------------------------

    def sync_sources(self, sources: Sequence[NewsSourceDefinition], *, now_ms: int) -> None:
        previous_enabled = {
            str(row["source_id"]): bool(row["enabled"])
            for row in self.conn.execute("SELECT source_id, enabled FROM news_sources").fetchall()
        }
        source_ids = [source.source_id for source in sources]
        for source in sources:
            self.conn.execute(
                """
                INSERT INTO news_sources (
                  source_id, name, feed_url, tier, lang, enabled,
                  refresh_interval_seconds, next_fetch_at_ms,
                  created_at_ms, updated_at_ms
                )
                VALUES (
                  %(source_id)s, %(name)s, %(feed_url)s, %(tier)s, %(lang)s,
                  %(enabled)s, %(refresh_interval_seconds)s, %(now_ms)s,
                  %(now_ms)s, %(now_ms)s
                )
                ON CONFLICT (source_id) DO UPDATE SET
                  name = EXCLUDED.name,
                  feed_url = EXCLUDED.feed_url,
                  tier = EXCLUDED.tier,
                  lang = EXCLUDED.lang,
                  enabled = EXCLUDED.enabled,
                  refresh_interval_seconds = EXCLUDED.refresh_interval_seconds,
                  updated_at_ms = EXCLUDED.updated_at_ms
                WHERE (
                  news_sources.name,
                  news_sources.feed_url,
                  news_sources.tier,
                  news_sources.lang,
                  news_sources.enabled,
                  news_sources.refresh_interval_seconds
                ) IS DISTINCT FROM (
                  EXCLUDED.name,
                  EXCLUDED.feed_url,
                  EXCLUDED.tier,
                  EXCLUDED.lang,
                  EXCLUDED.enabled,
                  EXCLUDED.refresh_interval_seconds
                )
                """,
                {**source.model_dump(), "now_ms": now_ms},
            )
            self.conn.execute(
                """
                DELETE FROM news_source_memberships
                 WHERE source_id = %s AND NOT (membership = ANY(%s))
                """,
                (source.source_id, list(source.memberships)),
            )
            for membership in source.memberships:
                self.conn.execute(
                    """
                    INSERT INTO news_source_memberships(source_id, membership)
                    VALUES (%s, %s)
                    ON CONFLICT (source_id, membership) DO NOTHING
                    """,
                    (source.source_id, membership),
                )
        if source_ids:
            self.conn.execute(
                """
                UPDATE news_sources
                   SET enabled = false, updated_at_ms = %s
                 WHERE enabled AND NOT (source_id = ANY(%s))
                """,
                (now_ms, source_ids),
            )
        else:
            self.conn.execute(
                "UPDATE news_sources SET enabled = false, updated_at_ms = %s WHERE enabled",
                (now_ms,),
            )
        current_enabled = {
            str(row["source_id"]): bool(row["enabled"])
            for row in self.conn.execute("SELECT source_id, enabled FROM news_sources").fetchall()
        }
        changed_source_ids = sorted(
            source_id
            for source_id, enabled in current_enabled.items()
            if source_id in previous_enabled and previous_enabled[source_id] != enabled
        )
        if changed_source_ids:
            for row in self.conn.execute(
                """
                SELECT item.item_id, item.content_fingerprint,
                       item.published_at_ms, source.enabled
                  FROM news_items item
                  JOIN news_sources source ON source.source_id = item.source_id
                 WHERE item.source_id = ANY(%s)
                 ORDER BY item.item_id
                """,
                (changed_source_ids,),
            ).fetchall():
                self._mark_identity_dirty(
                    item_id=str(row["item_id"]),
                    content_fingerprint=str(row["content_fingerprint"]),
                    published_at_ms=int(row["published_at_ms"]),
                    active=bool(row["enabled"]) and int(row["published_at_ms"]) >= now_ms - _ACTIVE_WINDOW_MS,
                    now_ms=now_ms,
                )

    def claim_due_sources(self, *, now_ms: int, limit: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT s.*,
                   COALESCE(m.memberships, ARRAY[]::text[]) AS memberships
              FROM news_sources s
              LEFT JOIN LATERAL (
                SELECT array_agg(membership ORDER BY membership) AS memberships
                  FROM news_source_memberships
                 WHERE source_id = s.source_id
              ) m ON true
             WHERE s.enabled AND s.next_fetch_at_ms <= %s
             ORDER BY s.next_fetch_at_ms, s.source_id
             FOR UPDATE OF s SKIP LOCKED
             LIMIT %s
            """,
            (now_ms, limit),
        ).fetchall()
        claimed: list[dict[str, Any]] = []
        for row in rows:
            failures = int(row["consecutive_failures"])
            backoff_ms = min(
                3_600_000,
                int(row["refresh_interval_seconds"]) * 1000 * (2**failures),
            )
            next_fetch_at_ms = now_ms + backoff_ms
            self.conn.execute(
                """
                UPDATE news_sources
                   SET last_fetch_started_at_ms = %s,
                       next_fetch_at_ms = %s,
                       updated_at_ms = %s
                 WHERE source_id = %s
                """,
                (now_ms, next_fetch_at_ms, now_ms, row["source_id"]),
            )
            claimed_row = dict(row)
            claimed_row["last_fetch_started_at_ms"] = now_ms
            claimed_row["next_fetch_at_ms"] = next_fetch_at_ms
            claimed.append(claimed_row)
        return claimed

    def record_fetch_success(
        self,
        *,
        source: NewsSourceDefinition,
        entries: Sequence[NewsFeedEntry],
        started_at_ms: int,
        finished_at_ms: int,
        status_code: int,
        fetch_path: str,
        direct_error_code: str | None,
        etag: str | None,
        last_modified: str | None,
        not_modified: bool,
        entries_seen: int | None = None,
        gate_counts: Mapping[str, int] | None = None,
    ) -> dict[str, int]:
        fetch_id = deterministic_id("news_fetch", source.source_id, started_at_ms)
        if not_modified:
            self._insert_fetch(
                fetch_id=fetch_id,
                source_id=source.source_id,
                started_at_ms=started_at_ms,
                finished_at_ms=finished_at_ms,
                status="not_modified",
                fetch_path=fetch_path,
                direct_error_code=direct_error_code,
                http_status=status_code,
                entries_seen=0,
                observations_inserted=0,
                items_inserted=0,
                items_updated=0,
                rejection_counts={},
            )
            self._finish_source_success(
                source=source,
                finished_at_ms=finished_at_ms,
                status_code=status_code,
                etag=etag,
                last_modified=last_modified,
            )
            return {
                "entries_seen": 0,
                "observations_inserted": 0,
                "items_inserted": 0,
                "items_updated": 0,
                "projection_frontiers_written": 0,
            }

        inserted = 0
        updated = 0
        projection_frontiers_written = 0
        observations = 0
        rejection_counts: Counter[str] = Counter(
            {str(key): max(0, int(value)) for key, value in (gate_counts or {}).items() if int(value) > 0}
        )
        observed_entry_count = max(len(entries), int(entries_seen or 0))
        self._insert_fetch(
            fetch_id=fetch_id,
            source_id=source.source_id,
            started_at_ms=started_at_ms,
            finished_at_ms=finished_at_ms,
            status="success",
            fetch_path=fetch_path,
            direct_error_code=direct_error_code,
            http_status=status_code,
            entries_seen=observed_entry_count,
            observations_inserted=0,
            items_inserted=0,
            items_updated=0,
            rejection_counts={},
        )

        for position, entry in enumerate(entries):
            title = str(entry.title or "").strip()
            canonical_url = _canonical_url(str(entry.link or "")) or _canonical_url(str(entry.guid or ""))
            reporting_origin = str(entry.reporting_origin or source.name).strip().lower()
            source_item_key = _source_item_key(entry, canonical_url) or deterministic_id(
                "missing_item_key",
                source.source_id,
                position,
                title,
            )
            rejection = self._rejection_reason(
                title=title,
                canonical_url=canonical_url,
                published_at_ms=entry.published_at_ms,
                now_ms=finished_at_ms,
            )
            stale = (
                rejection is None
                and entry.published_at_ms is not None
                and entry.published_at_ms < finished_at_ms - _ACTIVE_WINDOW_MS
            )
            gate_reason = "stale_age" if stale else rejection
            observation_id = deterministic_id(
                "news_observation",
                fetch_id,
                source_item_key,
            )
            row_count = self.conn.execute(
                """
                INSERT INTO news_feed_observations (
                  observation_id, fetch_id, source_id, source_item_key,
                  observed_at_ms, title, url, published_at_ms, raw,
                  admitted, rejection_reason, created_at_ms
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (observation_id) DO NOTHING
                """,
                (
                    observation_id,
                    fetch_id,
                    source.source_id,
                    source_item_key,
                    finished_at_ms,
                    title or None,
                    canonical_url or None,
                    entry.published_at_ms,
                    Jsonb(entry.raw),
                    rejection is None,
                    gate_reason,
                    finished_at_ms,
                ),
            ).rowcount
            observations += int(row_count or 0)
            if rejection is not None:
                rejection_counts[rejection] += 1
                continue
            if stale:
                rejection_counts["stale_age"] += 1

            published_at_ms = cast(int, entry.published_at_ms)
            description = str(entry.description or "").strip()
            fingerprint = _content_fingerprint(
                title=title,
                description=description,
                canonical_url=canonical_url,
            )
            classification = classify_by_keyword(title, now_ms=finished_at_ms)
            item_id = deterministic_id("news_item", source.source_id, source_item_key)
            existing = self.conn.execute(
                "SELECT content_fingerprint FROM news_items WHERE item_id = %s",
                (item_id,),
            ).fetchone()
            if existing is None:
                self.conn.execute(
                    """
                    INSERT INTO news_items (
                      item_id, source_id, source_item_key, canonical_url,
                      reporting_origin, title, normalized_title, description,
                      lang, published_at_ms, first_observed_at_ms,
                      last_observed_at_ms, content_fingerprint, level, category,
                      classification_source, classification_confidence,
                      importance_score, importance_factors, brief_excluded,
                      active, created_at_ms, updated_at_ms
                    )
                    VALUES (
                      %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                      %s, %s, %s, %s, 0, '{}'::jsonb, %s, %s, %s, %s
                    )
                    """,
                    (
                        item_id,
                        source.source_id,
                        source_item_key,
                        canonical_url,
                        reporting_origin,
                        title,
                        normalize_story_text(title),
                        description,
                        entry.language or source.lang,
                        published_at_ms,
                        finished_at_ms,
                        finished_at_ms,
                        fingerprint,
                        classification.level,
                        classification.category,
                        classification.source,
                        classification.confidence,
                        is_delayed_brief_excluded(
                            title=title,
                            url=canonical_url,
                            description=description,
                        ),
                        not stale,
                        finished_at_ms,
                        finished_at_ms,
                    ),
                )
                inserted += 1
                projection_frontiers_written += self._mark_identity_dirty(
                    item_id=item_id,
                    content_fingerprint=fingerprint,
                    published_at_ms=published_at_ms,
                    active=not stale,
                    now_ms=finished_at_ms,
                )
            elif str(existing["content_fingerprint"]) == fingerprint:
                rejection_counts["duplicate"] += 1
            else:
                self.conn.execute(
                    """
                    UPDATE news_items
                       SET canonical_url = %s,
                           reporting_origin = %s,
                           title = %s,
                           normalized_title = %s,
                           description = %s,
                           lang = %s,
                           published_at_ms = %s,
                           last_observed_at_ms = %s,
                           content_fingerprint = %s,
                           level = %s,
                           category = %s,
                           classification_source = %s,
                           classification_confidence = %s,
                           brief_excluded = %s,
                           active = %s,
                           updated_at_ms = %s
                     WHERE item_id = %s
                    """,
                    (
                        canonical_url,
                        reporting_origin,
                        title,
                        normalize_story_text(title),
                        description,
                        entry.language or source.lang,
                        published_at_ms,
                        finished_at_ms,
                        fingerprint,
                        classification.level,
                        classification.category,
                        classification.source,
                        classification.confidence,
                        is_delayed_brief_excluded(
                            title=title,
                            url=canonical_url,
                            description=description,
                        ),
                        not stale,
                        finished_at_ms,
                        item_id,
                    ),
                )
                updated += 1
                projection_frontiers_written += self._mark_identity_dirty(
                    item_id=item_id,
                    content_fingerprint=fingerprint,
                    published_at_ms=published_at_ms,
                    active=not stale,
                    now_ms=finished_at_ms,
                )

        self.conn.execute(
            """
            UPDATE news_source_fetches
               SET observations_inserted = %s,
                   items_inserted = %s,
                   items_updated = %s,
                   rejection_counts = %s
             WHERE fetch_id = %s
            """,
            (observations, inserted, updated, Jsonb(dict(rejection_counts)), fetch_id),
        )
        self._finish_source_success(
            source=source,
            finished_at_ms=finished_at_ms,
            status_code=status_code,
            etag=etag,
            last_modified=last_modified,
        )
        return {
            "entries_seen": observed_entry_count,
            "observations_inserted": observations,
            "items_inserted": inserted,
            "items_updated": updated,
            "projection_frontiers_written": projection_frontiers_written,
        }

    def _mark_identity_dirty(
        self,
        *,
        item_id: str,
        content_fingerprint: str,
        published_at_ms: int,
        active: bool,
        now_ms: int,
    ) -> int:
        input_fingerprint = _sha256_json(
            {
                "item_id": item_id,
                "content_fingerprint": content_fingerprint,
                "published_at_ms": int(published_at_ms),
                "active": bool(active),
            }
        )
        return ProjectionFrontierRepository(self.conn).mark_dirty(
            NEWS_FRONTIER,
            key={"bucket_id": f"identity:{item_id}"},
            dirty_at_ms=now_ms,
            deadline_at_ms=now_ms + _NEWS_PUBLIC_DEADLINE_MS,
            input_fingerprint=input_fingerprint,
            version=_STORY_PROJECTION_VERSION,
            extra_insert={"active_item_count": int(active)},
        )

    def record_fetch_failure(
        self,
        *,
        source_id: str,
        started_at_ms: int,
        finished_at_ms: int,
        error: Exception,
        status_code: int | None,
        fetch_path: str | None = None,
        direct_error_code: str | None = None,
    ) -> None:
        fetch_id = deterministic_id("news_fetch", source_id, started_at_ms)
        error_code = f"{type(error).__name__}:{str(error)[:500]}"
        self._insert_fetch(
            fetch_id=fetch_id,
            source_id=source_id,
            started_at_ms=started_at_ms,
            finished_at_ms=finished_at_ms,
            status="failed",
            fetch_path=fetch_path,
            direct_error_code=direct_error_code,
            http_status=status_code,
            entries_seen=0,
            observations_inserted=0,
            items_inserted=0,
            items_updated=0,
            rejection_counts={},
            error_code=error_code,
        )
        self.conn.execute(
            """
            UPDATE news_sources
               SET last_fetch_finished_at_ms = %s,
                   last_http_status = %s,
                   consecutive_failures = consecutive_failures + 1,
                   last_error = %s,
                   updated_at_ms = %s
             WHERE source_id = %s
            """,
            (finished_at_ms, status_code, error_code, finished_at_ms, source_id),
        )

    def _insert_fetch(self, **values: Any) -> None:
        self.conn.execute(
            """
            INSERT INTO news_source_fetches (
              fetch_id, source_id, started_at_ms, finished_at_ms, status,
              fetch_path, direct_error_code, http_status, entries_seen,
              observations_inserted, items_inserted, items_updated,
              rejection_counts, error_code, created_at_ms
            )
            VALUES (
              %(fetch_id)s, %(source_id)s, %(started_at_ms)s, %(finished_at_ms)s,
              %(status)s, %(fetch_path)s, %(direct_error_code)s, %(http_status)s,
              %(entries_seen)s, %(observations_inserted)s, %(items_inserted)s,
              %(items_updated)s, %(rejection_counts)s, %(error_code)s,
              %(finished_at_ms)s
            )
            ON CONFLICT (fetch_id) DO NOTHING
            """,
            {
                **values,
                "fetch_path": values.get("fetch_path"),
                "direct_error_code": values.get("direct_error_code"),
                "error_code": values.get("error_code"),
                "rejection_counts": Jsonb(values["rejection_counts"]),
            },
        )

    def _finish_source_success(
        self,
        *,
        source: NewsSourceDefinition,
        finished_at_ms: int,
        status_code: int,
        etag: str | None,
        last_modified: str | None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE news_sources
               SET etag = %s,
                   last_modified = %s,
                   last_fetch_finished_at_ms = %s,
                   last_success_at_ms = %s,
                   last_http_status = %s,
                   consecutive_failures = 0,
                   last_error = NULL,
                   next_fetch_at_ms = %s,
                   updated_at_ms = %s
             WHERE source_id = %s
            """,
            (
                etag,
                last_modified,
                finished_at_ms,
                finished_at_ms,
                status_code,
                finished_at_ms + source.refresh_interval_seconds * 1000,
                finished_at_ms,
                source.source_id,
            ),
        )

    @staticmethod
    def _rejection_reason(
        *,
        title: str,
        canonical_url: str,
        published_at_ms: int | None,
        now_ms: int,
    ) -> str | None:
        if not title:
            return "missing_title"
        if not canonical_url:
            return "missing_http_url"
        if published_at_ms is None:
            return "missing_date"
        if published_at_ms > now_ms + 3_600_000:
            return "future_date"
        return None

    # Persistent Story projection ----------------------------------------------

    def prepare_story_projection(self, *, now_ms: int) -> StoryProjectionPreparation:
        projection_input = self._story_projection_input(now_ms=now_ms)
        projection_state = self.conn.execute(
            """
            SELECT input_fingerprint, story_count
              FROM news_story_input_state
             WHERE singleton_key = %s
            """,
            (_STORY_PROJECTION_KEY,),
        ).fetchone()
        if projection_state is not None and str(projection_state["input_fingerprint"]) == projection_input.fingerprint:
            return StoryProjectionPreparation(
                input=projection_input,
                item_ids=(),
                temporary_clusters=(),
                current_story_count=int(projection_state["story_count"]),
            )
        rows = self.conn.execute(
            """
            SELECT item.item_id, item.title
            FROM news_items item
            JOIN news_sources source ON source.source_id = item.source_id
            WHERE item.published_at_ms >= %s
              AND source.enabled
            ORDER BY item.published_at_ms, item.item_id
            """,
            (projection_input.cutoff_ms,),
        ).fetchall()
        clusters = cluster_texts([str(row["title"]) for row in rows])
        return StoryProjectionPreparation(
            input=projection_input,
            item_ids=tuple(str(row["item_id"]) for row in rows),
            temporary_clusters=tuple(tuple(int(index) for index in cluster) for cluster in clusters),
        )

    def rebuild_stories(
        self,
        *,
        now_ms: int,
        prepared: StoryProjectionPreparation | None = None,
    ) -> dict[str, Any]:
        preparation = prepared or self.prepare_story_projection(now_ms=now_ms)
        if not preparation.requires_rebuild:
            story_count = int(preparation.current_story_count or 0)
            return {
                "items": preparation.input.item_count,
                "temporary_clusters": 0,
                "stories": story_count,
                "story_writes": 0,
                "membership_writes": 0,
                "rows_written": 0,
                "added": 0,
                "archived": 0,
                "unchanged": story_count,
                "projection_status": "unchanged_input",
                "clustered": 0,
            }
        self.conn.execute("SELECT pg_advisory_xact_lock(%s)", (_PIPELINE_LOCK_KEY,))
        projection_input = self._story_projection_input(now_ms=now_ms)
        if projection_input.fingerprint != preparation.input.fingerprint:
            return {
                "items": projection_input.item_count,
                "temporary_clusters": 0,
                "stories": 0,
                "story_writes": 0,
                "membership_writes": 0,
                "rows_written": 0,
                "added": 0,
                "archived": 0,
                "unchanged": 0,
                "projection_status": "stale_snapshot",
                "clustered": 0,
            }
        active_before = {
            str(row["story_id"])
            for row in self.conn.execute("SELECT story_id FROM news_stories WHERE active").fetchall()
        }
        cutoff_ms = projection_input.cutoff_ms
        scoring_now_ms = projection_input.scoring_epoch_ms
        self.conn.execute(
            """
            UPDATE news_items AS item
               SET active = (
                 item.published_at_ms >= %s
                 AND EXISTS (
                   SELECT 1
                     FROM news_sources AS source
                    WHERE source.source_id = item.source_id
                      AND source.enabled
                 )
               )
             WHERE active IS DISTINCT FROM (
               item.published_at_ms >= %s
               AND EXISTS (
                 SELECT 1
                   FROM news_sources AS source
                  WHERE source.source_id = item.source_id
                    AND source.enabled
               )
             )
            """,
            (cutoff_ms, cutoff_ms),
        )
        items = [
            dict(row)
            for row in self.conn.execute(
                """
                SELECT i.*, s.name AS source_name, s.tier
                  FROM news_items i
                  JOIN news_sources s ON s.source_id = i.source_id
                 WHERE i.active AND s.enabled
                 ORDER BY i.published_at_ms, i.item_id
                """
            ).fetchall()
        ]
        item_ids = tuple(str(item["item_id"]) for item in items)
        if item_ids != preparation.item_ids:
            raise RuntimeError("news_story_input_snapshot_changed")
        temporary_clusters = [list(cluster) for cluster in preparation.temporary_clusters]

        previous_by_item = {
            str(row["item_id"]): str(row["story_id"])
            for row in self.conn.execute("SELECT item_id, story_id FROM news_story_members WHERE current").fetchall()
        }
        alias_by_key = {
            str(row["alias_key"]): str(row["story_id"])
            for row in self.conn.execute(
                """
                SELECT alias_key, story_id
                  FROM news_story_aliases
                 WHERE expires_at_ms > %s
                """,
                (now_ms,),
            ).fetchall()
        }

        candidate_counts_by_cluster: list[Counter[str]] = []
        parents = list(range(len(temporary_clusters)))

        def find(value: int) -> int:
            while parents[value] != value:
                parents[value] = parents[parents[value]]
                value = parents[value]
            return value

        def union(left: int, right: int) -> None:
            left_root = find(left)
            right_root = find(right)
            if left_root != right_root:
                parents[max(left_root, right_root)] = min(left_root, right_root)

        first_cluster_by_story: dict[str, int] = {}
        for cluster_index, indices in enumerate(temporary_clusters):
            candidates: Counter[str] = Counter()
            for item_index in indices:
                item = items[item_index]
                item_id = str(item["item_id"])
                if item_id in previous_by_item:
                    candidates[previous_by_item[item_id]] += 1
                story_id = alias_by_key.get(_alias_key(str(item["normalized_title"])))
                if story_id:
                    candidates[story_id] += 1
            candidate_counts_by_cluster.append(candidates)
            for story_id in candidates:
                previous_cluster = first_cluster_by_story.setdefault(story_id, cluster_index)
                union(previous_cluster, cluster_index)

        merged_indices: dict[int, list[int]] = {}
        merged_candidates: dict[int, Counter[str]] = {}
        for cluster_index, indices in enumerate(temporary_clusters):
            root = find(cluster_index)
            merged_indices.setdefault(root, []).extend(indices)
            merged_candidates.setdefault(root, Counter()).update(candidate_counts_by_cluster[cluster_index])
        clusters = [
            sorted(indices)
            for _, indices in sorted(
                merged_indices.items(),
                key=lambda pair: min(pair[1], default=-1),
            )
        ]
        candidates_for_cluster = [
            merged_candidates[root]
            for root, _ in sorted(
                merged_indices.items(),
                key=lambda pair: min(pair[1], default=-1),
            )
        ]

        cluster_by_item: dict[str, int] = {}
        for cluster_index, indices in enumerate(clusters):
            for item_index in indices:
                cluster_by_item[str(items[item_index]["item_id"])] = cluster_index
        entity_buckets: dict[str, dict[str, set[str] | set[int]]] = {}
        for item in items:
            if scoring_now_ms - int(item["published_at_ms"]) > 86_400_000:
                continue
            for entity_key in diplomacy_entity_keys(str(item["title"])):
                bucket = entity_buckets.setdefault(
                    entity_key,
                    {"clusters": set(), "sources": set(), "tier12_sources": set()},
                )
                cast(set[int], bucket["clusters"]).add(cluster_by_item[str(item["item_id"])])
                cast(set[str], bucket["sources"]).add(str(item["source_id"]))
                if int(item["tier"]) <= 2:
                    cast(set[str], bucket["tier12_sources"]).add(str(item["source_id"]))
        entity_signal_by_cluster: dict[int, tuple[int, int]] = {}
        for bucket in entity_buckets.values():
            sources = cast(set[str], bucket["sources"])
            if len(sources) < 2:
                continue
            signal = (
                len(sources),
                len(cast(set[str], bucket["tier12_sources"])),
            )
            for cluster_index in cast(set[int], bucket["clusters"]):
                previous = entity_signal_by_cluster.get(cluster_index, (0, 0))
                entity_signal_by_cluster[cluster_index] = (
                    max(previous[0], signal[0]),
                    max(previous[1], signal[1]),
                )

        story_writes = 0
        membership_writes = 0
        changed_story_ids: set[str] = set()
        current_story_ids: list[str] = []
        current_item_ids: list[str] = []
        for cluster_index, indices in enumerate(clusters):
            members = [items[index] for index in indices]
            physical_sources = {str(member["source_id"]) for member in members}
            source_count = len(physical_sources)
            entity_source_count, tier12_entity_source_count = entity_signal_by_cluster.get(cluster_index, (0, 0))
            for member in members:
                classified = classify_by_keyword(
                    str(member["title"]),
                    now_ms=scoring_now_ms,
                )
                level = promote_diplomacy_severity(
                    classified.level,
                    title=str(member["title"]),
                    tier12_origin_count=tier12_entity_source_count,
                )
                factors = importance_factors(
                    level=level,
                    tier=int(member["tier"]),
                    corroboration_count=source_count,
                    published_at_ms=int(member["published_at_ms"]),
                    now_ms=scoring_now_ms,
                    title=str(member["title"]),
                    entity_corroboration_count=entity_source_count,
                )
                member.update(
                    {
                        "level": level,
                        "category": classified.category,
                        "classification_source": classified.source,
                        "classification_confidence": classified.confidence,
                        "importance_score": int(factors["total"]),
                        "importance_factors": factors,
                    }
                )
                self.conn.execute(
                    """
                    UPDATE news_items
                       SET importance_score = %s,
                           importance_factors = %s,
                           level = %s,
                           category = %s,
                           classification_source = %s,
                           classification_confidence = %s,
                           updated_at_ms = %s
                     WHERE item_id = %s
                       AND (
                         importance_score IS DISTINCT FROM %s
                         OR importance_factors IS DISTINCT FROM %s
                         OR level IS DISTINCT FROM %s
                         OR category IS DISTINCT FROM %s
                         OR classification_source IS DISTINCT FROM %s
                         OR classification_confidence IS DISTINCT FROM %s
                       )
                    """,
                    (
                        member["importance_score"],
                        Jsonb(factors),
                        level,
                        classified.category,
                        classified.source,
                        classified.confidence,
                        now_ms,
                        member["item_id"],
                        member["importance_score"],
                        Jsonb(factors),
                        level,
                        classified.category,
                        classified.source,
                        classified.confidence,
                    ),
                )

            earliest = min(
                members,
                key=lambda member: (
                    int(member["published_at_ms"]),
                    str(member["normalized_title"]),
                    str(member["item_id"]),
                ),
            )
            canonical_key = _alias_key(str(earliest["normalized_title"]))
            candidates = candidates_for_cluster[cluster_index]
            if candidates:
                max_hits = max(candidates.values())
                story_id = min(story for story, hits in candidates.items() if hits == max_hits)
            else:
                story_id = deterministic_id("story", canonical_key)
            current_story_ids.append(story_id)

            representative = min(
                members,
                key=lambda member: (
                    int(member["tier"]),
                    -int(member["published_at_ms"]),
                    str(member["normalized_title"]),
                    str(member["item_id"]),
                ),
            )
            scoring_item = min(
                members,
                key=lambda member: (
                    -int(member["importance_score"]),
                    int(member["tier"]),
                    -int(member["published_at_ms"]),
                    str(member["source_id"]),
                    str(member["item_id"]),
                ),
            )
            level = cast(
                ThreatLevel,
                max(
                    (str(member["level"]) for member in members),
                    key=lambda value: (SEVERITY_VALUES[cast(ThreatLevel, value)], value),
                ),
            )
            category = cast(
                EventCategory,
                _mode(
                    [str(member["category"]) for member in members],
                    _CATEGORY_ORDER,
                ),
            )
            first_published_at_ms = min(int(member["published_at_ms"]) for member in members)
            last_published_at_ms = max(int(member["published_at_ms"]) for member in members)
            fingerprint = _sha256_json(
                {
                    "identity_version": STORY_IDENTITY_VERSION,
                    "canonical_key": canonical_key,
                    "representative_item_id": representative["item_id"],
                    "scoring_item_id": scoring_item["item_id"],
                    "members": sorted(str(member["item_id"]) for member in members),
                    "level": level,
                    "category": category,
                    "importance_score": scoring_item["importance_score"],
                    "importance_factors": scoring_item["importance_factors"],
                    "source_count": source_count,
                    "first": first_published_at_ms,
                    "last": last_published_at_ms,
                }
            )
            row_count = self.conn.execute(
                """
                INSERT INTO news_stories (
                  story_id, canonical_key, canonical_title,
                  representative_item_id, representative_source_id,
                  representative_title, representative_url,
                  representative_description, scoring_item_id,
                  level, category, importance_score, importance_factors,
                  item_count, source_count, first_published_at_ms,
                  last_published_at_ms, active, state_fingerprint,
                  created_at_ms, updated_at_ms
                )
                VALUES (
                  %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                  %s, %s, %s, %s, %s, true, %s, %s, %s
                )
                ON CONFLICT (story_id) DO UPDATE SET
                  canonical_key = EXCLUDED.canonical_key,
                  canonical_title = EXCLUDED.canonical_title,
                  representative_item_id = EXCLUDED.representative_item_id,
                  representative_source_id = EXCLUDED.representative_source_id,
                  representative_title = EXCLUDED.representative_title,
                  representative_url = EXCLUDED.representative_url,
                  representative_description = EXCLUDED.representative_description,
                  scoring_item_id = EXCLUDED.scoring_item_id,
                  level = EXCLUDED.level,
                  category = EXCLUDED.category,
                  importance_score = EXCLUDED.importance_score,
                  importance_factors = EXCLUDED.importance_factors,
                  item_count = EXCLUDED.item_count,
                  source_count = EXCLUDED.source_count,
                  first_published_at_ms = EXCLUDED.first_published_at_ms,
                  last_published_at_ms = EXCLUDED.last_published_at_ms,
                  active = true,
                  state_fingerprint = EXCLUDED.state_fingerprint,
                  updated_at_ms = EXCLUDED.updated_at_ms
                WHERE news_stories.state_fingerprint IS DISTINCT FROM
                      EXCLUDED.state_fingerprint
                   OR NOT news_stories.active
                """,
                (
                    story_id,
                    canonical_key,
                    str(earliest["title"]),
                    representative["item_id"],
                    representative["source_id"],
                    representative["title"],
                    representative["canonical_url"],
                    representative["description"],
                    scoring_item["item_id"],
                    level,
                    category,
                    scoring_item["importance_score"],
                    Jsonb(scoring_item["importance_factors"]),
                    len(members),
                    source_count,
                    first_published_at_ms,
                    last_published_at_ms,
                    fingerprint,
                    now_ms,
                    now_ms,
                ),
            ).rowcount
            story_writes += int(row_count or 0)
            if row_count:
                changed_story_ids.add(story_id)

            for member in members:
                item_id = str(member["item_id"])
                current_item_ids.append(item_id)
                membership_writes += int(
                    self.conn.execute(
                        """
                        UPDATE news_story_members
                           SET current = false
                         WHERE item_id = %s AND story_id <> %s AND current
                        """,
                        (item_id, story_id),
                    ).rowcount
                    or 0
                )
                membership_writes += int(
                    self.conn.execute(
                        """
                        INSERT INTO news_story_members (
                          story_id, item_id, current,
                          first_joined_at_ms, last_confirmed_at_ms
                        )
                        VALUES (%s, %s, true, %s, %s)
                        ON CONFLICT (story_id, item_id) DO UPDATE SET
                          current = true,
                          last_confirmed_at_ms = EXCLUDED.last_confirmed_at_ms
                        WHERE NOT news_story_members.current
                        """,
                        (story_id, item_id, now_ms, now_ms),
                    ).rowcount
                    or 0
                )
                self.conn.execute(
                    """
                    INSERT INTO news_story_aliases (
                      alias_key, story_id, expires_at_ms, created_at_ms
                    )
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (alias_key) DO UPDATE SET
                      story_id = EXCLUDED.story_id,
                      expires_at_ms = EXCLUDED.expires_at_ms
                    WHERE news_story_aliases.story_id IS DISTINCT FROM
                          EXCLUDED.story_id
                       OR news_story_aliases.expires_at_ms < %s
                    """,
                    (
                        _alias_key(str(member["normalized_title"])),
                        story_id,
                        now_ms + _ALIAS_TTL_MS,
                        now_ms,
                        now_ms + _ALIAS_TTL_MS - _SCORING_EPOCH_MS,
                    ),
                )

        unique_story_ids = sorted(set(current_story_ids))
        if unique_story_ids:
            story_writes += int(
                self.conn.execute(
                    """
                    UPDATE news_stories
                       SET active = false, updated_at_ms = %s
                     WHERE active AND NOT (story_id = ANY(%s))
                    """,
                    (now_ms, unique_story_ids),
                ).rowcount
                or 0
            )
        else:
            story_writes += int(
                self.conn.execute(
                    """
                    UPDATE news_stories
                       SET active = false, updated_at_ms = %s
                     WHERE active
                    """,
                    (now_ms,),
                ).rowcount
                or 0
            )
        if current_item_ids:
            membership_writes += int(
                self.conn.execute(
                    """
                    UPDATE news_story_members
                       SET current = false
                     WHERE current AND NOT (item_id = ANY(%s))
                    """,
                    (current_item_ids,),
                ).rowcount
                or 0
            )
        else:
            membership_writes += int(
                self.conn.execute("UPDATE news_story_members SET current = false WHERE current").rowcount or 0
            )
        invariant_counts = self._story_invariant_counts()
        if invariant_counts["total"] > 0:
            raise RuntimeError(
                "news_story_invariant_failed:" + json.dumps(invariant_counts, sort_keys=True, separators=(",", ":"))
            )
        self.conn.execute(
            "DELETE FROM news_story_aliases WHERE expires_at_ms <= %s",
            (now_ms,),
        )
        self.conn.execute(
            "DELETE FROM news_source_fetches WHERE created_at_ms < %s",
            (now_ms - _OPERATIONS_RETENTION_MS,),
        )
        self.conn.execute(
            """
            DELETE FROM news_brief_runs
             WHERE updated_at_ms < %s
               AND (
                 status IN ('failed', 'insufficient_material')
                 OR (
                   status = 'running'
                   AND lease_expires_at_ms IS NOT NULL
                   AND lease_expires_at_ms < %s
                 )
               )
            """,
            (
                now_ms - _OPERATIONS_RETENTION_MS,
                now_ms - _OPERATIONS_RETENTION_MS,
            ),
        )
        current_story_id_set = set(unique_story_ids)
        self.conn.execute(
            """
            INSERT INTO news_story_input_state (
              singleton_key, input_fingerprint, scoring_epoch_ms,
              item_count, temporary_cluster_count, story_count, projected_at_ms
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (singleton_key) DO UPDATE SET
              input_fingerprint = EXCLUDED.input_fingerprint,
              scoring_epoch_ms = EXCLUDED.scoring_epoch_ms,
              item_count = EXCLUDED.item_count,
              temporary_cluster_count = EXCLUDED.temporary_cluster_count,
              story_count = EXCLUDED.story_count,
              projected_at_ms = EXCLUDED.projected_at_ms
            """,
            (
                _STORY_PROJECTION_KEY,
                projection_input.fingerprint,
                projection_input.scoring_epoch_ms,
                len(items),
                len(temporary_clusters),
                len(current_story_id_set),
                now_ms,
            ),
        )
        self.refresh_projection_summary_for_maintenance(now_ms=now_ms)
        return {
            "items": len(items),
            "temporary_clusters": len(temporary_clusters),
            "stories": len(current_story_id_set),
            "story_writes": story_writes,
            "membership_writes": membership_writes,
            "rows_written": story_writes + membership_writes,
            "added": len(current_story_id_set - active_before),
            "archived": len(active_before - current_story_id_set),
            "unchanged": len((current_story_id_set & active_before) - changed_story_ids),
            "projection_status": "rebuilt",
            "clustered": len(items),
        }

    def _story_projection_input(self, *, now_ms: int) -> _StoryProjectionInput:
        cutoff_ms = int(now_ms) - _ACTIVE_WINDOW_MS
        scoring_epoch_ms = int(now_ms) - (int(now_ms) % _SCORING_EPOCH_MS)
        rows = self.conn.execute(
            """
            SELECT
              item.item_id,
              item.source_id,
              item.content_fingerprint,
              item.published_at_ms,
              source.tier
            FROM news_items item
            JOIN news_sources source ON source.source_id = item.source_id
            WHERE item.published_at_ms >= %s
              AND source.enabled
            ORDER BY item.published_at_ms, item.item_id
            """,
            (cutoff_ms,),
        ).fetchall()
        fingerprint = _sha256_json(
            {
                "projection_version": _STORY_PROJECTION_VERSION,
                "scoring_epoch_ms": scoring_epoch_ms,
                "items": [
                    [
                        str(row["item_id"]),
                        str(row["source_id"]),
                        str(row["content_fingerprint"]),
                        int(row["published_at_ms"]),
                        int(row["tier"]),
                    ]
                    for row in rows
                ],
            }
        )
        return _StoryProjectionInput(
            fingerprint=fingerprint,
            cutoff_ms=cutoff_ms,
            scoring_epoch_ms=scoring_epoch_ms,
            item_count=len(rows),
        )

    def _story_invariant_counts(self) -> dict[str, int]:
        row = self.conn.execute(
            """
            WITH current_owners AS (
              SELECT i.item_id, count(m.story_id) AS owner_count
                FROM news_items i
                LEFT JOIN news_story_members m
                  ON m.item_id = i.item_id AND m.current
               WHERE i.active
               GROUP BY i.item_id
            ),
            story_aggregates AS (
              SELECT st.story_id,
                     st.item_count AS stored_item_count,
                     st.source_count AS stored_source_count,
                     st.first_published_at_ms AS stored_first_at_ms,
                     st.last_published_at_ms AS stored_last_at_ms,
                     count(m.item_id) AS actual_item_count,
                     count(DISTINCT i.source_id) AS actual_source_count,
                     min(i.published_at_ms) AS actual_first_at_ms,
                     max(i.published_at_ms) AS actual_last_at_ms,
                     bool_or(m.item_id = st.representative_item_id)
                       AS representative_is_member,
                     bool_or(m.item_id = st.scoring_item_id)
                       AS scoring_item_is_member
                FROM news_stories st
                LEFT JOIN news_story_members m
                  ON m.story_id = st.story_id AND m.current
                LEFT JOIN news_items i ON i.item_id = m.item_id
               WHERE st.active
               GROUP BY st.story_id
            ),
            invalid_stories AS (
              SELECT story_id
                FROM story_aggregates
               WHERE stored_item_count <> actual_item_count
                  OR stored_source_count <> actual_source_count
                  OR stored_first_at_ms IS DISTINCT FROM actual_first_at_ms
                  OR stored_last_at_ms IS DISTINCT FROM actual_last_at_ms
                  OR representative_is_member IS DISTINCT FROM true
                  OR scoring_item_is_member IS DISTINCT FROM true
            )
            SELECT count(*) FILTER (WHERE owner_count <> 1)
                     AS invalid_owner_count,
                   (SELECT count(*) FROM invalid_stories)
                     AS invalid_story_aggregate_count
              FROM current_owners
            """
        ).fetchone()
        invalid_owners = int(row["invalid_owner_count"] or 0)
        invalid_aggregates = int(row["invalid_story_aggregate_count"] or 0)
        return {
            "invalid_owner_count": invalid_owners,
            "invalid_story_aggregate_count": invalid_aggregates,
            "total": invalid_owners + invalid_aggregates,
        }

    def refresh_projection_summary_for_maintenance(self, *, now_ms: int) -> None:
        invariants = self._story_invariant_counts()
        self.conn.execute(
            """
            UPDATE news_projection_summary
               SET active_item_count = (
                     SELECT count(*) FROM news_items WHERE active
                   ),
                   active_story_count = (
                     SELECT count(*) FROM news_stories WHERE active
                   ),
                   unmaterialized_item_count = (
                     SELECT count(*)
                     FROM news_items item
                     WHERE item.active
                       AND NOT EXISTS (
                         SELECT 1
                         FROM news_story_members member
                         WHERE member.item_id = item.item_id
                           AND member.current
                       )
                   ),
                   invalid_owner_count = %s,
                   invalid_story_aggregate_count = %s,
                   newest_item_at_ms = (
                     SELECT published_at_ms
                     FROM news_items
                     WHERE active
                     ORDER BY published_at_ms DESC, item_id
                     LIMIT 1
                   ),
                   newest_story_at_ms = (
                     SELECT last_published_at_ms
                     FROM news_stories
                     WHERE active
                     ORDER BY last_published_at_ms DESC,
                              importance_score DESC,
                              story_id
                     LIMIT 1
                   ),
                   last_material_change_at_ms = (
                     SELECT max(updated_at_ms)
                     FROM news_stories
                     WHERE active
                   ),
                   updated_at_ms = %s
             WHERE singleton_key = 'current'
            """,
            (
                invariants["invalid_owner_count"],
                invariants["invalid_story_aggregate_count"],
                int(now_ms),
            ),
        )

    # Read contract ------------------------------------------------------------

    def list_feed(
        self,
        *,
        category: str | None = None,
        level: str | None = None,
        source_id: str | None = None,
        sort: str = "importance",
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        if sort not in {"importance", "latest"}:
            raise ValueError("news_feed_sort_invalid")
        if limit < 1 or limit > 100:
            raise ValueError("news_feed_limit_invalid")
        filters = {
            "category": str(category or "").strip().lower() or None,
            "level": str(level or "").strip().lower() or None,
            "source_id": str(source_id or "").strip() or None,
        }
        where = ["st.active"]
        params: list[Any] = []
        if filters["category"]:
            where.append("st.category = %s")
            params.append(filters["category"])
        if filters["level"]:
            where.append("st.level = %s")
            params.append(filters["level"])
        if filters["source_id"]:
            where.append(
                """
                EXISTS (
                  SELECT 1
                    FROM news_story_members fm
                    JOIN news_items fi ON fi.item_id = fm.item_id
                   WHERE fm.story_id = st.story_id
                     AND fm.current
                     AND fi.source_id = %s
                )
                """
            )
            params.append(filters["source_id"])

        if cursor:
            decoded = _cursor_decode(cursor)
            if decoded.get("v") != 1 or decoded.get("sort") != sort:
                raise ValueError("news_feed_cursor_invalid")
            if decoded.get("filters") != filters:
                raise ValueError("news_feed_cursor_filter_mismatch")
            try:
                last_ms = int(decoded["last_published_at_ms"])
                score = int(decoded["importance_score"])
                story_id_value = str(decoded["story_id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("news_feed_cursor_invalid") from exc
            if sort == "latest":
                where.append(
                    """
                    (
                      st.last_published_at_ms < %s
                      OR (
                        st.last_published_at_ms = %s
                        AND st.importance_score < %s
                      )
                      OR (
                        st.last_published_at_ms = %s
                        AND st.importance_score = %s
                        AND st.story_id > %s
                      )
                    )
                    """
                )
                params.extend([last_ms, last_ms, score, last_ms, score, story_id_value])
            else:
                where.append(
                    """
                    (
                      st.importance_score < %s
                      OR (
                        st.importance_score = %s
                        AND st.last_published_at_ms < %s
                      )
                      OR (
                        st.importance_score = %s
                        AND st.last_published_at_ms = %s
                        AND st.story_id > %s
                      )
                    )
                    """
                )
                params.extend([score, score, last_ms, score, last_ms, story_id_value])
        order = (
            "st.last_published_at_ms DESC, st.importance_score DESC, st.story_id"
            if sort == "latest"
            else "st.importance_score DESC, st.last_published_at_ms DESC, st.story_id"
        )
        rows = self.conn.execute(
            f"""
            SELECT st.*, src.name AS representative_source_name
              FROM news_stories st
              JOIN news_sources src
                ON src.source_id = st.representative_source_id
             WHERE {" AND ".join(where)}
             ORDER BY {order}
             LIMIT %s
            """,
            (*params, limit + 1),
        ).fetchall()
        has_more = len(rows) > limit
        page = rows[:limit]
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
        return {
            "sort": sort,
            "filters": filters,
            "stories": [_story_summary(row) for row in page],
            "next_cursor": next_cursor,
            "has_more": has_more,
            "facets": self._feed_facets(),
        }

    def _feed_facets(self) -> dict[str, Any]:
        categories = self.conn.execute(
            """
            SELECT category AS value, count(*) AS count
              FROM news_stories
             WHERE active
             GROUP BY category
             ORDER BY count(*) DESC, category
             LIMIT %s
            """,
            (_PUBLIC_LIST_LIMIT + 1,),
        ).fetchall()
        levels = self.conn.execute(
            """
            SELECT level AS value, count(*) AS count
              FROM news_stories
             WHERE active
             GROUP BY level
             ORDER BY count(*) DESC, level
             LIMIT %s
            """,
            (_PUBLIC_LIST_LIMIT + 1,),
        ).fetchall()
        sources = self.conn.execute(
            """
            SELECT src.source_id AS value, src.name AS label,
                   count(DISTINCT m.story_id) AS count
              FROM news_story_members m
              JOIN news_stories st ON st.story_id = m.story_id
              JOIN news_items i ON i.item_id = m.item_id
              JOIN news_sources src ON src.source_id = i.source_id
             WHERE st.active AND m.current
             GROUP BY src.source_id, src.name
             ORDER BY count(DISTINCT m.story_id) DESC, src.name, src.source_id
             LIMIT %s
            """,
            (_PUBLIC_LIST_LIMIT + 1,),
        ).fetchall()
        return {
            "categories": [dict(row) for row in categories[:_PUBLIC_LIST_LIMIT]],
            "levels": [dict(row) for row in levels[:_PUBLIC_LIST_LIMIT]],
            "sources": [dict(row) for row in sources[:_PUBLIC_LIST_LIMIT]],
            "page": {
                "categories_has_more": len(categories) > _PUBLIC_LIST_LIMIT,
                "levels_has_more": len(levels) > _PUBLIC_LIST_LIMIT,
                "sources_has_more": len(sources) > _PUBLIC_LIST_LIMIT,
            },
        }

    def get_story(
        self,
        *,
        story_id: str,
        members_limit: int = 100,
        members_cursor: str | None = None,
    ) -> dict[str, Any] | None:
        if members_limit < 1 or members_limit > 100:
            raise ValueError("news_story_members_limit_invalid")
        row = self.conn.execute(
            """
            SELECT st.*, src.name AS representative_source_name
              FROM news_stories st
              JOIN news_sources src
                ON src.source_id = st.representative_source_id
             WHERE st.story_id = %s
            """,
            (story_id,),
        ).fetchone()
        if row is None:
            return None
        member_where = ["m.story_id = %s"]
        member_params: list[Any] = [story_id]
        if members_cursor:
            decoded = _cursor_decode(members_cursor)
            if decoded.get("v") != 1 or decoded.get("kind") != "story_members":
                raise ValueError("news_story_members_cursor_invalid")
            if decoded.get("story_id") != story_id:
                raise ValueError("news_story_members_cursor_story_mismatch")
            try:
                current_rank = int(decoded["current_rank"])
                published_at_ms = int(decoded["published_at_ms"])
                item_id = str(decoded["item_id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("news_story_members_cursor_invalid") from exc
            member_where.append(
                """
                (
                  CASE WHEN m.current THEN 1 ELSE 0 END < %s
                  OR (
                    CASE WHEN m.current THEN 1 ELSE 0 END = %s
                    AND i.published_at_ms < %s
                  )
                  OR (
                    CASE WHEN m.current THEN 1 ELSE 0 END = %s
                    AND i.published_at_ms = %s
                    AND i.item_id > %s
                  )
                )
                """
            )
            member_params.extend(
                [
                    current_rank,
                    current_rank,
                    published_at_ms,
                    current_rank,
                    published_at_ms,
                    item_id,
                ]
            )
        member_params.append(members_limit + 1)
        members = self.conn.execute(
            f"""
            SELECT i.*, src.name AS source_name, src.tier,
                   m.current, m.first_joined_at_ms, m.last_confirmed_at_ms
              FROM news_story_members m
              JOIN news_items i ON i.item_id = m.item_id
              JOIN news_sources src ON src.source_id = i.source_id
             WHERE {" AND ".join(member_where)}
             ORDER BY m.current DESC, i.published_at_ms DESC, i.item_id
             LIMIT %s
            """,
            member_params,
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
                    "current_rank": 1 if bool(last["current"]) else 0,
                    "published_at_ms": int(last["published_at_ms"]),
                    "item_id": str(last["item_id"]),
                }
            )
        return {
            **_story_summary(row),
            "canonical_title": str(row["canonical_title"]),
            "active": bool(row["active"]),
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
        where = ["s.enabled"]
        params: list[Any] = []
        if cursor:
            decoded = _cursor_decode(cursor)
            if decoded.get("v") != 1 or decoded.get("kind") != "sources":
                raise ValueError("news_sources_cursor_invalid")
            try:
                tier = int(decoded["tier"])
                name = str(decoded["name"])
                source_id = str(decoded["source_id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("news_sources_cursor_invalid") from exc
            where.append("(s.tier, s.name, s.source_id) > (%s, %s, %s)")
            params.extend([tier, name, source_id])
        params.append(limit + 1)
        rows = self.conn.execute(
            f"""
            SELECT s.*,
                   COALESCE(m.memberships, ARRAY[]::text[]) AS memberships,
                   f.fetch_id AS latest_fetch_id,
                   f.status AS latest_fetch_status,
                   f.fetch_path AS latest_fetch_path,
                   f.direct_error_code AS latest_direct_error_code,
                   f.started_at_ms AS latest_fetch_started_at_ms,
                   f.finished_at_ms AS latest_fetch_finished_at_ms,
                   (f.finished_at_ms - f.started_at_ms)
                     AS latest_fetch_duration_ms,
                   f.http_status AS latest_fetch_http_status,
                   f.entries_seen AS latest_entries_seen,
                   f.observations_inserted AS latest_observations_inserted,
                   f.items_inserted AS latest_items_inserted,
                   f.items_updated AS latest_items_updated,
                   f.rejection_counts AS latest_rejection_counts,
                   f.error_code AS latest_fetch_error_code
              FROM news_sources s
              LEFT JOIN LATERAL (
                SELECT array_agg(membership ORDER BY membership) AS memberships
                  FROM news_source_memberships
                 WHERE source_id = s.source_id
              ) m ON true
              LEFT JOIN LATERAL (
                SELECT *
                  FROM news_source_fetches
                 WHERE source_id = s.source_id
                 ORDER BY finished_at_ms DESC, fetch_id DESC
                 LIMIT 1
              ) f ON true
             WHERE {" AND ".join(where)}
             ORDER BY s.tier, s.name, s.source_id
             LIMIT %s
            """,
            params,
        ).fetchall()
        has_more = len(rows) > limit
        page = rows[:limit]
        next_cursor = None
        if has_more and page:
            last = page[-1]
            next_cursor = _cursor_encode(
                {
                    "v": 1,
                    "kind": "sources",
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

    def brief_candidates(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            WITH ranked AS (
              SELECT st.*, src.name AS representative_source_name,
                     row_number() OVER (
                       PARTITION BY st.representative_source_id
                       ORDER BY st.importance_score DESC,
                                st.last_published_at_ms DESC,
                                st.story_id
                     ) AS source_rank
                FROM news_stories st
                JOIN news_sources src
                  ON src.source_id = st.representative_source_id
                JOIN news_items item ON item.item_id = st.representative_item_id
               WHERE st.active
                 AND src.enabled
                 AND NOT item.brief_excluded
            )
            SELECT *
              FROM ranked
             WHERE source_rank <= 3
             ORDER BY importance_score DESC, last_published_at_ms DESC, story_id
             LIMIT 8
            """
        ).fetchall()
        return select_top_stories(rows, limit=8, max_per_source=3)

    def record_brief_insufficient(
        self,
        *,
        fingerprint: str,
        story_count: int,
        source_count: int,
        now_ms: int,
    ) -> None:
        self.conn.execute("SELECT pg_advisory_xact_lock(%s)", (_BRIEF_LOCK_KEY,))
        run_id = deterministic_id("brief_run", fingerprint)
        self.conn.execute(
            """
            INSERT INTO news_brief_runs (
              run_id, fingerprint, status, attempt_count,
              candidate_story_count, candidate_source_count,
              created_at_ms, updated_at_ms, completed_at_ms
            )
            VALUES (%s, %s, 'insufficient_material', 0, %s, %s, %s, %s, %s)
            ON CONFLICT (fingerprint) DO UPDATE SET
              status = 'insufficient_material',
              candidate_story_count = EXCLUDED.candidate_story_count,
              candidate_source_count = EXCLUDED.candidate_source_count,
              lease_owner = NULL,
              lease_expires_at_ms = NULL,
              heartbeat_at_ms = NULL,
              last_error = NULL,
              updated_at_ms = EXCLUDED.updated_at_ms,
              completed_at_ms = EXCLUDED.completed_at_ms
            WHERE news_brief_runs.status <> 'ready'
              AND (
                news_brief_runs.status <> 'insufficient_material'
                OR news_brief_runs.candidate_story_count
                     IS DISTINCT FROM EXCLUDED.candidate_story_count
                OR news_brief_runs.candidate_source_count
                     IS DISTINCT FROM EXCLUDED.candidate_source_count
              )
            """,
            (
                run_id,
                fingerprint,
                story_count,
                source_count,
                now_ms,
                now_ms,
                now_ms,
            ),
        )
        self.conn.execute(
            """
            UPDATE news_brief_current
               SET target_fingerprint = %s,
                   latest_run_id = %s,
                   updated_at_ms = %s
             WHERE singleton_key
               AND (
                 target_fingerprint IS DISTINCT FROM %s
                 OR latest_run_id IS DISTINCT FROM %s
               )
            """,
            (fingerprint, run_id, now_ms, fingerprint, run_id),
        )

    def claim_brief_run(
        self,
        *,
        fingerprint: str,
        story_count: int,
        source_count: int,
        now_ms: int,
        max_attempts: int,
        lease_owner: str,
    ) -> dict[str, str] | None:
        self.conn.execute("SELECT pg_advisory_xact_lock(%s)", (_BRIEF_LOCK_KEY,))
        row = self.conn.execute(
            """
            SELECT *
              FROM news_brief_runs
             WHERE fingerprint = %s
             FOR UPDATE
            """,
            (fingerprint,),
        ).fetchone()
        if row is not None:
            status = str(row["status"])
            if status in {"ready", "insufficient_material"}:
                return None
            if (
                status == "running"
                and row["lease_expires_at_ms"] is not None
                and int(row["lease_expires_at_ms"]) > now_ms
            ):
                return None
            if int(row["attempt_count"]) >= max_attempts:
                self.conn.execute(
                    """
                    UPDATE news_brief_runs
                       SET status = 'failed',
                           lease_owner = NULL,
                           lease_expires_at_ms = NULL,
                           heartbeat_at_ms = NULL,
                           last_error = 'brief_attempts_exhausted',
                           updated_at_ms = %s,
                           completed_at_ms = %s
                     WHERE run_id = %s
                       AND status <> 'failed'
                    """,
                    (now_ms, now_ms, str(row["run_id"])),
                )
                return None
            run_id = str(row["run_id"])
            attempt_count = int(row["attempt_count"]) + 1
        else:
            run_id = deterministic_id("brief_run", fingerprint)
            attempt_count = 1
        owner = str(lease_owner).strip()
        if not owner:
            raise ValueError("news_brief_lease_owner_required")
        self.conn.execute(
            """
            INSERT INTO news_brief_runs (
              run_id, fingerprint, status, attempt_count,
              candidate_story_count, candidate_source_count,
              lease_owner, lease_expires_at_ms, heartbeat_at_ms,
              created_at_ms, updated_at_ms
            )
            VALUES (%s, %s, 'running', %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (fingerprint) DO UPDATE SET
              status = 'running',
              attempt_count = EXCLUDED.attempt_count,
              candidate_story_count = EXCLUDED.candidate_story_count,
              candidate_source_count = EXCLUDED.candidate_source_count,
              lease_owner = EXCLUDED.lease_owner,
              lease_expires_at_ms = EXCLUDED.lease_expires_at_ms,
              heartbeat_at_ms = EXCLUDED.heartbeat_at_ms,
              last_error = NULL,
              updated_at_ms = EXCLUDED.updated_at_ms,
              completed_at_ms = NULL
            """,
            (
                run_id,
                fingerprint,
                attempt_count,
                story_count,
                source_count,
                owner,
                now_ms + _BRIEF_LEASE_MS,
                now_ms,
                now_ms,
                now_ms,
            ),
        )
        self.conn.execute(
            """
            UPDATE news_brief_current
               SET target_fingerprint = %s,
                   latest_run_id = %s,
                   updated_at_ms = %s
             WHERE singleton_key
            """,
            (fingerprint, run_id, now_ms),
        )
        return {"run_id": run_id, "lease_owner": owner}

    def publish_brief(
        self,
        *,
        run_id: str,
        lease_owner: str,
        fingerprint: str,
        stories: Sequence[Mapping[str, Any]],
        draft: NewsBriefDraft,
        validation: Mapping[str, Any],
        now_ms: int,
    ) -> str:
        publication_id = deterministic_id("brief", fingerprint)
        sources = [
            {
                "n": index + 1,
                "story_id": str(story["story_id"]),
                "title": str(story["representative_title"]),
                "source": str(story["representative_source_name"]),
                "url": str(story["representative_url"]),
            }
            for index, story in enumerate(stories)
        ]
        evidence_cutoff = max(int(story["last_published_at_ms"]) for story in stories)
        claimed = self.conn.execute(
            """
            SELECT 1
              FROM news_brief_runs
             WHERE run_id = %s
               AND fingerprint = %s
               AND status = 'running'
               AND lease_owner = %s
               AND lease_expires_at_ms > %s
             FOR UPDATE
            """,
            (run_id, fingerprint, lease_owner, now_ms),
        ).fetchone()
        if claimed is None:
            raise RuntimeError("news_brief_lease_lost")
        current_candidates = self.brief_candidates()
        if brief_fingerprint(current_candidates) != fingerprint:
            raise RuntimeError("news_brief_source_fingerprint_changed")
        self.conn.execute(
            """
            INSERT INTO news_brief_publications (
              publication_id, fingerprint, evidence_cutoff_at_ms,
              published_at_ms, provider, model, prompt_version,
              workflow_version, schema_version, locale, selected_story_ids,
              lead, lines, sources, validation, raw_response, created_at_ms
            )
            VALUES (
              %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
              %s, %s, %s, %s
            )
            ON CONFLICT (fingerprint) DO NOTHING
            """,
            (
                publication_id,
                fingerprint,
                evidence_cutoff,
                now_ms,
                draft.provider,
                draft.model,
                BRIEF_PROMPT_VERSION,
                BRIEF_WORKFLOW_VERSION,
                BRIEF_SCHEMA_VERSION,
                NEWS_LOCALE,
                Jsonb([str(story["story_id"]) for story in stories]),
                draft.lead,
                Jsonb(list(draft.lines)),
                Jsonb(sources),
                Jsonb(dict(validation)),
                draft.raw_response,
                now_ms,
            ),
        )
        self.conn.execute(
            """
            UPDATE news_brief_runs
               SET status = 'ready',
                   lease_owner = NULL,
                   lease_expires_at_ms = NULL,
                   heartbeat_at_ms = %s,
                   completed_at_ms = %s,
                   updated_at_ms = %s
             WHERE run_id = %s AND lease_owner = %s
            """,
            (now_ms, now_ms, now_ms, run_id, lease_owner),
        )
        self.conn.execute(
            """
            UPDATE news_brief_current
               SET publication_id = %s,
                   target_fingerprint = %s,
                   latest_run_id = %s,
                   updated_at_ms = %s
             WHERE singleton_key
            """,
            (publication_id, fingerprint, run_id, now_ms),
        )
        return publication_id

    def fail_brief_run(
        self,
        *,
        run_id: str,
        lease_owner: str,
        error: Exception,
        now_ms: int,
    ) -> None:
        self.conn.execute(
            """
            UPDATE news_brief_runs
               SET status = 'failed',
                   lease_owner = NULL,
                   lease_expires_at_ms = NULL,
                   heartbeat_at_ms = %s,
                   last_error = %s,
                   completed_at_ms = %s,
                   updated_at_ms = %s
             WHERE run_id = %s
               AND status = 'running'
               AND lease_owner = %s
            """,
            (
                now_ms,
                f"{type(error).__name__}:{str(error)[:1000]}",
                now_ms,
                now_ms,
                run_id,
                lease_owner,
            ),
        )

    def get_brief(self, *, now_ms: int, history_limit: int = 20) -> dict[str, Any]:
        candidates = self.brief_candidates()
        fingerprint = brief_fingerprint(candidates)
        candidate_sources = {str(candidate["representative_source_id"]) for candidate in candidates}
        sufficient = len(candidates) >= 3 and len(candidate_sources) >= 2
        current = self.conn.execute(
            """
            SELECT c.publication_id, c.target_fingerprint, c.latest_run_id,
                   c.updated_at_ms, p.*
              FROM news_brief_current c
              LEFT JOIN news_brief_publications p
                ON p.publication_id = c.publication_id
             WHERE c.singleton_key
            """
        ).fetchone()
        run = self.conn.execute(
            """
            SELECT *
              FROM news_brief_runs
             WHERE fingerprint = %s
             ORDER BY updated_at_ms DESC
             LIMIT 1
            """,
            (fingerprint,),
        ).fetchone()
        history = self.conn.execute(
            """
            SELECT *
              FROM news_brief_publications
             ORDER BY published_at_ms DESC, publication_id DESC
             LIMIT %s
            """,
            (history_limit,),
        ).fetchall()
        publication = _brief_payload(current) if current is not None and current["publication_id"] else None
        publication_matches = publication is not None and publication["fingerprint"] == fingerprint
        run_active = (
            run is not None
            and str(run["status"]) == "running"
            and run["lease_expires_at_ms"] is not None
            and int(run["lease_expires_at_ms"]) > now_ms
            and run["heartbeat_at_ms"] is not None
            and int(run["heartbeat_at_ms"]) > now_ms - _BRIEF_LEASE_MS
        )
        run_failed = run is not None and (
            str(run["status"]) == "failed" or (str(run["status"]) == "running" and not run_active)
        )
        if not sufficient:
            state = "insufficient_material"
        elif publication_matches:
            state = "ready"
        elif run_active:
            state = "running"
        elif publication is not None:
            state = "stale_fallback"
        elif run_failed:
            state = "failed"
        else:
            state = "unavailable"
        return {
            "state": state,
            "target_fingerprint": fingerprint,
            "candidate_story_count": len(candidates),
            "candidate_source_count": len(candidate_sources),
            "publication": publication if sufficient else None,
            "latest_run": _brief_run_payload(run, now_ms=now_ms) if run else None,
            "history": [_brief_payload(row) for row in history],
        }

    # Health -------------------------------------------------------------------

    def health_snapshot(self, *, now_ms: int) -> dict[str, Any]:
        source = self.conn.execute(
            """
            WITH latest_fetch AS (
              SELECT DISTINCT ON (source_id)
                     source_id, status, fetch_path, direct_error_code,
                     entries_seen
                FROM news_source_fetches
               ORDER BY source_id, finished_at_ms DESC, fetch_id DESC
            )
            SELECT count(*) FILTER (WHERE s.enabled) AS enabled_count,
                   count(*) FILTER (
                     WHERE s.enabled
                       AND s.last_fetch_finished_at_ms IS NOT NULL
                   ) AS attempted_count,
                   count(*) FILTER (
                     WHERE s.enabled AND s.last_success_at_ms IS NOT NULL
                   ) AS successful_count,
                   count(*) FILTER (
                     WHERE s.enabled AND s.last_success_at_ms >= %s
                   ) AS recent_success_count,
                   count(*) FILTER (
                     WHERE s.enabled
                       AND lf.status = 'success'
                       AND lf.entries_seen = 0
                   ) AS empty_count,
                   count(*) FILTER (
                     WHERE s.enabled AND s.consecutive_failures > 0
                   ) AS failing_count,
                   count(*) FILTER (
                     WHERE s.enabled AND s.next_fetch_at_ms < %s
                   ) AS overdue_count,
                   count(*) FILTER (
                     WHERE s.enabled
                       AND lf.status <> 'failed'
                       AND lf.fetch_path = 'direct'
                   ) AS direct_success_count,
                   count(*) FILTER (
                     WHERE s.enabled
                       AND lf.status <> 'failed'
                       AND lf.fetch_path = 'relay'
                   ) AS relay_success_count,
                   count(*) FILTER (
                     WHERE s.enabled
                       AND lf.status = 'failed'
                       AND lf.fetch_path = 'relay'
                       AND lf.direct_error_code IS NOT NULL
                   ) AS both_failed_count,
                   max(s.last_success_at_ms) AS last_success_at_ms
              FROM news_sources s
              LEFT JOIN latest_fetch lf ON lf.source_id = s.source_id
            """,
            (now_ms - 3_600_000, now_ms - 300_000),
        ).fetchone()
        recent_fetches = self.conn.execute(
            """
            SELECT rejection_counts
              FROM news_source_fetches
             WHERE finished_at_ms >= %s
            """,
            (now_ms - 3_600_000,),
        ).fetchall()
        gate_counts: Counter[str] = Counter()
        for fetch in recent_fetches:
            gate_counts.update({str(key): int(value) for key, value in dict(fetch["rejection_counts"]).items()})
        story = self.conn.execute(
            """
            SELECT active_story_count AS active_count,
                   newest_story_at_ms,
                   last_material_change_at_ms,
                   active_item_count,
                   newest_item_at_ms,
                   unmaterialized_item_count,
                   invalid_owner_count,
                   invalid_story_aggregate_count
              FROM news_projection_summary
             WHERE singleton_key = 'current'
            """
        ).fetchone()
        brief = self.get_brief(now_ms=now_ms, history_limit=1)
        enabled = int(source["enabled_count"] or 0)
        attempted = int(source["attempted_count"] or 0)
        recent = int(source["recent_success_count"] or 0)
        coverage_ratio = recent / enabled if enabled else 0.0
        ingest_reasons: list[str] = []
        if enabled == 0:
            ingest_status = "degraded"
            ingest_reasons.append("no_enabled_sources")
        elif attempted < enabled:
            ingest_status = "warming"
            ingest_reasons.append("not_all_sources_attempted")
        elif int(source["failing_count"] or 0) > 0:
            ingest_status = "degraded"
            ingest_reasons.append("source_failures_present")
        elif coverage_ratio < 0.8:
            ingest_status = "degraded"
            ingest_reasons.append("recent_source_coverage_below_80_percent")
        elif int(source["overdue_count"] or 0) > max(3, enabled // 5):
            ingest_status = "degraded"
            ingest_reasons.append("material_source_poll_overdue")
        else:
            ingest_status = "ready"

        unmaterialized = int(story["unmaterialized_item_count"] or 0)
        invalid_owners = int(story["invalid_owner_count"] or 0)
        invalid_aggregates = int(story["invalid_story_aggregate_count"] or 0)
        active_stories = int(story["active_count"] or 0)
        story_reasons: list[str] = []
        if unmaterialized or invalid_owners or invalid_aggregates:
            story_status = "degraded"
            if unmaterialized:
                story_reasons.append("active_items_unmaterialized")
            if invalid_owners:
                story_reasons.append("current_item_owner_invalid")
            if invalid_aggregates:
                story_reasons.append("story_aggregate_invalid")
        elif active_stories == 0:
            story_status = "warming"
            story_reasons.append("no_active_stories_yet")
        else:
            story_status = "ready"

        brief_status = (
            "ready"
            if brief["state"] in {"ready", "insufficient_material"}
            else "degraded"
            if brief["state"] in {"stale_fallback", "failed"}
            else "warming"
        )
        brief_reasons = [] if brief_status == "ready" else [f"public_brief_{brief['state']}"]
        layers = {
            "ingest": {
                "status": ingest_status,
                "reasons": ingest_reasons,
                "configured_sources": enabled,
                "enabled_sources": enabled,
                "attempted_sources": attempted,
                "terminal_sources": attempted,
                "successful_sources": int(source["successful_count"] or 0),
                "empty_sources": int(source["empty_count"] or 0),
                "recent_success_sources": recent,
                "recent_coverage_ratio": round(coverage_ratio, 4),
                "failing_sources": int(source["failing_count"] or 0),
                "overdue_sources": int(source["overdue_count"] or 0),
                "direct_success_sources": int(source["direct_success_count"] or 0),
                "relay_success_sources": int(source["relay_success_count"] or 0),
                "both_failed_sources": int(source["both_failed_count"] or 0),
                "last_success_at_ms": source["last_success_at_ms"],
                "gate_counts_1h": dict(gate_counts),
            },
            "story": {
                "status": story_status,
                "reasons": story_reasons,
                "active_items": int(story["active_item_count"] or 0),
                "active_stories": active_stories,
                "newest_item_at_ms": story["newest_item_at_ms"],
                "newest_story_at_ms": story["newest_story_at_ms"],
                "last_material_change_at_ms": story["last_material_change_at_ms"],
                "unmaterialized_item_count": unmaterialized,
                "invalid_owner_count": invalid_owners,
                "invalid_story_aggregate_count": invalid_aggregates,
                "invariant_error_count": invalid_owners + invalid_aggregates,
                "identity_version": STORY_IDENTITY_VERSION,
                "classifier_version": CLASSIFIER_VERSION,
                "importance_version": IMPORTANCE_VERSION,
            },
            "brief": {
                "status": brief_status,
                "reasons": brief_reasons,
                "public_state": brief["state"],
                "target_fingerprint": brief["target_fingerprint"],
                "publication_id": (brief["publication"]["publication_id"] if brief["publication"] else None),
                "latest_run": brief["latest_run"],
            },
        }
        statuses = [ingest_status, story_status, brief_status]
        overall = "degraded" if "degraded" in statuses else "warming" if "warming" in statuses else "ready"
        return {
            "status": overall,
            "reasons": [f"{name}:{reason}" for name, details in layers.items() for reason in details["reasons"]],
            "layers": layers,
            "measured_at_ms": now_ms,
        }


def _story_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "story_id": str(row["story_id"]),
        "title": str(row["representative_title"]),
        "description": str(row["representative_description"]),
        "url": str(row["representative_url"]),
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


def _item_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "item_id": str(row["item_id"]),
        "source_id": str(row["source_id"]),
        "source_name": str(row["source_name"]),
        "reporting_origin": str(row["reporting_origin"]),
        "tier": int(row["tier"]),
        "title": str(row["title"]),
        "description": str(row["description"]),
        "url": str(row["canonical_url"]),
        "lang": str(row["lang"]),
        "published_at_ms": int(row["published_at_ms"]),
        "last_observed_at_ms": int(row["last_observed_at_ms"]),
        "level": str(row["level"]),
        "category": str(row["category"]),
        "importance_score": int(row["importance_score"]),
        "importance_factors": dict(row["importance_factors"]),
        "current": bool(row["current"]),
        "first_joined_at_ms": int(row["first_joined_at_ms"]),
        "last_confirmed_at_ms": int(row["last_confirmed_at_ms"]),
    }


def _brief_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "publication_id": str(row["publication_id"]),
        "fingerprint": str(row["fingerprint"]),
        "evidence_cutoff_at_ms": int(row["evidence_cutoff_at_ms"]),
        "published_at_ms": int(row["published_at_ms"]),
        "provider": str(row["provider"]),
        "model": str(row["model"]),
        "prompt_version": str(row["prompt_version"]),
        "workflow_version": str(row["workflow_version"]),
        "schema_version": str(row["schema_version"]),
        "locale": str(row["locale"]),
        "selected_story_ids": list(row["selected_story_ids"]),
        "lead": str(row["lead"]),
        "lines": list(row["lines"]),
        "sources": list(row["sources"]),
        "validation": dict(row["validation"]),
    }


def _brief_run_payload(
    row: Mapping[str, Any],
    *,
    now_ms: int,
) -> dict[str, Any]:
    status = str(row["status"])
    last_error = row["last_error"]
    if status == "running" and (
        row["lease_expires_at_ms"] is None
        or int(row["lease_expires_at_ms"]) <= now_ms
        or row["heartbeat_at_ms"] is None
        or int(row["heartbeat_at_ms"]) <= now_ms - _BRIEF_LEASE_MS
    ):
        status = "failed"
        last_error = last_error or "brief_lease_expired"
    return {
        "run_id": str(row["run_id"]),
        "fingerprint": str(row["fingerprint"]),
        "status": status,
        "attempt_count": int(row["attempt_count"]),
        "candidate_story_count": int(row["candidate_story_count"]),
        "candidate_source_count": int(row["candidate_source_count"]),
        "heartbeat_at_ms": row["heartbeat_at_ms"],
        "lease_expires_at_ms": row["lease_expires_at_ms"],
        "last_error": last_error,
        "created_at_ms": int(row["created_at_ms"]),
        "updated_at_ms": int(row["updated_at_ms"]),
        "completed_at_ms": row["completed_at_ms"],
    }


__all__ = ["NewsRepository", "deterministic_id"]
