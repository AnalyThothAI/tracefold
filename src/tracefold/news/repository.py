from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from psycopg.types.json import Jsonb

from tracefold.platform.postgres.queue_terminal import terminalize_source_row

from .brief import brief_fingerprint
from .classification import classify_by_keyword
from .identity import normalize_story_text
from .models import (
    BRIEF_PROMPT_VERSION,
    BRIEF_SCHEMA_VERSION,
    BRIEF_WORKFLOW_VERSION,
    CLASSIFIER_VERSION,
    IMPORTANCE_VERSION,
    NEWS_LOCALE,
    STORY_IDENTITY_VERSION,
    NewsBriefDraft,
    NewsSourceDefinition,
)
from .opennews import OpenNewsEvent
from .ranking import (
    is_delayed_brief_excluded,
    select_top_stories,
)

_BRIEF_LOCK_KEY = 727_301_985
_ACTIVE_WINDOW_MS = 96 * 60 * 60 * 1000
_BRIEF_LEASE_MS = 120_000
_PUBLIC_LIST_LIMIT = 100
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


def _opennews_content_fingerprint(
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


class NewsRepository:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    @staticmethod
    def stable_json_hash(value: object) -> str:
        return _sha256_json(value)

    # Source inventory and acquisition -----------------------------------------

    def sync_sources(self, sources: Sequence[NewsSourceDefinition], *, now_ms: int) -> None:
        source_ids = [source.source_id for source in sources]
        for source in sources:
            self.conn.execute(
                """
                INSERT INTO news_sources (
                  source_id, name, feed_url, tier, lang, enabled,
                  source_kind, refresh_interval_seconds, next_fetch_at_ms,
                  created_at_ms, updated_at_ms
                )
                VALUES (
                  %(source_id)s, %(name)s, %(feed_url)s, %(tier)s, %(lang)s,
                  %(enabled)s, %(source_kind)s, %(refresh_interval_seconds)s, %(now_ms)s,
                  %(now_ms)s, %(now_ms)s
                )
                ON CONFLICT (source_id) DO UPDATE SET
                  name = EXCLUDED.name,
                  feed_url = EXCLUDED.feed_url,
                  tier = EXCLUDED.tier,
                  lang = EXCLUDED.lang,
                  enabled = EXCLUDED.enabled,
                  source_kind = EXCLUDED.source_kind,
                  refresh_interval_seconds = EXCLUDED.refresh_interval_seconds,
                  updated_at_ms = EXCLUDED.updated_at_ms
                WHERE (
                  news_sources.name,
                  news_sources.feed_url,
                  news_sources.tier,
                  news_sources.lang,
                  news_sources.enabled,
                  news_sources.source_kind,
                  news_sources.refresh_interval_seconds
                ) IS DISTINCT FROM (
                  EXCLUDED.name,
                  EXCLUDED.feed_url,
                  EXCLUDED.tier,
                  EXCLUDED.lang,
                  EXCLUDED.enabled,
                  EXCLUDED.source_kind,
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

    def opennews_recovery_state(self, *, source_id: str) -> tuple[int | None, str | None, int]:
        row = self.conn.execute(
            """
            SELECT GREATEST(
                     COALESCE(source.last_fetch_started_at_ms, 0),
                     COALESCE(source.last_fetch_finished_at_ms, 0),
                     COALESCE(source.last_recovery_at_ms, 0)
                   ) AS last_attempt_at_ms,
                   COALESCE(
                     source.gap_boundary_provider_record_id,
                     (
                       SELECT item.provider_record_id
                         FROM news_items item
                        WHERE item.source_id = source.source_id
                        ORDER BY item.last_observed_at_ms DESC, item.item_id DESC
                        LIMIT 1
                     )
                   ) AS recovery_boundary_provider_record_id,
                   source.gap_version
              FROM news_sources source
             WHERE source.source_id = %s AND source.source_kind = 'opennews'
            """,
            (source_id,),
        ).fetchone()
        if row is None:
            return None, None, 0
        last_attempt_at_ms = int(row["last_attempt_at_ms"] or 0)
        boundary_provider_record_id = row["recovery_boundary_provider_record_id"]
        return (
            last_attempt_at_ms if last_attempt_at_ms > 0 else None,
            str(boundary_provider_record_id) if boundary_provider_record_id is not None else None,
            int(row["gap_version"]),
        )

    def mark_opennews_recovery_attempt(self, *, source_id: str, started_at_ms: int) -> None:
        self.conn.execute(
            """
            UPDATE news_sources
               SET last_fetch_started_at_ms = %s,
                   gap_unclosed = true,
                   updated_at_ms = %s
             WHERE source_id = %s AND source_kind = 'opennews'
            """,
            (started_at_ms, started_at_ms, source_id),
        )

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
                key: value
                for key, value in event.provider_metadata.items()
                if value not in (None, "", [], {})
            }
            if event.observation_kind == "provider_annotation":
                cursor = self.conn.execute(
                    """
                    UPDATE news_items
                       SET provider_metadata = provider_metadata || %s,
                           last_observed_at_ms = %s,
                           updated_at_ms = %s
                     WHERE source_id = %s
                       AND provider_record_id = %s
                       AND provider_metadata IS DISTINCT FROM provider_metadata || %s
                    """,
                    (
                        Jsonb(incoming_metadata),
                        int(observed_at_ms),
                        int(observed_at_ms),
                        source.source_id,
                        event.provider_record_id,
                        Jsonb(incoming_metadata),
                    ),
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
            if published_at_ms < observed_at_ms - _ACTIVE_WINDOW_MS:
                rejections["stale_age"] += 1
                continue

            reporting_origin = str(entry.reporting_origin or source.name).strip().lower()
            description = str(entry.description or "").strip()
            language = entry.language or source.lang
            content_fingerprint = _opennews_content_fingerprint(
                title=title,
                description=description,
                canonical_url=canonical_url,
                reporting_origin=reporting_origin,
                published_at_ms=published_at_ms,
                language=language,
            )
            item_id = deterministic_id("news_item", source.source_id, event.provider_record_id)
            classification = classify_by_keyword(title, now_ms=observed_at_ms)
            values = {
                "item_id": item_id,
                "source_id": source.source_id,
                "source_item_key": event.provider_record_id,
                "provider_record_id": event.provider_record_id,
                "provider_metadata": Jsonb(incoming_metadata),
                "canonical_url": canonical_url,
                "reporting_origin": reporting_origin,
                "title": title,
                "normalized_title": normalize_story_text(title),
                "description": description,
                "lang": language,
                "published_at_ms": published_at_ms,
                "observed_at_ms": observed_at_ms,
                "content_fingerprint": content_fingerprint,
                "level": classification.level,
                "category": classification.category,
                "classification_source": classification.source,
                "classification_confidence": classification.confidence,
                "brief_excluded": is_delayed_brief_excluded(
                    title=title,
                    url=str(canonical_url or ""),
                    description=description,
                ),
            }
            cursor = self.conn.execute(
                """
                INSERT INTO news_items AS current_item (
                  item_id, source_id, source_item_key, provider_record_id,
                  provider_metadata, canonical_url, reporting_origin, title,
                  normalized_title, description, lang, published_at_ms,
                  first_observed_at_ms, last_observed_at_ms,
                  content_fingerprint, level, category,
                  classification_source, classification_confidence,
                  importance_score, importance_factors, brief_excluded,
                  active, created_at_ms, updated_at_ms
                ) VALUES (
                  %(item_id)s, %(source_id)s, %(source_item_key)s,
                  %(provider_record_id)s, %(provider_metadata)s,
                  %(canonical_url)s, %(reporting_origin)s, %(title)s,
                  %(normalized_title)s, %(description)s, %(lang)s,
                  %(published_at_ms)s, %(observed_at_ms)s,
                  %(observed_at_ms)s, %(content_fingerprint)s,
                  %(level)s, %(category)s, %(classification_source)s,
                  %(classification_confidence)s, 0, '{}'::jsonb,
                  %(brief_excluded)s, true, %(observed_at_ms)s,
                  %(observed_at_ms)s
                )
                ON CONFLICT (source_id, provider_record_id)
                  WHERE provider_record_id IS NOT NULL
                DO UPDATE SET
                  source_item_key = EXCLUDED.source_item_key,
                  provider_metadata = current_item.provider_metadata || EXCLUDED.provider_metadata,
                  canonical_url = EXCLUDED.canonical_url,
                  reporting_origin = EXCLUDED.reporting_origin,
                  title = EXCLUDED.title,
                  normalized_title = EXCLUDED.normalized_title,
                  description = EXCLUDED.description,
                  lang = EXCLUDED.lang,
                  published_at_ms = EXCLUDED.published_at_ms,
                  last_observed_at_ms = EXCLUDED.last_observed_at_ms,
                  content_fingerprint = EXCLUDED.content_fingerprint,
                  level = EXCLUDED.level,
                  category = EXCLUDED.category,
                  classification_source = EXCLUDED.classification_source,
                  classification_confidence = EXCLUDED.classification_confidence,
                  brief_excluded = EXCLUDED.brief_excluded,
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

        if recovery_started_at_ms is not None:
            self.conn.execute(
                """
                UPDATE news_sources
                   SET last_fetch_started_at_ms = %s,
                       last_fetch_finished_at_ms = %s,
                       last_recovery_at_ms = %s,
                       last_success_at_ms = %s,
                       last_http_status = 200,
                       consecutive_failures = 0,
                       last_error = NULL,
                       updated_at_ms = %s
                 WHERE source_id = %s
                """,
                (
                    int(recovery_started_at_ms),
                    int(observed_at_ms),
                    int(observed_at_ms),
                    int(observed_at_ms),
                    int(observed_at_ms),
                    source.source_id,
                ),
            )
        elif events:
            self.conn.execute(
                """
                UPDATE news_sources
                   SET last_live_at_ms = %s,
                       last_success_at_ms = %s,
                       last_error = NULL,
                       updated_at_ms = %s
                 WHERE source_id = %s
                """,
                (observed_at_ms, observed_at_ms, observed_at_ms, source.source_id),
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
        gap_unclosed: bool,
        gap_boundary_provider_record_id: str | None,
        expected_gap_version: int | None,
    ) -> tuple[str | None, int] | None:
        row = self.conn.execute(
            """
            UPDATE news_sources AS source
               SET live_connected = %(connected)s,
                   last_live_at_ms = CASE
                     WHEN %(connected)s THEN %(now_ms)s
                     ELSE last_live_at_ms
                   END,
                   last_error = %(error_code)s,
                   gap_unclosed = %(gap_unclosed)s,
                   gap_boundary_provider_record_id = CASE
                     WHEN %(gap_unclosed)s THEN COALESCE(
                       source.gap_boundary_provider_record_id,
                       %(gap_boundary_provider_record_id)s,
                       (
                         SELECT item.provider_record_id
                           FROM news_items item
                          WHERE item.source_id = source.source_id
                          ORDER BY item.last_observed_at_ms DESC, item.item_id DESC
                          LIMIT 1
                       )
                     )
                     ELSE NULL
                   END,
                   gap_version = source.gap_version + CASE WHEN %(gap_unclosed)s THEN 1 ELSE 0 END,
                   updated_at_ms = %(now_ms)s
             WHERE source.source_id = %(source_id)s
               AND source.source_kind = 'opennews'
               AND (
                 %(gap_unclosed)s
                 OR source.gap_version = %(expected_gap_version)s
               )
             RETURNING source.gap_boundary_provider_record_id, source.gap_version
            """,
            {
                "connected": connected,
                "now_ms": now_ms,
                "error_code": error_code,
                "gap_unclosed": gap_unclosed,
                "gap_boundary_provider_record_id": gap_boundary_provider_record_id,
                "expected_gap_version": expected_gap_version,
                "source_id": source_id,
            },
        ).fetchone()
        if row is None:
            return None
        boundary = row["gap_boundary_provider_record_id"]
        return (
            str(boundary) if boundary is not None else None,
            int(row["gap_version"]),
        )

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
                   gap_unclosed = true,
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

    def rebuild_stories(self, *, now_ms: int) -> dict[str, Any]:
        from .projection import NewsProjectionSnapshot, compute_news_story_projection

        payload = self.load_story_projection(now_ms=now_ms)
        snapshot = NewsProjectionSnapshot(
            input_fingerprint=str(payload["input_fingerprint"]),
            cutoff_ms=int(payload["cutoff_ms"]),
            scoring_epoch_ms=int(payload["scoring_epoch_ms"]),
            current_input_fingerprint=(
                str(payload["current_input_fingerprint"]) if payload.get("current_input_fingerprint") else None
            ),
            rows=tuple(dict(row) for row in payload["rows"]),
        )
        if snapshot.unchanged:
            return {
                "projection_status": "unchanged_input",
                "items": len(snapshot.rows),
                "stories": 0,
                "rows_written": 0,
            }
        projection = compute_news_story_projection(snapshot)
        return self.publish_story_projection(
            snapshot=snapshot,
            projection=projection,
            now_ms=now_ms,
        )

    def _story_invariant_counts(self) -> dict[str, int]:
        row = self.conn.execute(
            """
            WITH current_owners AS (
              SELECT i.item_id, count(m.story_id) AS owner_count
                FROM news_items i
                LEFT JOIN news_story_members m
                  ON m.item_id = i.item_id
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
                     count(DISTINCT i.reporting_origin) AS actual_source_count,
                     min(i.published_at_ms) AS actual_first_at_ms,
                     max(i.published_at_ms) AS actual_last_at_ms,
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
                     SELECT count(*) FROM news_stories
                   ),
                   unmaterialized_item_count = (
                     SELECT count(*)
                     FROM news_items item
                     WHERE item.active
                       AND NOT EXISTS (
                         SELECT 1
                         FROM news_story_members member
                         WHERE member.item_id = item.item_id
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
                     ORDER BY last_published_at_ms DESC,
                              importance_score DESC,
                              story_id
                     LIMIT 1
                   ),
                   last_material_change_at_ms = (
                     SELECT max(updated_at_ms)
                     FROM news_stories
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

    def refresh_brief_selection(self, *, now_ms: int) -> int:
        candidates = self.conn.execute(
            """
            SELECT story.story_id, story.importance_score,
                   story.last_published_at_ms,
                   item.reporting_origin AS representative_source_name
              FROM news_stories story
              JOIN news_items item
                ON item.item_id = story.representative_item_id
             WHERE NOT item.brief_excluded
             ORDER BY story.importance_score DESC,
                      story.last_published_at_ms DESC,
                      story.story_id
            """
        ).fetchall()
        desired = [str(row["story_id"]) for row in select_top_stories(candidates, limit=8, max_per_source=3)]
        existing = [
            str(row["story_id"])
            for row in self.conn.execute(
                """
                SELECT story_id
                FROM news_brief_selection_current
                ORDER BY rank
                """
            ).fetchall()
        ]
        writes = 0
        if desired != existing:
            writes = int(self.conn.execute("DELETE FROM news_brief_selection_current").rowcount or 0)
            for rank, story_id in enumerate(desired, start=1):
                writes += int(
                    self.conn.execute(
                        """
                        INSERT INTO news_brief_selection_current (
                          rank, story_id, updated_at_ms
                        )
                        VALUES (%s, %s, %s)
                        """,
                        (rank, story_id, int(now_ms)),
                    ).rowcount
                    or 0
                )
        candidates = self.brief_candidates()
        fingerprint = brief_fingerprint(candidates)
        writes += self._schedule_brief_target(
            fingerprint=fingerprint,
            now_ms=now_ms,
        )
        return writes

    def _schedule_brief_target(self, *, fingerprint: str, now_ms: int) -> int:
        publication = self.conn.execute(
            """
            SELECT publication_id
              FROM news_brief_publications
             WHERE fingerprint = %s
            """,
            (fingerprint,),
        ).fetchone()
        run = self.conn.execute(
            """
            SELECT run_id, status
              FROM news_brief_runs
             WHERE fingerprint = %s
            """,
            (fingerprint,),
        ).fetchone()
        terminal_run = run is not None and str(run["status"]) in {
            "ready",
            "insufficient_material",
            "failed",
        }
        if publication is not None or terminal_run:
            cursor = self.conn.execute(
                """
                UPDATE news_brief_current
                   SET target_fingerprint = %s,
                       publication_id = COALESCE(%s, publication_id),
                       latest_run_id = COALESCE(%s, latest_run_id),
                       pending_first_dirty_at_ms = NULL,
                       pending_due_at_ms = NULL,
                       updated_at_ms = %s
                 WHERE singleton_key
                   AND (
                     target_fingerprint IS DISTINCT FROM %s
                     OR publication_id IS DISTINCT FROM COALESCE(%s, publication_id)
                     OR latest_run_id IS DISTINCT FROM COALESCE(%s, latest_run_id)
                     OR pending_first_dirty_at_ms IS NOT NULL
                     OR pending_due_at_ms IS NOT NULL
                   )
                """,
                (
                    fingerprint,
                    publication["publication_id"] if publication is not None else None,
                    run["run_id"] if run is not None else None,
                    now_ms,
                    fingerprint,
                    publication["publication_id"] if publication is not None else None,
                    run["run_id"] if run is not None else None,
                ),
            )
            return int(cursor.rowcount or 0)
        cursor = self.conn.execute(
            """
            UPDATE news_brief_current
               SET target_fingerprint = %s,
                   pending_first_dirty_at_ms = COALESCE(pending_first_dirty_at_ms, %s),
                   pending_due_at_ms = COALESCE(pending_due_at_ms, %s),
                   updated_at_ms = %s
             WHERE singleton_key
               AND (
                 target_fingerprint IS DISTINCT FROM %s
                 OR pending_first_dirty_at_ms IS NULL
                 OR pending_due_at_ms IS NULL
               )
            """,
            (fingerprint, now_ms, now_ms + 600_000, now_ms, fingerprint),
        )
        return int(cursor.rowcount or 0)

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
        where = ["true"]
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
            SELECT st.*, representative.reporting_origin
                         AS representative_source_name
              FROM news_stories st
              JOIN news_items representative
                ON representative.item_id = st.representative_item_id
             WHERE {" AND ".join(where)}
             ORDER BY {order}
             LIMIT %s
            """,
            (*params, limit + 1),
        ).fetchall()
        has_more = len(rows) > limit
        page = rows[:limit]
        provider_evidence = self.story_provider_evidence(
            story_ids=[str(row["story_id"]) for row in page]
        )
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
            story["provider_evidence"] = _public_provider_evidence(
                provider_evidence.get(str(row["story_id"]))
            )
            stories.append(story)
        return {
            "sort": sort,
            "filters": filters,
            "stories": stories,
            "next_cursor": next_cursor,
            "has_more": has_more,
            "facets": self._feed_facets(),
        }

    def _feed_facets(self) -> dict[str, Any]:
        categories = self.conn.execute(
            """
            SELECT facet_value AS value, story_count AS count
              FROM news_story_facet_counts
             WHERE facet_type = 'category'
             ORDER BY story_count DESC, facet_value
             LIMIT %s
            """,
            (_PUBLIC_LIST_LIMIT + 1,),
        ).fetchall()
        levels = self.conn.execute(
            """
            SELECT facet_value AS value, story_count AS count
              FROM news_story_facet_counts
             WHERE facet_type = 'level'
             ORDER BY story_count DESC, facet_value
             LIMIT %s
            """,
            (_PUBLIC_LIST_LIMIT + 1,),
        ).fetchall()
        sources = self.conn.execute(
            """
            SELECT src.source_id AS value, src.name AS label,
                   facet.story_count AS count
              FROM news_source_facet_counts facet
              JOIN news_sources src ON src.source_id = facet.source_id
             ORDER BY facet.story_count DESC, src.name, src.source_id
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
            SELECT st.*, representative.reporting_origin
                         AS representative_source_name
              FROM news_stories st
              JOIN news_items representative
                ON representative.item_id = st.representative_item_id
             WHERE st.story_id = %s
            """,
            (story_id,),
        ).fetchone()
        if row is None:
            return None
        provider_evidence = _public_provider_evidence(
            self.story_provider_evidence(story_ids=(story_id,)).get(story_id)
        )
        member_where = ["m.story_id = %s"]
        member_params: list[Any] = [story_id]
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
            member_where.append(
                """
                (
                  i.published_at_ms < %s
                  OR (
                    i.published_at_ms = %s
                    AND i.item_id > %s
                  )
                )
                """
            )
            member_params.extend(
                [
                    published_at_ms,
                    published_at_ms,
                    item_id,
                ]
            )
        member_params.append(members_limit + 1)
        members = self.conn.execute(
            f"""
            SELECT i.*, i.reporting_origin AS source_name, src.tier
              FROM news_story_members m
              JOIN news_items i ON i.item_id = m.item_id
              JOIN news_sources src ON src.source_id = i.source_id
             WHERE {" AND ".join(member_where)}
             ORDER BY i.published_at_ms DESC, i.item_id
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
                    "published_at_ms": int(last["published_at_ms"]),
                    "item_id": str(last["item_id"]),
                }
            )
        return {
            **_story_summary(row),
            "provider_evidence": provider_evidence,
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
            SELECT s.source_id, s.name, s.source_kind, s.tier,
                   s.enabled, s.live_connected, s.last_live_at_ms,
                   s.last_recovery_at_ms, s.gap_unclosed,
                   s.last_success_at_ms, s.last_http_status,
                   s.consecutive_failures, s.last_error
              FROM news_sources s
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
            SELECT story.*,
                   representative.reporting_origin AS representative_source_name
            FROM news_brief_selection_current selection
            JOIN news_stories story
              ON story.story_id = selection.story_id
            JOIN news_items representative
              ON representative.item_id = story.representative_item_id
            ORDER BY selection.rank
            """
        ).fetchall()
        return select_top_stories(rows, limit=8, max_per_source=3)

    def peek_brief_candidate(self, *, now_ms: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT current.target_fingerprint,
                   current.pending_first_dirty_at_ms,
                   current.pending_due_at_ms,
                   run.run_id,
                   run.status AS run_status,
                   run.next_due_at_ms,
                   run.lease_expires_at_ms
              FROM news_brief_current current
              LEFT JOIN news_brief_runs run
                ON run.fingerprint = current.target_fingerprint
             WHERE current.singleton_key
               AND (
                 (
                   run.status = 'retryable'
                   AND run.next_due_at_ms <= %s
                 )
                 OR (
                   run.status = 'running'
                   AND run.lease_expires_at_ms <= %s
                 )
                 OR (
                   current.pending_due_at_ms <= %s
                   AND (run.run_id IS NULL OR run.status NOT IN ('ready', 'insufficient_material', 'failed'))
                 )
               )
             LIMIT 1
            """,
            (now_ms, now_ms, now_ms),
        ).fetchone()
        return dict(row) if row is not None else None

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
              created_at_ms, updated_at_ms, completed_at_ms, next_due_at_ms
            )
            VALUES (%s, %s, 'insufficient_material', 0, %s, %s, %s, %s, %s, NULL)
            ON CONFLICT (fingerprint) DO UPDATE SET
              status = 'insufficient_material',
              candidate_story_count = EXCLUDED.candidate_story_count,
              candidate_source_count = EXCLUDED.candidate_source_count,
              lease_owner = NULL,
              lease_expires_at_ms = NULL,
              heartbeat_at_ms = NULL,
              last_error = NULL,
              next_due_at_ms = NULL,
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
                   pending_first_dirty_at_ms = NULL,
                   pending_due_at_ms = NULL,
                   updated_at_ms = %s
             WHERE singleton_key
               AND target_fingerprint = %s
               AND (
                 latest_run_id IS DISTINCT FROM %s
                 OR pending_first_dirty_at_ms IS NOT NULL
                 OR pending_due_at_ms IS NOT NULL
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
    ) -> dict[str, Any] | None:
        self.conn.execute("SELECT pg_advisory_xact_lock(%s)", (_BRIEF_LOCK_KEY,))
        current = self.conn.execute(
            """
            SELECT pending_due_at_ms
              FROM news_brief_current
             WHERE singleton_key AND target_fingerprint = %s
            """,
            (fingerprint,),
        ).fetchone()
        release_due_at_ms = int((current or {}).get("pending_due_at_ms") or now_ms)
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
                           next_due_at_ms = NULL,
                           updated_at_ms = %s,
                           completed_at_ms = %s
                     WHERE run_id = %s
                       AND status <> 'failed'
                    """,
                    (now_ms, now_ms, str(row["run_id"])),
                )
                self.conn.execute(
                    """
                    UPDATE news_brief_current
                       SET pending_first_dirty_at_ms = NULL,
                           pending_due_at_ms = NULL,
                           latest_run_id = %s,
                           updated_at_ms = %s
                     WHERE singleton_key AND target_fingerprint = %s
                    """,
                    (str(row["run_id"]), now_ms, fingerprint),
                )
                return None
            run_id = str(row["run_id"])
            attempt_count = int(row["attempt_count"])
            release_due_at_ms = int(row.get("next_due_at_ms") or release_due_at_ms)
        else:
            run_id = deterministic_id("brief_run", fingerprint)
            attempt_count = 0
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
              next_due_at_ms = NULL,
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
        current = self.conn.execute(
            """
            UPDATE news_brief_current
               SET latest_run_id = %s,
                   updated_at_ms = %s
             WHERE singleton_key
               AND target_fingerprint = %s
            """,
            (run_id, now_ms, fingerprint),
        )
        if int(current.rowcount or 0) != 1:
            self.conn.execute(
                """
                UPDATE news_brief_runs
                   SET status = 'retryable',
                       next_due_at_ms = %s,
                       lease_owner = NULL,
                       lease_expires_at_ms = NULL,
                       heartbeat_at_ms = NULL,
                       updated_at_ms = %s
                 WHERE run_id = %s AND lease_owner = %s
                """,
                (now_ms, now_ms, run_id, owner),
            )
            return None
        return {
            "run_id": run_id,
            "lease_owner": owner,
            "fingerprint": fingerprint,
            "attempt_count": attempt_count,
            "release_due_at_ms": release_due_at_ms,
        }

    def start_brief_model(
        self,
        *,
        run_id: str,
        lease_owner: str,
        now_ms: int,
        max_attempts: int,
    ) -> bool:
        cursor = self.conn.execute(
            """
            UPDATE news_brief_runs
               SET attempt_count = attempt_count + 1,
                   heartbeat_at_ms = %s,
                   updated_at_ms = %s
             WHERE run_id = %s
               AND status = 'running'
               AND lease_owner = %s
               AND lease_expires_at_ms > %s
               AND attempt_count < %s
            """,
            (now_ms, now_ms, run_id, lease_owner, now_ms, max_attempts),
        )
        return int(cursor.rowcount or 0) == 1

    def release_brief_claim(
        self,
        *,
        run_id: str,
        lease_owner: str,
        due_at_ms: int,
        now_ms: int,
    ) -> bool:
        cursor = self.conn.execute(
            """
            UPDATE news_brief_runs
               SET status = 'retryable',
                   next_due_at_ms = %s,
                   lease_owner = NULL,
                   lease_expires_at_ms = NULL,
                   heartbeat_at_ms = NULL,
                   updated_at_ms = %s
             WHERE run_id = %s
               AND status = 'running'
               AND lease_owner = %s
            """,
            (due_at_ms, now_ms, run_id, lease_owner),
        )
        return int(cursor.rowcount or 0) == 1

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
    ) -> str | None:
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
              FROM news_brief_runs run
              JOIN news_brief_current current
                ON current.singleton_key
               AND current.target_fingerprint = run.fingerprint
             WHERE run.run_id = %s
               AND run.fingerprint = %s
               AND run.status = 'running'
               AND run.lease_owner = %s
               AND run.lease_expires_at_ms > %s
             FOR UPDATE OF run, current
            """,
            (run_id, fingerprint, lease_owner, now_ms),
        ).fetchone()
        if claimed is None:
            return None
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
                   next_due_at_ms = NULL,
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
                   latest_run_id = %s,
                   pending_first_dirty_at_ms = NULL,
                   pending_due_at_ms = NULL,
                   updated_at_ms = %s
             WHERE singleton_key AND target_fingerprint = %s
            """,
            (publication_id, run_id, now_ms, fingerprint),
        )
        return publication_id

    def fail_brief_run(
        self,
        *,
        run_id: str,
        lease_owner: str,
        error: Exception,
        now_ms: int,
        max_attempts: int = 3,
        retry_delay_ms: int = 300_000,
    ) -> str | None:
        row = self.conn.execute(
            """
            SELECT fingerprint, attempt_count
              FROM news_brief_runs
             WHERE run_id = %s
               AND status = 'running'
               AND lease_owner = %s
             FOR UPDATE
            """,
            (run_id, lease_owner),
        ).fetchone()
        if row is None:
            return None
        exhausted = int(row["attempt_count"]) >= int(max_attempts)
        status = "failed" if exhausted else "retryable"
        next_due_at_ms = None if exhausted else int(now_ms) + int(retry_delay_ms)
        self.conn.execute(
            """
            UPDATE news_brief_runs
               SET status = %s,
                   lease_owner = NULL,
                   lease_expires_at_ms = NULL,
                   heartbeat_at_ms = %s,
                   last_error = %s,
                   next_due_at_ms = %s,
                   completed_at_ms = CASE WHEN %s THEN %s ELSE NULL END,
                   updated_at_ms = %s
             WHERE run_id = %s
               AND status = 'running'
               AND lease_owner = %s
            """,
            (
                status,
                now_ms,
                f"{type(error).__name__}:{str(error)[:1000]}",
                next_due_at_ms,
                exhausted,
                now_ms,
                now_ms,
                run_id,
                lease_owner,
            ),
        )
        if exhausted:
            self.conn.execute(
                """
                UPDATE news_brief_current
                   SET latest_run_id = %s,
                       pending_first_dirty_at_ms = NULL,
                       pending_due_at_ms = NULL,
                       updated_at_ms = %s
                 WHERE singleton_key AND target_fingerprint = %s
                """,
                (run_id, now_ms, str(row["fingerprint"])),
            )
            terminal_row = self.conn.execute(
                "SELECT * FROM news_brief_runs WHERE run_id = %s",
                (run_id,),
            ).fetchone()
            if terminal_row is None:
                raise RuntimeError("news_brief_terminal_row_missing")
            source_row = {**dict(terminal_row), "native_target_key": str(row["fingerprint"])}
            terminalize_source_row(
                self.conn,
                owner_key="news_brief",
                source_table="news_brief_runs",
                target_key=str(row["fingerprint"]),
                source_row=source_row,
                final_status="failed",
                final_reason=str(source_row.get("last_error") or "brief_attempts_exhausted"),
                final_reason_bucket="model_attempts_exhausted",
                now_ms=int(now_ms),
                attempt_count=int(source_row.get("attempt_count") or 0),
            )
        else:
            self.conn.execute(
                """
                UPDATE news_brief_current
                   SET latest_run_id = %s,
                       pending_due_at_ms = %s,
                       updated_at_ms = %s
                 WHERE singleton_key AND target_fingerprint = %s
                """,
                (run_id, next_due_at_ms, now_ms, str(row["fingerprint"])),
            )
        return status

    def get_brief(self, *, now_ms: int, history_limit: int = 20) -> dict[str, Any]:
        candidates = self.brief_candidates()
        fingerprint = brief_fingerprint(candidates)
        candidate_sources = {str(candidate["representative_source_name"]) for candidate in candidates}
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

    # News Story push ----------------------------------------------------------

    def story_provider_evidence(
        self,
        *,
        story_ids: Sequence[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Return each Story's highest numeric provider-score item.

        Selection is deterministic: numeric score descending, then newest
        publication, then item identity. The returned ``provider_evidence``
        shape is also the reusable public Feed seam; this method does not
        mutate the Feed contract itself.
        """

        if story_ids is not None and not story_ids:
            return {}
        story_filter = ""
        params: tuple[Any, ...] = ()
        if story_ids is not None:
            story_filter = "AND member.story_id = ANY(%s)"
            params = (list(story_ids),)
        rows = self.conn.execute(
            f"""
            SELECT DISTINCT ON (member.story_id)
                   member.story_id,
                   story.importance_score,
                   story.item_count,
                   story.source_count,
                   story.first_published_at_ms,
                   story.last_published_at_ms,
                   item.item_id,
                   item.canonical_url,
                   item.provider_metadata,
                   item.reporting_origin,
                   item.title,
                   item.description,
                   item.lang,
                   item.published_at_ms,
                   item.updated_at_ms AS threshold_observed_at_ms,
                   (item.provider_metadata ->> 'score')::numeric
                     AS provider_score
              FROM news_story_members member
              JOIN news_stories story ON story.story_id = member.story_id
              JOIN news_items item ON item.item_id = member.item_id
             WHERE jsonb_typeof(item.provider_metadata -> 'score') = 'number'
                   {story_filter}
             ORDER BY member.story_id,
                      (item.provider_metadata ->> 'score')::numeric DESC,
                      item.published_at_ms DESC,
                      item.item_id
            """,
            params,
        ).fetchall()
        return {
            str(row["story_id"]): {
                "story_id": str(row["story_id"]),
                "importance_score": int(row["importance_score"]),
                "item_count": int(row["item_count"]),
                "source_count": int(row["source_count"]),
                "first_published_at_ms": int(row["first_published_at_ms"]),
                "last_published_at_ms": int(row["last_published_at_ms"]),
                "provider_evidence": {
                    "item_id": str(row["item_id"]),
                    "url": str(row["canonical_url"]) if row["canonical_url"] else None,
                    "provider_metadata": dict(row["provider_metadata"] or {}),
                    "reporting_origin": str(row["reporting_origin"]),
                    "title": str(row["title"]),
                    "description": str(row["description"]),
                    "lang": str(row["lang"]),
                    "published_at_ms": int(row["published_at_ms"]),
                    "threshold_observed_at_ms": int(row["threshold_observed_at_ms"]),
                    "provider_score": float(row["provider_score"]),
                },
            }
            for row in rows
        }

    def initialize_push_baseline(self, *, now_ms: int) -> tuple[int, bool]:
        row = self.conn.execute(
            """
            SELECT baseline_at_ms
              FROM news_push_state
             WHERE singleton_key = 'current'
             FOR UPDATE
            """
        ).fetchone()
        if row is None:
            raise RuntimeError("news_push_state_missing")
        baseline_at_ms = row["baseline_at_ms"]
        if baseline_at_ms is not None:
            return int(baseline_at_ms), False
        self.conn.execute(
            """
            UPDATE news_push_state
               SET baseline_at_ms = %s, updated_at_ms = %s
             WHERE singleton_key = 'current' AND baseline_at_ms IS NULL
            """,
            (int(now_ms), int(now_ms)),
        )
        return int(now_ms), True

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
        cursor = self.conn.execute(
            """
            INSERT INTO news_push_deliveries (
              story_id, selected_item_id, provider_score,
              threshold_observed_at_ms, source_payload, delivery_payload,
              payload_fingerprint, translation_status, status,
              delivery_attempts, next_attempt_at_ms, lease_owner,
              lease_token, lease_expires_at_ms, receipt, last_error,
              sent_at_ms, created_at_ms, updated_at_ms
            ) VALUES (
              %s, %s, %s, %s, %s, NULL, NULL, %s, %s,
              0, %s, NULL, NULL, NULL, NULL, NULL, NULL, %s, %s
            )
            ON CONFLICT (story_id) DO NOTHING
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
            ),
        )
        return bool(cursor.rowcount)

    def terminalize_exhausted_push_deliveries(
        self,
        *,
        now_ms: int,
        max_attempts: int,
    ) -> int:
        cursor = self.conn.execute(
            """
            UPDATE news_push_deliveries
               SET status = 'terminal',
                   next_attempt_at_ms = NULL,
                   lease_owner = NULL,
                   lease_token = NULL,
                   lease_expires_at_ms = NULL,
                   last_error = COALESCE(last_error, 'delivery_attempt_limit_exhausted'),
                   updated_at_ms = %s
             WHERE status IN ('pending_delivery', 'retry_wait')
               AND delivery_attempts >= %s
               AND (lease_expires_at_ms IS NULL OR lease_expires_at_ms <= %s)
            """,
            (int(now_ms), int(max_attempts), int(now_ms)),
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
            UPDATE news_push_deliveries
               SET lease_owner = %s,
                   lease_token = %s,
                   lease_expires_at_ms = %s,
                   updated_at_ms = %s
             WHERE story_id = %s
               AND status IN (
                     'pending_translation', 'pending_delivery', 'retry_wait'
                   )
               AND next_attempt_at_ms <= %s
               AND delivery_attempts < %s
               AND (lease_expires_at_ms IS NULL OR lease_expires_at_ms <= %s)
            RETURNING *
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

    def record_push_translation(
        self,
        *,
        story_id: str,
        lease_token: str,
        translation_status: str,
        delivery_payload: Mapping[str, Any],
        payload_fingerprint: str,
        now_ms: int,
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            UPDATE news_push_deliveries
               SET translation_status = %s,
                   delivery_payload = %s,
                   payload_fingerprint = %s,
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
                int(now_ms),
                int(now_ms),
                story_id,
                lease_token,
            ),
        ).fetchone()
        return dict(row) if row is not None else None

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
        cursor = self.conn.execute(
            """
            UPDATE news_push_deliveries
               SET translation_status = %s,
                   delivery_payload = %s,
                   payload_fingerprint = %s,
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
            """,
            (
                translation_status,
                Jsonb(dict(delivery_payload)),
                payload_fingerprint,
                str(error_code)[:500],
                int(now_ms),
                story_id,
                lease_token,
            ),
        )
        return bool(cursor.rowcount)

    def start_push_delivery_attempt(
        self,
        *,
        story_id: str,
        lease_token: str,
        now_ms: int,
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            UPDATE news_push_deliveries
               SET status = 'pending_delivery',
                   delivery_attempts = delivery_attempts + 1,
                   updated_at_ms = %s
             WHERE story_id = %s
               AND lease_token = %s
               AND status IN ('pending_delivery', 'retry_wait')
               AND delivery_payload IS NOT NULL
               AND payload_fingerprint IS NOT NULL
            RETURNING *
            """,
            (int(now_ms), story_id, lease_token),
        ).fetchone()
        return dict(row) if row is not None else None

    def complete_push_delivery(
        self,
        *,
        story_id: str,
        lease_token: str,
        receipt: Mapping[str, Any],
        now_ms: int,
    ) -> bool:
        cursor = self.conn.execute(
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
            """,
            (Jsonb(dict(receipt)), int(now_ms), int(now_ms), story_id, lease_token),
        )
        return bool(cursor.rowcount)

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
            RETURNING status
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
        return str(row["status"]) if row is not None else None

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
        state = self.conn.execute(
            """
            SELECT baseline_at_ms, updated_at_ms
              FROM news_push_state
             WHERE singleton_key = 'current'
            """
        ).fetchone()
        if state is None:
            raise RuntimeError("news_push_state_missing")
        aggregate = self.conn.execute(
            """
            SELECT count(*) AS total_count,
                   count(*) FILTER (WHERE status = 'suppressed')
                     AS suppressed_count,
                   count(*) FILTER (
                     WHERE status IN ('pending_translation', 'pending_delivery')
                   ) AS pending_count,
                   count(*) FILTER (WHERE status = 'retry_wait')
                     AS retry_count,
                   count(*) FILTER (WHERE status = 'sent') AS sent_count,
                   count(*) FILTER (WHERE status = 'terminal')
                     AS terminal_count,
                   min(next_attempt_at_ms) FILTER (
                     WHERE status IN (
                       'pending_translation', 'pending_delivery', 'retry_wait'
                     )
                   ) AS oldest_due_at_ms,
                   max(sent_at_ms) AS latest_sent_at_ms
              FROM news_push_deliveries
            """
        ).fetchone()
        latest_error = self.conn.execute(
            """
            SELECT last_error, updated_at_ms
              FROM news_push_deliveries
             WHERE last_error IS NOT NULL
             ORDER BY updated_at_ms DESC, story_id
             LIMIT 1
            """
        ).fetchone()
        baseline_at_ms = state["baseline_at_ms"]
        retry_count = int(aggregate["retry_count"] or 0)
        terminal_count = int(aggregate["terminal_count"] or 0)
        status = (
            "warming"
            if baseline_at_ms is None
            else "degraded"
            if retry_count or terminal_count
            else "ready"
        )
        return {
            "status": status,
            "initialized": baseline_at_ms is not None,
            "baseline_at_ms": int(baseline_at_ms) if baseline_at_ms is not None else None,
            "total_count": int(aggregate["total_count"] or 0),
            "suppressed_count": int(aggregate["suppressed_count"] or 0),
            "pending_count": int(aggregate["pending_count"] or 0),
            "retry_count": retry_count,
            "sent_count": int(aggregate["sent_count"] or 0),
            "terminal_count": terminal_count,
            "oldest_due_at_ms": aggregate["oldest_due_at_ms"],
            "latest_sent_at_ms": aggregate["latest_sent_at_ms"],
            "latest_error": (
                _public_push_error(str(latest_error["last_error"]))
                if latest_error is not None
                else None
            ),
            "latest_error_at_ms": latest_error["updated_at_ms"] if latest_error is not None else None,
            "measured_at_ms": int(now_ms),
        }

    # Health -------------------------------------------------------------------

    def health_snapshot(
        self,
        *,
        now_ms: int,
        push_enabled: bool = False,
        feishu_webhook_url_configured: bool = False,
        feishu_signing_secret_configured: bool = False,
    ) -> dict[str, Any]:
        opennews = self.conn.execute(
            """
            SELECT source_id, name, live_connected, last_live_at_ms,
                   last_recovery_at_ms, gap_unclosed, last_error,
                   last_http_status, last_success_at_ms,
                   consecutive_failures
              FROM news_sources
             WHERE source_kind = 'opennews' AND enabled
             ORDER BY source_id
             LIMIT 1
            """
        ).fetchone()
        story = self.conn.execute(
            """
            SELECT active_story_count AS active_count,
                   newest_story_at_ms,
                   last_material_change_at_ms,
                   active_item_count,
                   newest_item_at_ms,
                   unmaterialized_item_count,
                   invalid_owner_count,
                   invalid_story_aggregate_count,
                   last_attempt_at_ms,
                   last_error
              FROM news_projection_summary
             WHERE singleton_key = 'current'
            """
        ).fetchone()
        brief = self.get_brief(now_ms=now_ms, history_limit=1)
        ingest_reasons: list[str] = []
        opennews_payload = dict(opennews) if opennews is not None else None
        if opennews_payload is None:
            ingest_status = "degraded"
            ingest_reasons.append("opennews_not_configured")
        elif opennews_payload["last_success_at_ms"] is None:
            ingest_status = "warming"
            ingest_reasons.append("opennews_no_items_yet")
        elif not bool(opennews_payload["live_connected"]):
            ingest_status = "degraded"
            ingest_reasons.append("opennews_disconnected")
        elif bool(opennews_payload["gap_unclosed"]):
            ingest_status = "degraded"
            ingest_reasons.append("opennews_gap_unclosed")
        elif opennews_payload["last_error"] is not None:
            ingest_status = "degraded"
            ingest_reasons.append("opennews_error")
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
        elif story["last_error"] is not None:
            story_status = "degraded"
            story_reasons.append(str(story["last_error"]))
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
        push_snapshot = self.push_health_snapshot(now_ms=now_ms)
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
            push_status = (
                "degraded"
                if (
                    not feishu_webhook_url_configured or push_snapshot["retry_count"] or push_snapshot["terminal_count"]
                )
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
                "unmaterialized_item_count": unmaterialized,
                "invalid_owner_count": invalid_owners,
                "invalid_story_aggregate_count": invalid_aggregates,
                "invariant_error_count": invalid_owners + invalid_aggregates,
                "identity_version": STORY_IDENTITY_VERSION,
                "classifier_version": CLASSIFIER_VERSION,
                "importance_version": IMPORTANCE_VERSION,
                "last_attempt_at_ms": story["last_attempt_at_ms"],
                "last_error": story["last_error"],
            },
            "brief": {
                "status": brief_status,
                "reasons": brief_reasons,
                "public_state": brief["state"],
                "target_fingerprint": brief["target_fingerprint"],
                "publication_id": (brief["publication"]["publication_id"] if brief["publication"] else None),
                "latest_run": brief["latest_run"],
            },
            "push": push_payload,
        }
        statuses = [ingest_status, story_status, brief_status]
        if push_enabled:
            statuses.append(push_status)
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


def _public_provider_evidence(selected: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if selected is None:
        return None
    evidence = selected.get("provider_evidence")
    if not isinstance(evidence, Mapping):
        return None
    return {
        "item_id": str(evidence["item_id"]),
        "url": str(evidence["url"]) if evidence.get("url") else None,
        "provider_metadata": dict(evidence.get("provider_metadata") or {}),
    }


def _public_push_error(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if re.fullmatch(r"[a-z0-9_]{1,120}", normalized):
        return normalized
    return "news_story_push_delivery_error"


def _item_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "item_id": str(row["item_id"]),
        "provider_record_id": (
            str(row["provider_record_id"])
            if row.get("provider_record_id") is not None
            else None
        ),
        "provider_metadata": dict(row.get("provider_metadata") or {}),
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


def _story_facet_counter(value: Any) -> Counter[tuple[str, str]]:
    rows = value if isinstance(value, list) else []
    counts: Counter[tuple[str, str]] = Counter()
    for raw in rows:
        row = dict(raw)
        counts[("category", str(row["category"]))] += 1
        counts[("level", str(row["level"]))] += 1
    return counts


def _source_facet_counter(value: Any) -> Counter[str]:
    rows = value if isinstance(value, list) else []
    pairs = {(str(dict(raw)["story_id"]), str(dict(raw)["source_id"])) for raw in rows}
    return Counter(source_id for _, source_id in pairs)


__all__ = ["NewsRepository", "deterministic_id"]
