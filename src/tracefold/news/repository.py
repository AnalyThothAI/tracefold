from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from math import ceil, isfinite
from typing import Any, cast
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
from .presentation import normalize_news_display_text, normalize_news_display_title
from .ranking import (
    is_delayed_brief_excluded,
    select_top_stories,
)
from .title_translation import (
    TITLE_TRANSLATION_LOCALE,
    TITLE_TRANSLATION_PROMPT_VERSION,
    TITLE_TRANSLATION_WORKFLOW_VERSION,
    looks_zh_cn_title,
    story_title_fingerprint,
)

_BRIEF_LOCK_KEY = 727_301_985
_ACTIVE_WINDOW_MS = 96 * 60 * 60 * 1000
_STORY_ACTIVE_WINDOW_MS = 12 * 60 * 60 * 1000
_NEWS_STALL_AFTER_MS = 120_000
_SLO_WINDOW_MS = 24 * 60 * 60 * 1000
_BRIEF_LEASE_MS = 120_000
_PUBLIC_LIST_LIMIT = 100
_TITLE_TRANSLATION_PROVIDER_SCORE_THRESHOLD = 70
_TITLE_TRANSLATION_TARGET_LIMIT = 10_000
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


def _feed_filter_where(
    *,
    category: str | None,
    level: str | None,
    source_id: str | None,
    reporting_origin: str | None,
    provider_score_gt: float | None,
    q: str | None,
) -> tuple[list[str], list[Any]]:
    """Build the bounded Story-membership predicate shared by rows and facets."""

    where = ["true"]
    params: list[Any] = []
    if category:
        where.append("st.category = %s")
        params.append(category)
    if level:
        where.append("st.level = %s")
        params.append(level)
    if source_id:
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
        params.append(source_id)
    if reporting_origin:
        where.append(
            """
            EXISTS (
              SELECT 1
                FROM news_story_members fm
                JOIN news_items fi ON fi.item_id = fm.item_id
               WHERE fm.story_id = st.story_id
                 AND lower(btrim(fi.reporting_origin)) = %s
            )
            """
        )
        params.append(reporting_origin)
    if provider_score_gt is not None:
        where.append(
            """
            EXISTS (
              SELECT 1
                FROM news_story_members fm
                JOIN news_items fi ON fi.item_id = fm.item_id
               WHERE fm.story_id = st.story_id
                 AND CASE
                       WHEN jsonb_typeof(fi.provider_metadata -> 'score') = 'number'
                         THEN (fi.provider_metadata ->> 'score')::numeric
                       ELSE NULL
                     END > %s
            )
            """
        )
        params.append(provider_score_gt)
    if q:
        where.append(
            """
            (
              EXISTS (
                SELECT 1
                  FROM news_story_members fm
                  JOIN news_items fi ON fi.item_id = fm.item_id
                 WHERE fm.story_id = st.story_id
                   AND (
                     strpos(lower(fi.title), %s) > 0
                     OR strpos(lower(fi.description), %s) > 0
                     OR strpos(lower(fi.reporting_origin), %s) > 0
                     OR strpos(
                       lower(coalesce(fi.provider_metadata ->> 'source', '')),
                       %s
                     ) > 0
                     OR EXISTS (
                       SELECT 1
                         FROM jsonb_array_elements(
                           CASE
                             WHEN jsonb_typeof(fi.provider_metadata -> 'coins') = 'array'
                               THEN fi.provider_metadata -> 'coins'
                             ELSE '[]'::jsonb
                           END
                         ) coin
                        WHERE strpos(lower(coalesce(coin ->> 'symbol', '')), %s) > 0
                     )
                   )
              )
              OR EXISTS (
                SELECT 1
                  FROM news_story_title_translations translation
                 WHERE translation.story_id = st.story_id
                   AND translation.source_raw_title_fingerprint = encode(
                     sha256(convert_to(st.representative_title, 'UTF8')),
                     'hex'
                   )
                   AND translation.locale = %s
                   AND translation.workflow_version = %s
                   AND translation.prompt_version = %s
                   AND translation.status = 'ready'
                   AND strpos(lower(translation.translated_title), %s) > 0
                   AND EXISTS (
                     SELECT 1
                       FROM news_story_members eligible_member
                       JOIN news_items eligible_item
                         ON eligible_item.item_id = eligible_member.item_id
                      WHERE eligible_member.story_id = st.story_id
                        AND jsonb_typeof(
                          eligible_item.provider_metadata -> 'score'
                        ) = 'number'
                        AND (
                          eligible_item.provider_metadata ->> 'score'
                        )::numeric > 70
                   )
              )
            )
            """
        )
        params.extend(
            [
                q,
                q,
                q,
                q,
                q,
                TITLE_TRANSLATION_LOCALE,
                TITLE_TRANSLATION_WORKFLOW_VERSION,
                TITLE_TRANSLATION_PROMPT_VERSION,
                q,
            ]
        )
    return where, params


class NewsRepository:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    @staticmethod
    def stable_json_hash(value: object) -> str:
        return _sha256_json(value)

    # Source inventory and acquisition -----------------------------------------

    def sync_source(self, source: NewsSourceDefinition, *, now_ms: int) -> None:
        self.conn.execute(
            """
            INSERT INTO news_sources (
              source_id, name, tier, lang, enabled, source_kind,
              created_at_ms, updated_at_ms
            )
            VALUES (
              %(source_id)s, %(name)s, %(tier)s, %(lang)s,
              %(enabled)s, %(source_kind)s, %(now_ms)s, %(now_ms)s
            )
            ON CONFLICT (source_id) DO UPDATE SET
              name = EXCLUDED.name,
              tier = EXCLUDED.tier,
              lang = EXCLUDED.lang,
              enabled = EXCLUDED.enabled,
              source_kind = EXCLUDED.source_kind,
              updated_at_ms = EXCLUDED.updated_at_ms
            WHERE (
              news_sources.name,
              news_sources.tier,
              news_sources.lang,
              news_sources.enabled,
              news_sources.source_kind
            ) IS DISTINCT FROM (
              EXCLUDED.name,
              EXCLUDED.tier,
              EXCLUDED.lang,
              EXCLUDED.enabled,
              EXCLUDED.source_kind
            )
            """,
            {**source.model_dump(), "now_ms": now_ms},
        )
        self.conn.execute(
            """
            UPDATE news_sources
               SET enabled = false, updated_at_ms = %s
             WHERE enabled AND source_id <> %s
            """,
            (now_ms, source.source_id),
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
                key: value for key, value in event.provider_metadata.items() if value not in (None, "", [], {})
            }
            if event.observation_kind == "provider_annotation":
                metadata = Jsonb(incoming_metadata)
                cursor = self.conn.execute(
                    """
                    UPDATE news_items
                       SET provider_score_updated_at_ms = CASE
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
            normalized_title = normalize_story_text(title)
            rejection = self._opennews_rejection_reason(
                title=title,
                normalized_title=normalized_title,
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
                "provider_score_updated_at_ms": (
                    observed_at_ms if _numeric_provider_score(incoming_metadata.get("score")) else None
                ),
                "canonical_url": canonical_url,
                "reporting_origin": reporting_origin,
                "title": title,
                "normalized_title": normalized_title,
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
                  provider_metadata, provider_score_updated_at_ms,
                  canonical_url, reporting_origin, title,
                  normalized_title, description, lang, published_at_ms,
                  first_observed_at_ms, last_observed_at_ms,
                  content_fingerprint, level, category,
                  classification_source, classification_confidence,
                  importance_score, importance_factors, brief_excluded,
                  active, created_at_ms, updated_at_ms
                ) VALUES (
                  %(item_id)s, %(source_id)s, %(source_item_key)s,
                  %(provider_record_id)s, %(provider_metadata)s,
                  %(provider_score_updated_at_ms)s,
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
        normalized_title: str,
        published_at_ms: int | None,
        now_ms: int,
    ) -> str | None:
        if not title:
            return "missing_title"
        if not normalized_title:
            return "unusable_title"
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
                   last_error = CASE
                     WHEN %(gap_unclosed)s AND %(error_code)s::text IS NULL
                       THEN source.last_error
                     ELSE %(error_code)s::text
                   END,
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
        from .projection import (
            NewsProjectionSnapshot,
            _require_bounded_snapshot,
            compute_news_story_projection,
        )

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
        _require_bounded_snapshot(snapshot)
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

    def story_title_translations(
        self,
        *,
        story_ids: Sequence[str],
    ) -> dict[str, dict[str, Any]]:
        """Read only translations attached to the exact current raw Story title."""

        if not story_ids:
            return {}
        rows = self.conn.execute(
            """
            SELECT translation.*
              FROM news_story_title_translations translation
              JOIN news_stories story
                ON story.story_id = translation.story_id
               AND translation.source_raw_title_fingerprint = encode(
                 sha256(convert_to(story.representative_title, 'UTF8')),
                 'hex'
               )
             WHERE translation.story_id = ANY(%s)
               AND translation.locale = %s
               AND translation.workflow_version = %s
               AND translation.prompt_version = %s
             ORDER BY translation.story_id
            """,
            (
                list(story_ids),
                TITLE_TRANSLATION_LOCALE,
                TITLE_TRANSLATION_WORKFLOW_VERSION,
                TITLE_TRANSLATION_PROMPT_VERSION,
            ),
        ).fetchall()
        return {str(row["story_id"]): dict(row) for row in rows}

    def list_feed(
        self,
        *,
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
        where, params = _feed_filter_where(
            category=normalized_category,
            level=normalized_level,
            source_id=normalized_source_id,
            reporting_origin=normalized_reporting_origin,
            provider_score_gt=normalized_provider_score_gt,
            q=normalized_query,
        )

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
        page_story_ids = [str(row["story_id"]) for row in page]
        provider_evidence = self.story_provider_evidence(story_ids=page_story_ids)
        title_translations = self.story_title_translations(story_ids=page_story_ids)
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
            story["push_delivery_state"] = _public_push_delivery_state(selected)
            story["title_translation"] = _public_story_title_translation(
                story=story,
                selected=selected,
                translation=title_translations.get(str(row["story_id"])),
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
        where, params = _feed_filter_where(
            category=category,
            level=level,
            source_id=source_id,
            reporting_origin=reporting_origin,
            provider_score_gt=provider_score_gt,
            q=q,
        )
        rows = self.conn.execute(
            f"""
            WITH filtered_stories AS MATERIALIZED (
              SELECT st.story_id, st.category, st.level
                FROM news_stories st
               WHERE {" AND ".join(where)}
            ),
            facet_rows AS (
              SELECT 'category'::text AS facet_type,
                     filtered.category AS value,
                     filtered.category AS label,
                     count(*)::integer AS count
                FROM filtered_stories filtered
               GROUP BY filtered.category
              UNION ALL
              SELECT 'level'::text AS facet_type,
                     filtered.level AS value,
                     filtered.level AS label,
                     count(*)::integer AS count
                FROM filtered_stories filtered
               GROUP BY filtered.level
              UNION ALL
              SELECT 'source'::text AS facet_type,
                     source.source_id AS value,
                     source.name AS label,
                     count(DISTINCT filtered.story_id)::integer AS count
                FROM filtered_stories filtered
                JOIN news_story_members member ON member.story_id = filtered.story_id
                JOIN news_items item ON item.item_id = member.item_id
                JOIN news_sources source ON source.source_id = item.source_id
               GROUP BY source.source_id, source.name
              UNION ALL
              SELECT 'reporting_origin'::text AS facet_type,
                     lower(btrim(item.reporting_origin)) AS value,
                     min(btrim(item.reporting_origin)) AS label,
                     count(DISTINCT filtered.story_id)::integer AS count
                FROM filtered_stories filtered
                JOIN news_story_members member ON member.story_id = filtered.story_id
                JOIN news_items item ON item.item_id = member.item_id
               WHERE nullif(btrim(item.reporting_origin), '') IS NOT NULL
               GROUP BY lower(btrim(item.reporting_origin))
            ),
            ranked AS (
              SELECT facet_rows.*,
                     row_number() OVER (
                       PARTITION BY facet_type
                       ORDER BY count DESC, value
                     ) AS position
                FROM facet_rows
            )
            SELECT facet_type, value, label, count, position
              FROM ranked
             WHERE position <= %s
             ORDER BY facet_type, position
            """,
            (*params, _PUBLIC_LIST_LIMIT + 1),
        ).fetchall()
        facets: dict[str, list[dict[str, Any]]] = {
            "category": [],
            "level": [],
            "source": [],
            "reporting_origin": [],
        }
        has_more = {facet_type: False for facet_type in facets}
        for row in rows:
            facet_type = str(row["facet_type"])
            if int(row["position"]) > _PUBLIC_LIST_LIMIT:
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
        selected = self.story_provider_evidence(story_ids=(story_id,)).get(story_id)
        provider_evidence = _public_provider_evidence(selected)
        title_translation = self.story_title_translations(story_ids=(story_id,)).get(story_id)
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
        story = _story_summary(row)
        return {
            **story,
            "provider_evidence": provider_evidence,
            "push_delivery_state": _public_push_delivery_state(selected),
            "title_translation": _public_story_title_translation(
                story=story,
                selected=selected,
                translation=title_translation,
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

    # Story display-title translation ----------------------------------------

    def reconcile_story_title_translation_targets(
        self,
        *,
        now_ms: int,
        configured: bool,
        locale: str,
        workflow_version: str,
        prompt_version: str,
        max_attempts: int,
        retry_delays_ms: Sequence[int],
        retention_ms: int,
    ) -> dict[str, int]:
        if locale != TITLE_TRANSLATION_LOCALE:
            raise ValueError("news_title_translation_locale_invalid")
        if max_attempts != 3 or len(retry_delays_ms) != max_attempts - 1:
            raise ValueError("news_title_translation_retry_policy_invalid")
        if retention_ms <= 0:
            raise ValueError("news_title_translation_retention_invalid")

        recovered = 0
        expired = self.conn.execute(
            """
            SELECT *
             FROM news_story_title_translations
             WHERE status = 'running'
               AND lease_expires_at_ms <= %s
             ORDER BY lease_expires_at_ms, story_id
             LIMIT 100
             FOR UPDATE SKIP LOCKED
            """,
            (int(now_ms),),
        ).fetchall()
        for row in expired:
            attempt_count = int(row["attempt_count"])
            current_identity = (
                str(row["locale"]) == locale
                and str(row["workflow_version"]) == workflow_version
                and str(row["prompt_version"]) == prompt_version
            )
            attempt_error = (
                "news_title_translation_interrupted" if current_identity else "news_title_translation_workflow_obsolete"
            )
            attempts = _finish_story_title_translation_attempt(
                row["attempts"],
                attempt_count=attempt_count,
                now_ms=now_ms,
                outcome="failed",
                error_code=attempt_error,
            )
            if not current_identity:
                status = "unavailable"
                next_attempt_at_ms = None
                completed_at_ms = int(now_ms)
                last_error = "news_title_translation_workflow_obsolete"
            elif not configured:
                status = "unavailable"
                next_attempt_at_ms = None
                completed_at_ms = int(now_ms)
                last_error = "news_title_translation_not_configured"
            elif attempt_count >= max_attempts:
                status = "failed"
                next_attempt_at_ms = None
                completed_at_ms = int(now_ms)
                last_error = "news_title_translation_interrupted"
            else:
                status = "retry_wait"
                next_attempt_at_ms = int(now_ms) + int(retry_delays_ms[attempt_count - 1])
                completed_at_ms = None
                last_error = "news_title_translation_interrupted"
            cursor = self.conn.execute(
                """
                UPDATE news_story_title_translations
                   SET status = %s,
                       attempts = %s,
                       next_attempt_at_ms = %s,
                       lease_owner = NULL,
                       lease_token = NULL,
                       lease_expires_at_ms = NULL,
                       last_error = %s,
                       completed_at_ms = %s,
                       updated_at_ms = %s
                 WHERE story_id = %s
                   AND source_title_fingerprint = %s
                   AND locale = %s
                   AND workflow_version = %s
                   AND prompt_version = %s
                   AND status = 'running'
                """,
                (
                    status,
                    Jsonb(attempts),
                    next_attempt_at_ms,
                    last_error,
                    completed_at_ms,
                    int(now_ms),
                    str(row["story_id"]),
                    str(row["source_title_fingerprint"]),
                    str(row["locale"]),
                    str(row["workflow_version"]),
                    str(row["prompt_version"]),
                ),
            )
            recovered += int(cursor.rowcount or 0)

        story_rows = self.conn.execute(
            """
            SELECT story.story_id,
                   story.representative_title,
                   story.last_published_at_ms
              FROM news_stories story
             WHERE EXISTS (
               SELECT 1
                 FROM news_story_members member
                 JOIN news_items item ON item.item_id = member.item_id
                WHERE member.story_id = story.story_id
                  AND jsonb_typeof(item.provider_metadata -> 'score') = 'number'
                  AND (item.provider_metadata ->> 'score')::numeric > %s
             )
             ORDER BY story.last_published_at_ms DESC, story.story_id
             LIMIT %s
            """,
            (
                _TITLE_TRANSLATION_PROVIDER_SCORE_THRESHOLD,
                _TITLE_TRANSLATION_TARGET_LIMIT,
            ),
        ).fetchall()
        targets: list[dict[str, Any]] = []
        for row in story_rows:
            raw_title = str(row["representative_title"])
            source_title = normalize_news_display_title(raw_title)
            targets.append(
                {
                    "story_id": str(row["story_id"]),
                    "source_title": source_title,
                    "source_title_fingerprint": story_title_fingerprint(source_title),
                    "source_raw_title_fingerprint": hashlib.sha256(raw_title.encode("utf-8")).hexdigest(),
                    "source_is_zh": looks_zh_cn_title(source_title),
                }
            )

        inserted_or_rebound = 0
        transitioned = 0
        if targets:
            target_payload = Jsonb(targets)
            cursor = self.conn.execute(
                """
                WITH targets AS (
                  SELECT *
                    FROM jsonb_to_recordset(%s) AS target(
                      story_id text,
                      source_title text,
                      source_title_fingerprint text,
                      source_raw_title_fingerprint text,
                      source_is_zh boolean
                    )
                )
                INSERT INTO news_story_title_translations (
                  story_id, source_title, source_title_fingerprint,
                  source_raw_title_fingerprint, locale, workflow_version,
                  prompt_version, status, result_kind, translated_title,
                  provider, model, attempt_count, attempts,
                  next_attempt_at_ms, lease_owner, lease_token,
                  lease_expires_at_ms, last_error, completed_at_ms,
                  created_at_ms, updated_at_ms
                )
                SELECT target.story_id,
                       target.source_title,
                       target.source_title_fingerprint,
                       target.source_raw_title_fingerprint,
                       %s, %s, %s,
                       CASE
                         WHEN target.source_is_zh THEN 'ready'
                         WHEN %s THEN 'pending'
                         ELSE 'unavailable'
                       END,
                       CASE WHEN target.source_is_zh THEN 'source_zh' ELSE NULL END,
                       CASE WHEN target.source_is_zh THEN target.source_title ELSE NULL END,
                       NULL, NULL, 0, '[]'::jsonb,
                       CASE WHEN NOT target.source_is_zh AND %s THEN %s ELSE NULL END,
                       NULL, NULL, NULL,
                       CASE
                         WHEN NOT target.source_is_zh AND NOT %s
                           THEN 'news_title_translation_not_configured'
                         ELSE NULL
                       END,
                       CASE
                         WHEN target.source_is_zh OR NOT %s THEN %s
                         ELSE NULL
                       END,
                       %s, %s
                  FROM targets target
                ON CONFLICT (
                  story_id, source_title_fingerprint, locale,
                  workflow_version, prompt_version
                ) DO UPDATE
                   SET source_raw_title_fingerprint =
                         EXCLUDED.source_raw_title_fingerprint,
                       updated_at_ms = EXCLUDED.updated_at_ms
                 WHERE news_story_title_translations.source_raw_title_fingerprint
                       <> EXCLUDED.source_raw_title_fingerprint
                """,
                (
                    target_payload,
                    locale,
                    workflow_version,
                    prompt_version,
                    bool(configured),
                    bool(configured),
                    int(now_ms),
                    bool(configured),
                    bool(configured),
                    int(now_ms),
                    int(now_ms),
                    int(now_ms),
                ),
            )
            inserted_or_rebound = int(cursor.rowcount or 0)

            transitioned += int(
                self.conn.execute(
                    """
                    WITH targets AS (
                      SELECT *
                        FROM jsonb_to_recordset(%s) AS target(
                          story_id text,
                          source_title_fingerprint text,
                          source_is_zh boolean
                        )
                    )
                    UPDATE news_story_title_translations translation
                       SET status = 'ready',
                           result_kind = 'source_zh',
                           translated_title = translation.source_title,
                           next_attempt_at_ms = NULL,
                           last_error = NULL,
                           completed_at_ms = %s,
                           updated_at_ms = %s
                      FROM targets target
                     WHERE translation.story_id = target.story_id
                       AND translation.source_title_fingerprint =
                           target.source_title_fingerprint
                       AND translation.locale = %s
                       AND translation.workflow_version = %s
                       AND translation.prompt_version = %s
                       AND target.source_is_zh
                       AND translation.status IN (
                         'pending', 'retry_wait', 'unavailable'
                       )
                       AND translation.attempt_count = 0
                    """,
                    (
                        target_payload,
                        int(now_ms),
                        int(now_ms),
                        locale,
                        workflow_version,
                        prompt_version,
                    ),
                ).rowcount
                or 0
            )
            if configured:
                transitioned += int(
                    self.conn.execute(
                        """
                        WITH targets AS (
                          SELECT *
                            FROM jsonb_to_recordset(%s) AS target(
                              story_id text,
                              source_title_fingerprint text,
                              source_is_zh boolean
                            )
                        )
                        UPDATE news_story_title_translations translation
                           SET status = 'pending',
                               next_attempt_at_ms = %s,
                               last_error = NULL,
                               completed_at_ms = NULL,
                               updated_at_ms = %s
                          FROM targets target
                         WHERE translation.story_id = target.story_id
                           AND translation.source_title_fingerprint =
                               target.source_title_fingerprint
                           AND translation.locale = %s
                           AND translation.workflow_version = %s
                           AND translation.prompt_version = %s
                           AND NOT target.source_is_zh
                           AND translation.status = 'unavailable'
                           AND translation.last_error =
                               'news_title_translation_not_configured'
                           AND translation.attempt_count < %s
                        """,
                        (
                            target_payload,
                            int(now_ms),
                            int(now_ms),
                            locale,
                            workflow_version,
                            prompt_version,
                            int(max_attempts),
                        ),
                    ).rowcount
                    or 0
                )
            else:
                transitioned += int(
                    self.conn.execute(
                        """
                        WITH targets AS (
                          SELECT *
                            FROM jsonb_to_recordset(%s) AS target(
                              story_id text,
                              source_title_fingerprint text,
                              source_is_zh boolean
                            )
                        )
                        UPDATE news_story_title_translations translation
                           SET status = 'unavailable',
                               next_attempt_at_ms = NULL,
                               last_error =
                                 'news_title_translation_not_configured',
                               completed_at_ms = %s,
                               updated_at_ms = %s
                          FROM targets target
                         WHERE translation.story_id = target.story_id
                           AND translation.source_title_fingerprint =
                               target.source_title_fingerprint
                           AND translation.locale = %s
                           AND translation.workflow_version = %s
                           AND translation.prompt_version = %s
                           AND NOT target.source_is_zh
                           AND translation.status IN ('pending', 'retry_wait')
                        """,
                        (
                            target_payload,
                            int(now_ms),
                            int(now_ms),
                            locale,
                            workflow_version,
                            prompt_version,
                        ),
                    ).rowcount
                    or 0
                )

        pruned = int(
            self.conn.execute(
                """
                DELETE FROM news_story_title_translations translation
                 WHERE translation.status <> 'running'
                   AND translation.updated_at_ms < %s
                   AND NOT EXISTS (
                     SELECT 1
                       FROM news_stories story
                      WHERE story.story_id = translation.story_id
                        AND translation.locale = %s
                        AND translation.workflow_version = %s
                        AND translation.prompt_version = %s
                        AND translation.source_raw_title_fingerprint = encode(
                          sha256(convert_to(story.representative_title, 'UTF8')),
                          'hex'
                        )
                        AND EXISTS (
                          SELECT 1
                            FROM news_story_members member
                            JOIN news_items item
                              ON item.item_id = member.item_id
                           WHERE member.story_id = story.story_id
                             AND jsonb_typeof(
                               item.provider_metadata -> 'score'
                             ) = 'number'
                             AND (
                               item.provider_metadata ->> 'score'
                             )::numeric > %s
                        )
                   )
                """,
                (
                    int(now_ms) - int(retention_ms),
                    locale,
                    workflow_version,
                    prompt_version,
                    _TITLE_TRANSLATION_PROVIDER_SCORE_THRESHOLD,
                ),
            ).rowcount
            or 0
        )
        return {
            "eligible": len(targets),
            "recovered": recovered,
            "inserted_or_rebound": inserted_or_rebound,
            "transitioned": transitioned,
            "pruned": pruned,
        }

    def peek_story_title_translation_target(
        self,
        *,
        now_ms: int,
        locale: str,
        workflow_version: str,
        prompt_version: str,
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT translation.*
              FROM news_story_title_translations translation
              JOIN news_stories story
                ON story.story_id = translation.story_id
               AND translation.source_raw_title_fingerprint = encode(
                 sha256(convert_to(story.representative_title, 'UTF8')),
                 'hex'
               )
             WHERE translation.locale = %s
               AND translation.workflow_version = %s
               AND translation.prompt_version = %s
               AND translation.status IN ('pending', 'retry_wait')
               AND translation.next_attempt_at_ms <= %s
               AND EXISTS (
                 SELECT 1
                   FROM news_story_members member
                   JOIN news_items item ON item.item_id = member.item_id
                  WHERE member.story_id = story.story_id
                    AND jsonb_typeof(item.provider_metadata -> 'score') = 'number'
                    AND (item.provider_metadata ->> 'score')::numeric > %s
               )
             ORDER BY story.last_published_at_ms DESC,
                      translation.next_attempt_at_ms,
                      translation.story_id
             LIMIT 1
            """,
            (
                locale,
                workflow_version,
                prompt_version,
                int(now_ms),
                _TITLE_TRANSLATION_PROVIDER_SCORE_THRESHOLD,
            ),
        ).fetchone()
        return dict(row) if row is not None else None

    def claim_story_title_translation(
        self,
        *,
        story_id: str,
        source_title_fingerprint: str,
        locale: str,
        workflow_version: str,
        prompt_version: str,
        lease_owner: str,
        lease_token: str,
        lease_expires_at_ms: int,
        now_ms: int,
        max_attempts: int,
    ) -> dict[str, Any] | None:
        if max_attempts != 3:
            raise ValueError("news_title_translation_retry_policy_invalid")
        row = self.conn.execute(
            """
            SELECT translation.*, story.representative_title AS current_raw_title
              FROM news_story_title_translations translation
              JOIN news_stories story
                ON story.story_id = translation.story_id
               AND translation.source_raw_title_fingerprint = encode(
                 sha256(convert_to(story.representative_title, 'UTF8')),
                 'hex'
               )
             WHERE translation.story_id = %s
               AND translation.source_title_fingerprint = %s
               AND translation.locale = %s
               AND translation.workflow_version = %s
               AND translation.prompt_version = %s
               AND translation.status IN ('pending', 'retry_wait')
               AND translation.next_attempt_at_ms <= %s
               AND translation.attempt_count < %s
               AND EXISTS (
                 SELECT 1
                   FROM news_story_members member
                   JOIN news_items item ON item.item_id = member.item_id
                  WHERE member.story_id = story.story_id
                    AND jsonb_typeof(item.provider_metadata -> 'score') = 'number'
                    AND (item.provider_metadata ->> 'score')::numeric > %s
               )
             FOR UPDATE OF translation SKIP LOCKED
            """,
            (
                story_id,
                source_title_fingerprint,
                locale,
                workflow_version,
                prompt_version,
                int(now_ms),
                int(max_attempts),
                _TITLE_TRANSLATION_PROVIDER_SCORE_THRESHOLD,
            ),
        ).fetchone()
        if row is None:
            return None
        current_source_title = normalize_news_display_title(row["current_raw_title"])
        if (
            current_source_title != str(row["source_title"])
            or story_title_fingerprint(current_source_title) != source_title_fingerprint
        ):
            return None
        attempts = [dict(value) for value in list(row["attempts"] or [])]
        attempts.append({"attempted_at_ms": int(now_ms), "outcome": "started"})
        claimed = self.conn.execute(
            """
            UPDATE news_story_title_translations
               SET status = 'running',
                   attempt_count = attempt_count + 1,
                   attempts = %s,
                   next_attempt_at_ms = NULL,
                   lease_owner = %s,
                   lease_token = %s,
                   lease_expires_at_ms = %s,
                   last_error = NULL,
                   completed_at_ms = NULL,
                   updated_at_ms = %s
             WHERE story_id = %s
               AND source_title_fingerprint = %s
               AND locale = %s
               AND workflow_version = %s
               AND prompt_version = %s
               AND status IN ('pending', 'retry_wait')
            RETURNING *
            """,
            (
                Jsonb(attempts),
                lease_owner,
                lease_token,
                int(lease_expires_at_ms),
                int(now_ms),
                story_id,
                source_title_fingerprint,
                locale,
                workflow_version,
                prompt_version,
            ),
        ).fetchone()
        return dict(claimed) if claimed is not None else None

    def complete_story_title_translation(
        self,
        *,
        story_id: str,
        source_title_fingerprint: str,
        locale: str,
        workflow_version: str,
        prompt_version: str,
        lease_owner: str,
        lease_token: str,
        title_zh: str,
        provider: str,
        model: str,
        now_ms: int,
    ) -> bool:
        normalized_title = str(title_zh or "").strip()
        if normalize_news_display_text(normalized_title) != normalized_title or not looks_zh_cn_title(normalized_title):
            raise ValueError("news_title_translation_result_invalid")
        if not str(provider or "").strip() or not str(model or "").strip():
            raise ValueError("news_title_translation_provenance_required")
        row = self.conn.execute(
            """
            SELECT attempt_count, attempts
              FROM news_story_title_translations
             WHERE story_id = %s
               AND source_title_fingerprint = %s
               AND locale = %s
               AND workflow_version = %s
               AND prompt_version = %s
               AND status = 'running'
               AND lease_owner = %s
               AND lease_token = %s
             FOR UPDATE
            """,
            (
                story_id,
                source_title_fingerprint,
                locale,
                workflow_version,
                prompt_version,
                lease_owner,
                lease_token,
            ),
        ).fetchone()
        if row is None:
            return False
        attempts = _finish_story_title_translation_attempt(
            row["attempts"],
            attempt_count=int(row["attempt_count"]),
            now_ms=now_ms,
            outcome="succeeded",
        )
        cursor = self.conn.execute(
            """
            UPDATE news_story_title_translations
               SET status = 'ready',
                   result_kind = 'translated',
                   translated_title = %s,
                   provider = %s,
                   model = %s,
                   attempts = %s,
                   next_attempt_at_ms = NULL,
                   lease_owner = NULL,
                   lease_token = NULL,
                   lease_expires_at_ms = NULL,
                   last_error = NULL,
                   completed_at_ms = %s,
                   updated_at_ms = %s
             WHERE story_id = %s
               AND source_title_fingerprint = %s
               AND locale = %s
               AND workflow_version = %s
               AND prompt_version = %s
               AND status = 'running'
               AND lease_owner = %s
               AND lease_token = %s
            """,
            (
                normalized_title,
                str(provider).strip(),
                str(model).strip(),
                Jsonb(attempts),
                int(now_ms),
                int(now_ms),
                story_id,
                source_title_fingerprint,
                locale,
                workflow_version,
                prompt_version,
                lease_owner,
                lease_token,
            ),
        )
        return bool(cursor.rowcount)

    def fail_story_title_translation(
        self,
        *,
        story_id: str,
        source_title_fingerprint: str,
        locale: str,
        workflow_version: str,
        prompt_version: str,
        lease_owner: str,
        lease_token: str,
        error_code: str,
        retryable: bool,
        retry_delays_ms: Sequence[int],
        max_attempts: int,
        now_ms: int,
    ) -> bool:
        if max_attempts != 3 or len(retry_delays_ms) != max_attempts - 1:
            raise ValueError("news_title_translation_retry_policy_invalid")
        normalized_error = _public_story_title_translation_error(error_code)
        row = self.conn.execute(
            """
            SELECT attempt_count, attempts
              FROM news_story_title_translations
             WHERE story_id = %s
               AND source_title_fingerprint = %s
               AND locale = %s
               AND workflow_version = %s
               AND prompt_version = %s
               AND status = 'running'
               AND lease_owner = %s
               AND lease_token = %s
             FOR UPDATE
            """,
            (
                story_id,
                source_title_fingerprint,
                locale,
                workflow_version,
                prompt_version,
                lease_owner,
                lease_token,
            ),
        ).fetchone()
        if row is None:
            return False
        attempt_count = int(row["attempt_count"])
        attempts = _finish_story_title_translation_attempt(
            row["attempts"],
            attempt_count=attempt_count,
            now_ms=now_ms,
            outcome="failed",
            error_code=normalized_error,
        )
        if retryable and attempt_count < max_attempts:
            status = "retry_wait"
            next_attempt_at_ms = int(now_ms) + int(retry_delays_ms[attempt_count - 1])
            completed_at_ms = None
        else:
            status = "failed" if retryable else "unavailable"
            next_attempt_at_ms = None
            completed_at_ms = int(now_ms)
        cursor = self.conn.execute(
            """
            UPDATE news_story_title_translations
               SET status = %s,
                   attempts = %s,
                   next_attempt_at_ms = %s,
                   lease_owner = NULL,
                   lease_token = NULL,
                   lease_expires_at_ms = NULL,
                   last_error = %s,
                   completed_at_ms = %s,
                   updated_at_ms = %s
             WHERE story_id = %s
               AND source_title_fingerprint = %s
               AND locale = %s
               AND workflow_version = %s
               AND prompt_version = %s
               AND status = 'running'
               AND lease_owner = %s
               AND lease_token = %s
            """,
            (
                status,
                Jsonb(attempts),
                next_attempt_at_ms,
                normalized_error,
                completed_at_ms,
                int(now_ms),
                story_id,
                source_title_fingerprint,
                locale,
                workflow_version,
                prompt_version,
                lease_owner,
                lease_token,
            ),
        )
        return bool(cursor.rowcount)

    # News Story push ----------------------------------------------------------

    def story_provider_evidence(
        self,
        *,
        story_ids: Sequence[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Return each Story's highest numeric provider-score item.

        Selection is deterministic: numeric score descending, then newest
        publication, then item identity. The same bounded query also resolves
        the selected Article's durable push-ledger status for Feed and detail.
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
            WITH selected AS (
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
                     coalesce(
                       item.provider_score_updated_at_ms,
                       item.updated_at_ms
                     ) AS threshold_observed_at_ms,
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
            )
            SELECT selected.*, delivery.status AS push_delivery_status
              FROM selected
              LEFT JOIN LATERAL (
                SELECT status
                  FROM news_push_deliveries delivery
                 WHERE delivery.story_id = selected.story_id
                    OR delivery.selected_item_id = selected.item_id
                 ORDER BY (delivery.story_id = selected.story_id) DESC,
                          delivery.updated_at_ms DESC,
                          delivery.story_id
                 LIMIT 1
              ) delivery ON true
             ORDER BY selected.story_id
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
                "push_delivery_status": row["push_delivery_status"],
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
            ) SELECT
              %s, %s, %s, %s, %s, NULL, NULL, %s, %s,
              0, %s, NULL, NULL, NULL, NULL, NULL, NULL, %s, %s
             WHERE NOT EXISTS (
               SELECT 1
                 FROM news_push_deliveries existing
                WHERE existing.selected_item_id = %s
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
                selected_item_id,
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

    def suppress_claimed_push_delivery(
        self,
        *,
        story_id: str,
        lease_token: str,
        now_ms: int,
    ) -> bool:
        cursor = self.conn.execute(
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
            """,
            (int(now_ms), story_id, lease_token),
        )
        return bool(cursor.rowcount)

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
        cursor = self.conn.execute(
            """
            UPDATE news_push_deliveries
               SET translation_status = %s,
                   delivery_payload = %s,
                   payload_fingerprint = %s,
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
            """,
            (
                translation_status,
                Jsonb(dict(delivery_payload)),
                payload_fingerprint,
                int(now_ms),
                story_id,
                lease_token,
            ),
        )
        return bool(cursor.rowcount)

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

    def suppress_unsubmitted_push_delivery(
        self,
        *,
        story_id: str,
        lease_token: str,
        now_ms: int,
    ) -> bool:
        cursor = self.conn.execute(
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
            """,
            (int(now_ms), story_id, lease_token),
        )
        return bool(cursor.rowcount)

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
                   min(threshold_observed_at_ms) FILTER (
                     WHERE status IN (
                       'pending_translation', 'pending_delivery', 'retry_wait'
                     )
                   ) AS oldest_waiting_since_at_ms,
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
        translation_24h = self._push_translation_24h_snapshot(now_ms=now_ms)
        delivery_24h = self._push_delivery_24h_snapshot(now_ms=now_ms)
        slo_breached = translation_24h["slo_met"] is False or delivery_24h["slo_met"] is False
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
            "total_count": int(aggregate["total_count"] or 0),
            "suppressed_count": int(aggregate["suppressed_count"] or 0),
            "pending_count": int(aggregate["pending_count"] or 0),
            "retry_count": retry_count,
            "sent_count": int(aggregate["sent_count"] or 0),
            "terminal_count": terminal_count,
            "oldest_due_at_ms": aggregate["oldest_due_at_ms"],
            "_oldest_waiting_since_at_ms": aggregate["oldest_waiting_since_at_ms"],
            "latest_sent_at_ms": aggregate["latest_sent_at_ms"],
            "latest_error": (_public_push_error(str(latest_error["last_error"])) if latest_error is not None else None),
            "latest_error_at_ms": latest_error["updated_at_ms"] if latest_error is not None else None,
            "translation_24h": translation_24h,
            "delivery_24h": delivery_24h,
            "measured_at_ms": int(now_ms),
        }

    def _push_translation_24h_snapshot(self, *, now_ms: int) -> dict[str, Any]:
        row = self.conn.execute(
            """
            WITH samples AS (
              SELECT translation_status,
                     nullif(
                       delivery_payload #>> '{presentation,fallback_code}',
                       ''
                     ) AS fallback_code,
                     CASE
                       WHEN jsonb_typeof(
                         delivery_payload #> '{presentation,translation_duration_ms}'
                       ) = 'number'
                         THEN (
                           delivery_payload #>> '{presentation,translation_duration_ms}'
                         )::numeric
                       ELSE NULL
                     END AS duration_ms
                FROM news_push_deliveries
               WHERE delivery_payload #>> '{presentation,prompt_version}' = 'title_zh_v2'
                 AND jsonb_typeof(
                   delivery_payload #> '{presentation,translation_attempted_at_ms}'
                 ) = 'number'
                 AND (
                   delivery_payload #>> '{presentation,translation_attempted_at_ms}'
                 )::numeric BETWEEN %s AND %s
            ), failures AS (
              SELECT coalesce(
                       fallback_code,
                       'news_push_translation_unknown_failure'
                     ) AS failure_code,
                     count(*) AS failure_count
                FROM samples
               WHERE translation_status <> 'translated'
               GROUP BY 1
            )
            SELECT count(*) AS attempted,
                   count(*) FILTER (
                     WHERE translation_status = 'translated'
                   ) AS succeeded,
                   percentile_cont(0.95) WITHIN GROUP (
                     ORDER BY duration_ms
                   ) FILTER (
                     WHERE duration_ms IS NOT NULL AND duration_ms >= 0
                   ) AS latency_p95_ms,
                   coalesce(
                     (SELECT jsonb_object_agg(failure_code, failure_count)
                        FROM failures),
                     '{}'::jsonb
                   ) AS failure_counts
              FROM samples
            """,
            (int(now_ms) - _SLO_WINDOW_MS, int(now_ms)),
        ).fetchone()
        attempted = int(row["attempted"] or 0)
        succeeded = int(row["succeeded"] or 0)
        latency_value = row["latency_p95_ms"]
        latency_p95_ms = ceil(latency_value) if latency_value is not None else None
        success_ratio = succeeded / attempted if attempted else None
        failure_counts: Counter[str] = Counter()
        for failure_code, count in dict(row["failure_counts"] or {}).items():
            failure_counts[_public_push_error(str(failure_code))] += int(count)
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
        }

    def _push_delivery_24h_snapshot(self, *, now_ms: int) -> dict[str, Any]:
        row = self.conn.execute(
            """
            WITH samples AS (
              SELECT (
                       CASE
                         WHEN status = 'sent' THEN sent_at_ms
                         ELSE updated_at_ms
                       END
                     ) - threshold_observed_at_ms AS latency_ms
                FROM news_push_deliveries
               WHERE delivery_payload #>> '{presentation,prompt_version}' = 'title_zh_v2'
                 AND status IN ('sent', 'terminal')
                 AND CASE
                       WHEN status = 'sent' THEN sent_at_ms
                       ELSE updated_at_ms
                     END BETWEEN %s AND %s
            )
            SELECT count(*) FILTER (WHERE latency_ms >= 0) AS completed,
                   percentile_cont(0.95) WITHIN GROUP (
                     ORDER BY latency_ms
                   ) FILTER (WHERE latency_ms >= 0) AS latency_p95_ms,
                   count(*) FILTER (WHERE latency_ms > %s) AS over_120s
              FROM samples
            """,
            (
                int(now_ms) - _SLO_WINDOW_MS,
                int(now_ms),
                _NEWS_STALL_AFTER_MS,
            ),
        ).fetchone()
        completed = int(row["completed"] or 0)
        latency_value = row["latency_p95_ms"]
        latency_p95_ms = ceil(latency_value) if latency_value is not None else None
        return {
            "completed": completed,
            "latency_p95_ms": latency_p95_ms,
            "over_120s": int(row["over_120s"] or 0),
            "slo_met": latency_p95_ms <= 90_000 if latency_p95_ms is not None else None,
        }

    # Health -------------------------------------------------------------------

    def story_title_translation_health_snapshot(
        self,
        *,
        now_ms: int,
        configured: bool,
        locale: str = TITLE_TRANSLATION_LOCALE,
        workflow_version: str = TITLE_TRANSLATION_WORKFLOW_VERSION,
        prompt_version: str = TITLE_TRANSLATION_PROMPT_VERSION,
    ) -> dict[str, Any]:
        aggregate = self.conn.execute(
            """
            WITH eligible AS MATERIALIZED (
              SELECT story.story_id,
                     story.last_published_at_ms,
                     encode(
                       sha256(convert_to(story.representative_title, 'UTF8')),
                       'hex'
                     ) AS source_raw_title_fingerprint
                FROM news_stories story
               WHERE EXISTS (
                 SELECT 1
                   FROM news_story_members member
                   JOIN news_items item ON item.item_id = member.item_id
                  WHERE member.story_id = story.story_id
                    AND jsonb_typeof(item.provider_metadata -> 'score') = 'number'
                    AND (item.provider_metadata ->> 'score')::numeric > %s
               )
            ), current_targets AS (
              SELECT eligible.*,
                     translation.status,
                     translation.last_error,
                     translation.created_at_ms,
                     translation.completed_at_ms
                FROM eligible
                LEFT JOIN news_story_title_translations translation
                  ON translation.story_id = eligible.story_id
                 AND translation.source_raw_title_fingerprint =
                     eligible.source_raw_title_fingerprint
                 AND translation.locale = %s
                 AND translation.workflow_version = %s
                 AND translation.prompt_version = %s
            )
            SELECT count(*) AS eligible_count,
                   count(*) FILTER (WHERE status = 'ready') AS ready_count,
                   count(*) FILTER (
                     WHERE status IS NULL OR status IN ('pending', 'running')
                   ) AS pending_count,
                   count(*) FILTER (WHERE status = 'retry_wait') AS retry_count,
                   count(*) FILTER (WHERE status = 'failed') AS failed_count,
                   count(*) FILTER (WHERE status = 'unavailable')
                     AS unavailable_count,
                   count(*) FILTER (
                     WHERE status = 'unavailable'
                       AND last_error = 'news_title_translation_not_configured'
                   ) AS not_configured_count,
                   min(created_at_ms) FILTER (
                     WHERE status IN ('pending', 'running', 'retry_wait')
                   ) AS oldest_pending_at_ms,
                   max(completed_at_ms) FILTER (WHERE status = 'ready')
                     AS latest_success_at_ms
              FROM current_targets
            """,
            (
                _TITLE_TRANSLATION_PROVIDER_SCORE_THRESHOLD,
                locale,
                workflow_version,
                prompt_version,
            ),
        ).fetchone()
        rolling = self.conn.execute(
            """
            WITH samples AS (
              SELECT attempt ->> 'outcome' AS outcome,
                     CASE
                       WHEN jsonb_typeof(attempt -> 'duration_ms') = 'number'
                         THEN (attempt ->> 'duration_ms')::numeric
                       ELSE NULL
                     END AS duration_ms,
                     nullif(attempt ->> 'error_code', '') AS error_code
                FROM news_story_title_translations translation
                CROSS JOIN LATERAL jsonb_array_elements(
                  translation.attempts
                ) attempt
               WHERE translation.locale = %s
                 AND translation.workflow_version = %s
                 AND translation.prompt_version = %s
                 AND jsonb_typeof(attempt -> 'attempted_at_ms') = 'number'
                 AND (attempt ->> 'attempted_at_ms')::numeric
                     BETWEEN %s AND %s
            ), failures AS (
              SELECT coalesce(
                       error_code,
                       'news_title_translation_unknown_failure'
                     ) AS failure_code,
                     count(*) AS failure_count
                FROM samples
               WHERE outcome = 'failed'
               GROUP BY 1
            )
            SELECT count(*) AS attempted,
                   count(*) FILTER (WHERE outcome = 'succeeded') AS succeeded,
                   percentile_cont(0.95) WITHIN GROUP (
                     ORDER BY duration_ms
                   ) FILTER (
                     WHERE duration_ms IS NOT NULL AND duration_ms >= 0
                   ) AS latency_p95_ms,
                   coalesce(
                     (SELECT jsonb_object_agg(failure_code, failure_count)
                        FROM failures),
                     '{}'::jsonb
                   ) AS failure_counts
              FROM samples
            """,
            (
                locale,
                workflow_version,
                prompt_version,
                int(now_ms) - _SLO_WINDOW_MS,
                int(now_ms),
            ),
        ).fetchone()
        eligible_count = int(aggregate["eligible_count"] or 0)
        ready_count = int(aggregate["ready_count"] or 0)
        pending_count = int(aggregate["pending_count"] or 0)
        retry_count = int(aggregate["retry_count"] or 0)
        failed_count = int(aggregate["failed_count"] or 0)
        unavailable_count = int(aggregate["unavailable_count"] or 0)
        not_configured_count = int(aggregate["not_configured_count"] or 0)
        oldest_pending_at_ms = aggregate["oldest_pending_at_ms"]
        reasons: list[str] = []
        configuration_missing = bool(not configured and not_configured_count > 0)
        if configuration_missing:
            reasons.append("title_translation_not_configured")
        if failed_count:
            reasons.append("title_translation_failed")
        if unavailable_count and not configuration_missing:
            reasons.append("title_translation_unavailable")
        status = (
            "degraded"
            if configuration_missing or failed_count or unavailable_count
            else "warming"
            if pending_count or retry_count
            else "ready"
        )
        attempted = int(rolling["attempted"] or 0)
        succeeded = int(rolling["succeeded"] or 0)
        latency_value = rolling["latency_p95_ms"]
        failure_counts: Counter[str] = Counter()
        for error_code, count in dict(rolling["failure_counts"] or {}).items():
            failure_counts[_public_story_title_translation_error(error_code)] += int(count)
        return {
            "status": status,
            "reasons": reasons,
            "configured": bool(configured),
            "locale": locale,
            "workflow_version": workflow_version,
            "prompt_version": prompt_version,
            "eligible_count": eligible_count,
            "ready_count": ready_count,
            "pending_count": pending_count,
            "retry_count": retry_count,
            "failed_count": failed_count,
            "unavailable_count": unavailable_count,
            "oldest_pending_at_ms": (int(oldest_pending_at_ms) if oldest_pending_at_ms is not None else None),
            "latest_success_at_ms": (
                int(aggregate["latest_success_at_ms"]) if aggregate["latest_success_at_ms"] is not None else None
            ),
            "rolling_24h": {
                "attempted": attempted,
                "succeeded": succeeded,
                "success_ratio": succeeded / attempted if attempted else None,
                "latency_p95_ms": ceil(latency_value) if latency_value is not None else None,
                "failure_counts": dict(sorted(failure_counts.items())),
            },
        }

    def health_snapshot(
        self,
        *,
        now_ms: int,
        push_enabled: bool = False,
        title_translation_configured: bool = False,
        feishu_webhook_url_configured: bool = False,
        feishu_signing_secret_configured: bool = False,
        workers_state: str | None = None,
        workers_reason: str | None = None,
    ) -> dict[str, Any]:
        if workers_state not in {None, "running", "recovering", "stalled"}:
            raise ValueError("news_workers_state_invalid")
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
                   last_success_at_ms,
                   last_error
              FROM news_projection_summary
             WHERE singleton_key = 'current'
            """
        ).fetchone()
        unmaterialized_snapshot = self.conn.execute(
            """
            SELECT count(*) AS item_count,
                   min(item.first_observed_at_ms) AS oldest_observed_at_ms
              FROM news_items item
              JOIN news_sources source ON source.source_id = item.source_id
              LEFT JOIN news_story_members member ON member.item_id = item.item_id
             WHERE source.enabled
               AND item.published_at_ms >= %s
               AND member.item_id IS NULL
            """,
            (int(now_ms) - _STORY_ACTIVE_WINDOW_MS,),
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

        unmaterialized = int(unmaterialized_snapshot["item_count"] or 0)
        oldest_unmaterialized_at_ms = unmaterialized_snapshot["oldest_observed_at_ms"]
        invalid_owners = int(story["invalid_owner_count"] or 0)
        invalid_aggregates = int(story["invalid_story_aggregate_count"] or 0)
        active_stories = int(story["active_count"] or 0)
        story_last_success_at_ms = int(story["last_success_at_ms"] or 0) or None
        story_reasons: list[str] = []
        story_stalled = bool(
            unmaterialized
            and oldest_unmaterialized_at_ms is not None
            and int(now_ms) - int(oldest_unmaterialized_at_ms) > _NEWS_STALL_AFTER_MS
        )
        story_recovering = bool(unmaterialized and not story_stalled)
        if story_stalled:
            story_reasons.append("story_projection_stalled")
        elif story_recovering:
            story_reasons.append("active_items_unmaterialized")
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

        if story_stalled or runtime_stalled or invalid_owners or invalid_aggregates or story["last_error"] is not None:
            story_status = "degraded"
        elif story_recovering or runtime_recovering or active_stories == 0:
            story_status = "warming"
            if active_stories == 0:
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
        title_translation = self.story_title_translation_health_snapshot(
            now_ms=now_ms,
            configured=title_translation_configured,
        )
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
                "oldest_unmaterialized_at_ms": (
                    int(oldest_unmaterialized_at_ms) if oldest_unmaterialized_at_ms is not None else None
                ),
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
                "target_fingerprint": brief["target_fingerprint"],
                "publication_id": (brief["publication"]["publication_id"] if brief["publication"] else None),
                "latest_run": brief["latest_run"],
            },
            "translation": title_translation,
            "push": push_payload,
        }
        statuses = [
            ingest_status,
            story_status,
            brief_status,
            str(title_translation["status"]),
        ]
        if push_enabled:
            statuses.append(push_status)
        overall = "degraded" if "degraded" in statuses else "warming" if "warming" in statuses else "ready"
        operating_state = (
            "stalled"
            if story_stalled or runtime_stalled or push_stalled
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
        "title": normalize_news_display_title(row["representative_title"]),
        "description": normalize_news_display_text(row["representative_description"]),
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


def _public_push_delivery_state(selected: Mapping[str, Any] | None) -> str | None:
    if selected is None:
        return None
    evidence = selected.get("provider_evidence")
    if not isinstance(evidence, Mapping):
        return None
    score = evidence.get("provider_score")
    if not _numeric_provider_score(score) or float(cast(int | float, score)) <= 70:
        return None
    status = selected.get("push_delivery_status")
    if status in {"pending_translation", "pending_delivery", "retry_wait"}:
        return "pending"
    if status == "sent":
        return "sent"
    if status == "suppressed":
        return "suppressed"
    if status == "terminal":
        return "failed"
    return "pending"


def _public_story_title_translation(
    *,
    story: Mapping[str, Any],
    selected: Mapping[str, Any] | None,
    translation: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    evidence = selected.get("provider_evidence") if selected is not None else None
    score = evidence.get("provider_score") if isinstance(evidence, Mapping) else None
    if not _numeric_provider_score(score) or float(cast(int | float, score)) <= 70:
        return None
    source_title = str(story["title"])
    fingerprint = story_title_fingerprint(source_title)
    public_state = "pending"
    title_zh: str | None = None
    if (
        translation is not None
        and str(translation.get("source_title") or "") == source_title
        and str(translation.get("source_title_fingerprint") or "") == fingerprint
    ):
        private_state = str(translation.get("status") or "")
        if private_state == "ready" and str(translation.get("translated_title") or "").strip():
            public_state = "ready"
            title_zh = str(translation["translated_title"])
        elif private_state in {"failed", "unavailable"}:
            public_state = private_state
    return {
        "state": public_state,
        "title_zh": title_zh,
        "source_title": source_title,
        "source_title_fingerprint": fingerprint,
        "locale": TITLE_TRANSLATION_LOCALE,
        "workflow_version": TITLE_TRANSLATION_WORKFLOW_VERSION,
        "prompt_version": TITLE_TRANSLATION_PROMPT_VERSION,
    }


def _public_push_error(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if re.fullmatch(r"[a-z0-9_]{1,120}", normalized):
        return normalized
    return "news_story_push_delivery_error"


def _public_story_title_translation_error(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if re.fullmatch(r"[a-z0-9_]{1,120}", normalized):
        return normalized
    return "news_title_translation_error"


def _finish_story_title_translation_attempt(
    value: object,
    *,
    attempt_count: int,
    now_ms: int,
    outcome: str,
    error_code: str | None = None,
) -> list[dict[str, Any]]:
    if outcome not in {"succeeded", "failed"}:
        raise ValueError("news_title_translation_attempt_outcome_invalid")
    attempts = [dict(attempt) for attempt in cast(Sequence[Mapping[str, Any]], value or [])]
    if len(attempts) != attempt_count or not attempts or attempts[-1].get("outcome") != "started":
        raise RuntimeError("news_title_translation_attempt_ledger_invalid")
    attempted_at_ms = int(attempts[-1].get("attempted_at_ms") or now_ms)
    completed = {
        "attempted_at_ms": attempted_at_ms,
        "outcome": outcome,
        "duration_ms": max(0, int(now_ms) - attempted_at_ms),
    }
    if outcome == "failed":
        completed["error_code"] = _public_story_title_translation_error(error_code)
    attempts[-1] = completed
    return attempts


def _item_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "item_id": str(row["item_id"]),
        "provider_record_id": (str(row["provider_record_id"]) if row.get("provider_record_id") is not None else None),
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
