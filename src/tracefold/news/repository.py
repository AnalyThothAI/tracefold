from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any, cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from psycopg.types.json import Jsonb

from .brief import brief_fingerprint
from .classification import bounded_ai_classification, classify_by_keyword
from .identity import cluster_texts, normalize_story_text
from .models import (
    AI_CLASSIFIER_PROMPT_VERSION,
    BRIEF_PROMPT_VERSION,
    BRIEF_SCHEMA_VERSION,
    BRIEF_WORKFLOW_VERSION,
    CLASSIFIER_VERSION,
    IMPORTANCE_VERSION,
    NEWS_LOCALE,
    STORY_IDENTITY_VERSION,
    NewsBriefDraft,
    NewsClassification,
    NewsFeedEntry,
    NewsSourceDefinition,
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
_FETCH_RETENTION_MS = 30 * 24 * 60 * 60 * 1000
_AI_CACHE_RETENTION_MS = 30 * 24 * 60 * 60 * 1000
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


def _source_item_key(entry: NewsFeedEntry, canonical_url: str) -> str:
    return str(entry.guid or "").strip() or canonical_url


def _content_fingerprint(*, title: str, description: str, canonical_url: str) -> str:
    # pubDate is deliberately absent. Timestamp-only drift must not mutate
    # NewsItem or trigger a Story write.
    return _sha256_json(
        {
            "title": title,
            "description": description,
            "canonical_url": canonical_url,
        }
    )


class NewsRepository:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    # Source inventory and acquisition --------------------------------------------

    def sync_sources(self, sources: Sequence[NewsSourceDefinition], *, now_ms: int) -> None:
        source_ids = [source.source_id for source in sources]
        for source in sources:
            self.conn.execute(
                """
                INSERT INTO news_sources (
                  source_id, name, feed_url, reporting_origin, tier, lang,
                  category_hint, enabled, refresh_interval_seconds,
                  next_fetch_at_ms, created_at_ms, updated_at_ms
                )
                VALUES (
                  %(source_id)s, %(name)s, %(feed_url)s, %(reporting_origin)s,
                  %(tier)s, %(lang)s, %(category_hint)s, %(enabled)s,
                  %(refresh_interval_seconds)s, %(now_ms)s, %(now_ms)s, %(now_ms)s
                )
                ON CONFLICT (source_id) DO UPDATE SET
                  name = EXCLUDED.name,
                  feed_url = EXCLUDED.feed_url,
                  reporting_origin = EXCLUDED.reporting_origin,
                  tier = EXCLUDED.tier,
                  lang = EXCLUDED.lang,
                  category_hint = EXCLUDED.category_hint,
                  enabled = EXCLUDED.enabled,
                  refresh_interval_seconds = EXCLUDED.refresh_interval_seconds,
                  updated_at_ms = EXCLUDED.updated_at_ms
                WHERE (
                  news_sources.name,
                  news_sources.feed_url,
                  news_sources.reporting_origin,
                  news_sources.tier,
                  news_sources.lang,
                  news_sources.category_hint,
                  news_sources.enabled,
                  news_sources.refresh_interval_seconds
                ) IS DISTINCT FROM (
                  EXCLUDED.name,
                  EXCLUDED.feed_url,
                  EXCLUDED.reporting_origin,
                  EXCLUDED.tier,
                  EXCLUDED.lang,
                  EXCLUDED.category_hint,
                  EXCLUDED.enabled,
                  EXCLUDED.refresh_interval_seconds
                )
                """,
                {**source.model_dump(), "now_ms": now_ms},
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

    def claim_due_sources(self, *, now_ms: int, limit: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT *
              FROM news_sources
             WHERE enabled AND next_fetch_at_ms <= %s
             ORDER BY next_fetch_at_ms, source_id
             FOR UPDATE SKIP LOCKED
             LIMIT %s
            """,
            (now_ms, limit),
        ).fetchall()
        for row in rows:
            failures = int(row["consecutive_failures"])
            backoff_ms = min(3_600_000, int(row["refresh_interval_seconds"]) * 1000 * (2**failures))
            self.conn.execute(
                """
                UPDATE news_sources
                   SET last_fetch_started_at_ms = %s,
                       next_fetch_at_ms = %s,
                       updated_at_ms = %s
                 WHERE source_id = %s
                """,
                (now_ms, now_ms + backoff_ms, now_ms, row["source_id"]),
            )
        return [dict(row) for row in rows]

    def record_fetch_success(
        self,
        *,
        source: NewsSourceDefinition,
        entries: Sequence[NewsFeedEntry],
        started_at_ms: int,
        finished_at_ms: int,
        status_code: int,
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
            }

        inserted = 0
        updated = 0
        observations = 0
        rejection_counts: Counter[str] = Counter(
            {str(key): max(0, int(value)) for key, value in (gate_counts or {}).items() if int(value) > 0}
        )
        observed_entry_count = max(len(entries), int(entries_seen or 0))
        # The fetch row is inserted first because every immutable observation
        # references it. Counts are finalized after all raw observations exist.
        self._insert_fetch(
            fetch_id=fetch_id,
            source_id=source.source_id,
            started_at_ms=started_at_ms,
            finished_at_ms=finished_at_ms,
            status="success",
            http_status=status_code,
            entries_seen=observed_entry_count,
            observations_inserted=0,
            items_inserted=0,
            items_updated=0,
            rejection_counts={},
        )

        for position, entry in enumerate(entries):
            title = str(entry.title or "").strip()
            canonical_url = _canonical_url(str(entry.link or ""))
            reporting_origin = str(entry.reporting_origin or source.reporting_origin).strip().lower()
            source_item_key = _source_item_key(entry, canonical_url) or deterministic_id(
                "missing_item_key", source.source_id, position, title
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
                      item_id, source_id, source_item_key, canonical_url, title,
                      reporting_origin, normalized_title, description, lang, published_at_ms,
                      first_observed_at_ms, last_observed_at_ms, content_fingerprint,
                      level, category, classification_source,
                      classification_confidence, importance_score, importance_factors,
                      brief_excluded,
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
                        title,
                        reporting_origin,
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
            elif str(existing["content_fingerprint"]) == fingerprint:
                # Timestamp-only/source-repeat drift is an observation, not an
                # item revision and not a Story event.
                self.conn.execute(
                    """
                    UPDATE news_items
                       SET last_observed_at_ms = GREATEST(last_observed_at_ms, %s)
                     WHERE item_id = %s
                    """,
                    (finished_at_ms, item_id),
                )
                rejection_counts["duplicate"] += 1
            else:
                self.conn.execute(
                    """
                    UPDATE news_items
                       SET canonical_url = %s,
                           title = %s,
                           reporting_origin = %s,
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
                        title,
                        reporting_origin,
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
        }

    def record_fetch_failure(
        self,
        *,
        source_id: str,
        started_at_ms: int,
        finished_at_ms: int,
        error: Exception,
        status_code: int | None,
    ) -> None:
        fetch_id = deterministic_id("news_fetch", source_id, started_at_ms)
        error_code = f"{type(error).__name__}:{str(error)[:500]}"
        self._insert_fetch(
            fetch_id=fetch_id,
            source_id=source_id,
            started_at_ms=started_at_ms,
            finished_at_ms=finished_at_ms,
            status="failed",
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
              http_status, entries_seen, observations_inserted, items_inserted,
              items_updated, rejection_counts, error_code, created_at_ms
            )
            VALUES (
              %(fetch_id)s, %(source_id)s, %(started_at_ms)s, %(finished_at_ms)s,
              %(status)s, %(http_status)s, %(entries_seen)s,
              %(observations_inserted)s, %(items_inserted)s, %(items_updated)s,
              %(rejection_counts)s, %(error_code)s, %(finished_at_ms)s
            )
            ON CONFLICT (fetch_id) DO NOTHING
            """,
            {
                **values,
                "rejection_counts": Jsonb(values["rejection_counts"]),
                "error_code": values.get("error_code"),
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

    # Persistent Story projection ---------------------------------------------------

    def list_ai_classification_candidates(
        self,
        *,
        now_ms: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT i.item_id, i.title
              FROM news_items i
             WHERE i.active
               AND NOT EXISTS (
                 SELECT 1
                   FROM news_ai_classification_cache c
                  WHERE c.item_id = i.item_id
                    AND c.prompt_version = %s
                    AND c.expires_at_ms > %s
               )
             ORDER BY i.first_observed_at_ms, i.item_id
             LIMIT %s
            """,
            (AI_CLASSIFIER_PROMPT_VERSION, now_ms, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def store_ai_classification(
        self,
        *,
        item_id: str,
        classification: NewsClassification,
        model: str,
        raw_response: str,
        now_ms: int,
    ) -> None:
        cache_key = deterministic_id(
            "news_classification",
            item_id,
            AI_CLASSIFIER_PROMPT_VERSION,
        )
        self.conn.execute(
            """
            INSERT INTO news_ai_classification_cache (
              cache_key, item_id, model, prompt_version, level, category,
              confidence, raw_response, created_at_ms, expires_at_ms
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (cache_key) DO UPDATE SET
              model = EXCLUDED.model,
              level = EXCLUDED.level,
              category = EXCLUDED.category,
              confidence = EXCLUDED.confidence,
              raw_response = EXCLUDED.raw_response,
              created_at_ms = EXCLUDED.created_at_ms,
              expires_at_ms = EXCLUDED.expires_at_ms
            """,
            (
                cache_key,
                item_id,
                model,
                AI_CLASSIFIER_PROMPT_VERSION,
                classification.level,
                classification.category,
                classification.confidence,
                raw_response,
                now_ms,
                now_ms + _AI_CACHE_RETENTION_MS,
            ),
        )

    def rebuild_stories(self, *, now_ms: int) -> dict[str, int]:
        self.conn.execute("SELECT pg_advisory_xact_lock(%s)", (_PIPELINE_LOCK_KEY,))
        active_before = {
            str(row["story_id"])
            for row in self.conn.execute("SELECT story_id FROM news_stories WHERE active").fetchall()
        }
        cutoff_ms = now_ms - _ACTIVE_WINDOW_MS
        # WorldMonitor recomputes Date.now()-based recency behind its healthy
        # one-hour RSS cache. Persisting that score every pipeline tick would
        # create false Story revisions, so this projection uses the equivalent
        # one-hour scoring epoch.
        scoring_now_ms = now_ms - (now_ms % _SCORING_EPOCH_MS)
        self.conn.execute(
            "UPDATE news_items SET active = (published_at_ms >= %s)",
            (cutoff_ms,),
        )
        items = self.conn.execute(
            """
            SELECT i.*, s.name AS source_name, s.tier,
                   ai.level AS ai_level,
                   ai.category AS ai_category,
                   ai.confidence AS ai_confidence
              FROM news_items i
              JOIN news_sources s ON s.source_id = i.source_id
              LEFT JOIN LATERAL (
                SELECT c.level, c.category, c.confidence
                  FROM news_ai_classification_cache c
                 WHERE c.item_id = i.item_id
                   AND c.prompt_version = %s
                   AND c.expires_at_ms > %s
                 ORDER BY c.created_at_ms DESC, c.cache_key
                 LIMIT 1
              ) ai ON true
             WHERE i.active
             ORDER BY i.published_at_ms, i.item_id
            """,
            (AI_CLASSIFIER_PROMPT_VERSION, now_ms),
        ).fetchall()
        clusters = cluster_texts([str(item["title"]) for item in items])
        cluster_index_by_item: dict[str, int] = {}
        for cluster_index, indices in enumerate(clusters):
            for item_index in indices:
                cluster_index_by_item[str(items[item_index]["item_id"])] = cluster_index
        entity_buckets: dict[str, dict[str, Any]] = {}
        for item in items:
            if scoring_now_ms - int(item["published_at_ms"]) > 86_400_000:
                continue
            for entity_key in diplomacy_entity_keys(str(item["title"])):
                bucket = entity_buckets.setdefault(
                    entity_key,
                    {"clusters": set(), "origins": set(), "tier12_origins": set()},
                )
                bucket["clusters"].add(cluster_index_by_item[str(item["item_id"])])
                origin = str(item["reporting_origin"])
                bucket["origins"].add(origin)
                if int(item["tier"]) <= 2:
                    bucket["tier12_origins"].add(origin)
        entity_signal_by_cluster: dict[int, tuple[int, int]] = {}
        for bucket in entity_buckets.values():
            if len(bucket["origins"]) < 2:
                continue
            signal = (len(bucket["origins"]), len(bucket["tier12_origins"]))
            for cluster_index in bucket["clusters"]:
                previous_signal = entity_signal_by_cluster.get(cluster_index, (0, 0))
                entity_signal_by_cluster[cluster_index] = (
                    max(previous_signal[0], signal[0]),
                    max(previous_signal[1], signal[1]),
                )

        previous = self.conn.execute(
            """
            SELECT m.item_id, m.story_id
              FROM news_story_members m
              JOIN news_stories s ON s.story_id = m.story_id
             WHERE m.current
            """
        ).fetchall()
        previous_by_item = {str(row["item_id"]): str(row["story_id"]) for row in previous}
        aliases = self.conn.execute(
            "SELECT alias_key, story_id FROM news_story_aliases WHERE expires_at_ms > %s",
            (now_ms,),
        ).fetchall()
        alias_by_key = {str(row["alias_key"]): str(row["story_id"]) for row in aliases}

        story_writes = 0
        membership_writes = 0
        changed_story_ids: set[str] = set()
        current_story_ids: list[str] = []
        current_item_ids: list[str] = []
        for cluster_index, indices in enumerate(clusters):
            members = [dict(items[index]) for index in indices]
            origins = {str(member["reporting_origin"]) for member in members}
            origin_count = len(origins)
            entity_origin_count, tier12_entity_origin_count = entity_signal_by_cluster.get(
                cluster_index,
                (0, 0),
            )
            for member in members:
                deterministic = classify_by_keyword(str(member["title"]), now_ms=scoring_now_ms)
                classified = deterministic
                if member["ai_level"] is not None:
                    ai = NewsClassification(
                        level=str(member["ai_level"]),
                        category=str(member["ai_category"]),
                        confidence=float(member["ai_confidence"]),
                        source="llm",
                    )
                    if deterministic.source == "keyword-historical-downgrade":
                        classified = ai.model_copy(update={"level": "info", "source": "llm"})
                    else:
                        classified = bounded_ai_classification(deterministic, ai)
                level = promote_diplomacy_severity(
                    classified.level,
                    title=str(member["title"]),
                    tier12_origin_count=tier12_entity_origin_count,
                )
                member["level"] = level
                member["category"] = classified.category
                member["classification_source"] = classified.source
                member["classification_confidence"] = classified.confidence
                factors = importance_factors(
                    level=level,
                    tier=int(member["tier"]),
                    corroboration_count=origin_count,
                    published_at_ms=int(member["published_at_ms"]),
                    now_ms=scoring_now_ms,
                    title=str(member["title"]),
                    entity_corroboration_count=entity_origin_count,
                )
                score = int(factors["total"])
                member["importance_score"] = score
                member["importance_factors"] = factors
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
                        score,
                        Jsonb(factors),
                        member["level"],
                        member["category"],
                        member["classification_source"],
                        member["classification_confidence"],
                        now_ms,
                        member["item_id"],
                        score,
                        Jsonb(factors),
                        member["level"],
                        member["category"],
                        member["classification_source"],
                        member["classification_confidence"],
                    ),
                )

            earliest = min(
                members,
                key=lambda member: (int(member["published_at_ms"]), str(member["normalized_title"])),
            )
            canonical_key = hashlib.sha256(str(earliest["normalized_title"]).encode()).hexdigest()
            candidate_counts: Counter[str] = Counter()
            for member in members:
                item_id = str(member["item_id"])
                if item_id in previous_by_item:
                    candidate_counts[previous_by_item[item_id]] += 1
                alias_key = hashlib.sha256(str(member["normalized_title"]).encode()).hexdigest()
                if alias_key in alias_by_key:
                    candidate_counts[alias_by_key[alias_key]] += 1
            if candidate_counts:
                max_hits = max(candidate_counts.values())
                story_id = min(story for story, hits in candidate_counts.items() if hits == max_hits)
            else:
                story_id = deterministic_id("story", canonical_key)
            current_story_ids.append(story_id)

            representative = min(
                members,
                key=lambda member: (
                    -int(member["importance_score"]),
                    -int(member["published_at_ms"]),
                    str(member["title"]),
                    str(member["item_id"]),
                ),
            )
            fingerprint_payload = {
                "identity_version": STORY_IDENTITY_VERSION,
                "canonical_key": canonical_key,
                "representative_item_id": representative["item_id"],
                "members": sorted(str(member["item_id"]) for member in members),
                "level": representative["level"],
                "category": representative["category"],
                "importance_score": representative["importance_score"],
                "importance_factors": representative["importance_factors"],
                "source_count": origin_count,
                "first": min(int(member["published_at_ms"]) for member in members),
                "last": max(int(member["published_at_ms"]) for member in members),
            }
            fingerprint = _sha256_json(fingerprint_payload)
            row_count = self.conn.execute(
                """
                INSERT INTO news_stories (
                  story_id, canonical_key, canonical_title,
                  representative_item_id, representative_source_id,
                  representative_title, representative_url,
                  representative_description, level, category, importance_score,
                  importance_factors, item_count, source_count, first_published_at_ms,
                  last_published_at_ms, active, state_fingerprint,
                  created_at_ms, updated_at_ms
                )
                VALUES (
                  %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                  %s, %s, %s, %s, true, %s, %s, %s
                )
                ON CONFLICT (story_id) DO UPDATE SET
                  canonical_key = EXCLUDED.canonical_key,
                  canonical_title = EXCLUDED.canonical_title,
                  representative_item_id = EXCLUDED.representative_item_id,
                  representative_source_id = EXCLUDED.representative_source_id,
                  representative_title = EXCLUDED.representative_title,
                  representative_url = EXCLUDED.representative_url,
                  representative_description = EXCLUDED.representative_description,
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
                WHERE news_stories.state_fingerprint IS DISTINCT FROM EXCLUDED.state_fingerprint
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
                    representative["level"],
                    representative["category"],
                    representative["importance_score"],
                    Jsonb(representative["importance_factors"]),
                    len(members),
                    origin_count,
                    min(int(member["published_at_ms"]) for member in members),
                    max(int(member["published_at_ms"]) for member in members),
                    fingerprint,
                    now_ms,
                    now_ms,
                ),
            ).rowcount
            story_writes += int(row_count or 0)
            if row_count:
                changed_story_ids.add(story_id)

            for member in members:
                current_item_ids.append(str(member["item_id"]))
                self.conn.execute(
                    """
                    UPDATE news_story_members
                       SET current = false
                     WHERE item_id = %s AND story_id <> %s AND current
                    """,
                    (member["item_id"], story_id),
                )
                membership_writes += int(
                    self.conn.execute(
                        """
                        INSERT INTO news_story_members (
                          story_id, item_id, current, first_joined_at_ms, last_confirmed_at_ms
                        )
                        VALUES (%s, %s, true, %s, %s)
                        ON CONFLICT (story_id, item_id) DO UPDATE SET
                          current = true,
                          last_confirmed_at_ms = EXCLUDED.last_confirmed_at_ms
                        WHERE NOT news_story_members.current
                        """,
                        (story_id, member["item_id"], now_ms, now_ms),
                    ).rowcount
                    or 0
                )
                alias_key = hashlib.sha256(str(member["normalized_title"]).encode()).hexdigest()
                self.conn.execute(
                    """
                    INSERT INTO news_story_aliases(alias_key, story_id, expires_at_ms, created_at_ms)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (alias_key) DO UPDATE SET
                      story_id = EXCLUDED.story_id,
                      expires_at_ms = EXCLUDED.expires_at_ms
                    """,
                    (alias_key, story_id, now_ms + _ALIAS_TTL_MS, now_ms),
                )

        if current_story_ids:
            story_writes += int(
                self.conn.execute(
                    """
                    UPDATE news_stories
                       SET active = false, updated_at_ms = %s
                     WHERE active AND NOT (story_id = ANY(%s))
                    """,
                    (now_ms, current_story_ids),
                ).rowcount
                or 0
            )
        else:
            story_writes += int(
                self.conn.execute(
                    "UPDATE news_stories SET active = false, updated_at_ms = %s WHERE active",
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
        self.conn.execute("DELETE FROM news_story_aliases WHERE expires_at_ms <= %s", (now_ms,))
        self.conn.execute(
            "DELETE FROM news_source_fetches WHERE created_at_ms < %s",
            (now_ms - _FETCH_RETENTION_MS,),
        )
        self.conn.execute(
            "DELETE FROM news_ai_classification_cache WHERE expires_at_ms <= %s",
            (now_ms,),
        )
        current_story_id_set = set(current_story_ids)
        added = len(current_story_id_set - active_before)
        archived = len(active_before - current_story_id_set)
        unchanged = len((current_story_id_set & active_before) - changed_story_ids)
        return {
            "items": len(items),
            "stories": len(clusters),
            "story_writes": story_writes,
            "membership_writes": membership_writes,
            "added": added,
            "archived": archived,
            "unchanged": unchanged,
        }

    # Read contract ---------------------------------------------------------------

    def list_feed(
        self,
        *,
        category: str | None = None,
        sort: str = "importance",
    ) -> dict[str, Any]:
        if sort not in {"importance", "latest"}:
            raise ValueError("news_feed_sort_invalid")
        categories = (
            [category]
            if category
            else [
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
            ]
        )
        query = (
            """
            SELECT st.*, src.name AS representative_source_name
              FROM news_stories st
              JOIN news_sources src ON src.source_id = st.representative_source_id
             WHERE st.active AND st.category = ANY(%s)
             ORDER BY st.category, st.importance_score DESC,
                      st.last_published_at_ms DESC, st.story_id
            """
            if sort == "importance"
            else """
            SELECT st.*, src.name AS representative_source_name
              FROM news_stories st
              JOIN news_sources src ON src.source_id = st.representative_source_id
             WHERE st.active AND st.category = ANY(%s)
             ORDER BY st.category, st.last_published_at_ms DESC,
                      st.importance_score DESC, st.story_id
            """
        )
        rows = self.conn.execute(
            query,
            (categories,),
        ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {value: [] for value in categories}
        for row in rows:
            bucket = grouped[str(row["category"])]
            if len(bucket) < 20:
                bucket.append(_story_summary(row))
        return {
            "sort": sort,
            "categories": [{"category": value, "stories": grouped[value]} for value in categories if grouped[value]],
            "story_count": sum(len(values) for values in grouped.values()),
            "per_category_cap_count": max(0, len(rows) - sum(len(values) for values in grouped.values())),
        }

    def get_story(self, *, story_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT st.*, src.name AS representative_source_name
              FROM news_stories st
              JOIN news_sources src ON src.source_id = st.representative_source_id
             WHERE st.story_id = %s
            """,
            (story_id,),
        ).fetchone()
        if row is None:
            return None
        members = self.conn.execute(
            """
            SELECT i.*, src.name AS source_name, src.tier,
                   m.current, m.first_joined_at_ms, m.last_confirmed_at_ms
              FROM news_story_members m
              JOIN news_items i ON i.item_id = m.item_id
              JOIN news_sources src ON src.source_id = i.source_id
             WHERE m.story_id = %s
             ORDER BY m.current DESC, i.published_at_ms DESC, i.item_id
            """,
            (story_id,),
        ).fetchall()
        return {
            **_story_summary(row),
            "canonical_title": str(row["canonical_title"]),
            "active": bool(row["active"]),
            "members": [_item_payload(member) for member in members],
        }

    def list_sources(self) -> dict[str, Any]:
        rows = self.conn.execute(
            """
            SELECT s.*,
                   f.fetch_id AS latest_fetch_id,
                   f.status AS latest_fetch_status,
                   f.started_at_ms AS latest_fetch_started_at_ms,
                   f.finished_at_ms AS latest_fetch_finished_at_ms,
                   (f.finished_at_ms - f.started_at_ms) AS latest_fetch_duration_ms,
                   f.http_status AS latest_fetch_http_status,
                   f.entries_seen AS latest_entries_seen,
                   f.observations_inserted AS latest_observations_inserted,
                   f.items_inserted AS latest_items_inserted,
                   f.items_updated AS latest_items_updated,
                   f.rejection_counts AS latest_rejection_counts,
                   f.error_code AS latest_fetch_error_code
              FROM news_sources s
              LEFT JOIN LATERAL (
                SELECT *
                  FROM news_source_fetches
                 WHERE source_id = s.source_id
                 ORDER BY finished_at_ms DESC
                 LIMIT 1
              ) f ON true
             ORDER BY s.category_hint, s.tier, s.name, s.source_id
            """
        ).fetchall()
        return {"items": [dict(row) for row in rows]}

    # World Brief ------------------------------------------------------------------

    def brief_candidates(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT st.*, src.name AS representative_source_name,
                   item.reporting_origin AS representative_reporting_origin
              FROM news_stories st
              JOIN news_sources src ON src.source_id = st.representative_source_id
              JOIN news_items item ON item.item_id = st.representative_item_id
             WHERE st.active AND NOT item.brief_excluded
            """
        ).fetchall()
        return select_top_stories(rows, limit=8, max_per_source=3)

    def current_brief_fingerprint(self) -> str | None:
        row = self.conn.execute(
            """
            SELECT p.fingerprint
              FROM news_brief_current c
              LEFT JOIN news_brief_publications p ON p.publication_id = c.publication_id
             WHERE c.singleton_key
            """
        ).fetchone()
        return str(row["fingerprint"]) if row and row["fingerprint"] else None

    def brief_publication_exists(self, *, fingerprint: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM news_brief_publications WHERE fingerprint = %s",
            (fingerprint,),
        ).fetchone()
        return row is not None

    def begin_brief_update(self, *, fingerprint: str, now_ms: int) -> None:
        self.conn.execute("SELECT pg_advisory_xact_lock(%s)", (_BRIEF_LOCK_KEY,))
        self.conn.execute(
            """
            UPDATE news_brief_current
               SET pending_fingerprint = %s,
                   update_started_at_ms = %s,
                   last_attempt_at_ms = %s,
                   last_error = NULL,
                   updated_at_ms = %s
             WHERE singleton_key
            """,
            (fingerprint, now_ms, now_ms, now_ms),
        )

    def publish_brief(
        self,
        *,
        fingerprint: str,
        stories: Sequence[Mapping[str, Any]],
        draft: NewsBriefDraft,
        validation: Mapping[str, Any],
        now_ms: int,
        degraded: bool,
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
        self.conn.execute(
            """
            INSERT INTO news_brief_publications (
              publication_id, fingerprint, evidence_cutoff_at_ms, published_at_ms,
              provider, model, prompt_version, workflow_version, schema_version,
              locale, selected_story_ids, lead, lines, sources, validation,
              raw_response, status, created_at_ms
            )
            VALUES (
              %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
              %s, %s, %s, %s, %s, %s, %s, %s
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
                "degraded" if degraded else "published",
                now_ms,
            ),
        )
        if degraded:
            self.conn.execute(
                """
                UPDATE news_brief_current
                   SET pending_fingerprint = NULL,
                       update_started_at_ms = NULL,
                       last_failure_at_ms = %s,
                       last_error = 'brief_validation_degraded',
                       updated_at_ms = %s
                 WHERE singleton_key
                """,
                (now_ms, now_ms),
            )
        else:
            self.conn.execute(
                """
                UPDATE news_brief_current
                   SET publication_id = %s,
                       pending_fingerprint = NULL,
                       update_started_at_ms = NULL,
                       last_error = NULL,
                       updated_at_ms = %s
                 WHERE singleton_key
                """,
                (publication_id, now_ms),
            )
        return publication_id

    def fail_brief_update(self, *, error: Exception, now_ms: int) -> None:
        self.conn.execute(
            """
            UPDATE news_brief_current
               SET pending_fingerprint = NULL,
                   update_started_at_ms = NULL,
                   last_failure_at_ms = %s,
                   last_error = %s,
                   updated_at_ms = %s
             WHERE singleton_key
            """,
            (now_ms, f"{type(error).__name__}:{str(error)[:1000]}", now_ms),
        )

    def get_brief(self, *, now_ms: int, history_limit: int = 20) -> dict[str, Any]:
        current = self.conn.execute(
            """
            SELECT c.*, p.*
              FROM news_brief_current c
              LEFT JOIN news_brief_publications p ON p.publication_id = c.publication_id
             WHERE c.singleton_key
            """
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
        publication = _brief_payload(current) if current and current["publication_id"] else None
        publication_age_ms = max(0, now_ms - int(publication["published_at_ms"])) if publication is not None else None
        if current and current["pending_fingerprint"]:
            state = "updating"
        elif publication is None:
            state = "failed" if current and current["last_error"] else "unavailable"
        else:
            age_ms = max(0, now_ms - int(publication["published_at_ms"]))
            state = (
                "fresh"
                if age_ms <= 3_600_000
                else "stale"
                if age_ms <= 10_800_000
                else "failed"
                if current and current["last_error"]
                else "unavailable"
            )
        readable_publication = (
            publication if publication_age_ms is not None and publication_age_ms <= 10_800_000 else None
        )
        return {
            "state": state,
            "publication": readable_publication,
            "last_known_good_published_at_ms": (publication["published_at_ms"] if publication else None),
            "pending_fingerprint": current["pending_fingerprint"] if current else None,
            "update_started_at_ms": current["update_started_at_ms"] if current else None,
            "last_error": current["last_error"] if current else None,
            "last_failure_at_ms": current["last_failure_at_ms"] if current else None,
            "history": [_brief_payload(row) for row in history],
        }

    # Health -----------------------------------------------------------------------

    def health_snapshot(self, *, now_ms: int) -> dict[str, Any]:
        source = self.conn.execute(
            """
            SELECT
              count(*) FILTER (WHERE enabled) AS enabled_count,
              count(*) FILTER (
                WHERE enabled AND last_success_at_ms >= %s
              ) AS recent_success_count,
              max(last_success_at_ms) AS last_success_at_ms,
              count(*) FILTER (
                WHERE enabled AND consecutive_failures > 0
              ) AS failing_count,
              count(*) FILTER (
                WHERE enabled AND next_fetch_at_ms < %s
              ) AS overdue_count
              FROM news_sources
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
            SELECT
              count(*) FILTER (WHERE active) AS active_count,
              max(updated_at_ms) FILTER (WHERE active) AS last_material_change_at_ms,
              (
                SELECT count(*)
                  FROM news_items i
                 WHERE i.active
                   AND NOT EXISTS (
                     SELECT 1 FROM news_story_members m
                      WHERE m.item_id = i.item_id AND m.current
                   )
              ) AS unmaterialized_item_count
              ,(
                SELECT count(*)
                  FROM (
                    SELECT row_number() OVER (
                             PARTITION BY category
                             ORDER BY importance_score DESC,
                                      last_published_at_ms DESC,
                                      story_id
                           ) AS category_rank
                      FROM news_stories
                     WHERE active
                  ) ranked
                 WHERE category_rank > 20
              ) AS per_category_cap_count
              FROM news_stories
            """
        ).fetchone()
        brief = self.get_brief(now_ms=now_ms, history_limit=1)
        enabled = int(source["enabled_count"] or 0)
        recent = int(source["recent_success_count"] or 0)
        coverage_ratio = recent / enabled if enabled else 0.0
        ingest_reasons: list[str] = []
        if enabled == 0:
            ingest_reasons.append("no_enabled_sources")
            ingest_status = "unavailable"
        elif source["last_success_at_ms"] is None:
            ingest_reasons.append("no_source_success")
            ingest_status = "unavailable"
        elif coverage_ratio < 0.8:
            ingest_reasons.append("recent_source_coverage_below_80_percent")
            ingest_status = "degraded"
        elif int(source["overdue_count"] or 0) > max(3, enabled // 5):
            ingest_reasons.append("material_source_poll_overdue")
            ingest_status = "degraded"
        else:
            ingest_status = "healthy"
        active_story_count = int(story["active_count"] or 0)
        unmaterialized = int(story["unmaterialized_item_count"] or 0)
        story_reasons: list[str] = []
        if unmaterialized:
            story_reasons.append("current_items_unmaterialized")
            story_status = "degraded"
        elif active_story_count == 0:
            story_reasons.append("no_active_stories")
            story_status = "unavailable"
        else:
            story_status = "healthy"
        brief_status = (
            "healthy"
            if brief["state"] in {"fresh", "updating"}
            else "degraded"
            if brief["state"] == "stale"
            else "unavailable"
        )
        brief_reasons = [] if brief_status == "healthy" else [f"public_brief_{brief['state']}"]
        candidates = self.brief_candidates()
        top8_fingerprint = brief_fingerprint(candidates) if candidates else None
        current_fingerprint = str(brief["publication"]["fingerprint"]) if brief["publication"] else None
        fingerprint_current = top8_fingerprint is not None and top8_fingerprint == current_fingerprint
        if candidates and not fingerprint_current and brief["state"] not in {"updating"}:
            brief_reasons.append("top8_fingerprint_unpublished")
            if brief_status == "healthy":
                brief_status = "degraded"
        layers = {
            "ingest": {
                "status": ingest_status,
                "reasons": ingest_reasons,
                "enabled_sources": enabled,
                "recent_success_sources": recent,
                "recent_coverage_ratio": round(coverage_ratio, 4),
                "failing_sources": int(source["failing_count"] or 0),
                "overdue_sources": int(source["overdue_count"] or 0),
                "last_success_at_ms": source["last_success_at_ms"],
                "last_success_age_ms": (
                    max(0, now_ms - int(source["last_success_at_ms"]))
                    if source["last_success_at_ms"] is not None
                    else None
                ),
                "gate_counts_1h": dict(gate_counts),
            },
            "story": {
                "status": story_status,
                "reasons": story_reasons,
                "active_stories": int(story["active_count"] or 0),
                "last_material_change_at_ms": story["last_material_change_at_ms"],
                "last_material_change_age_ms": (
                    max(0, now_ms - int(story["last_material_change_at_ms"]))
                    if story["last_material_change_at_ms"] is not None
                    else None
                ),
                "unmaterialized_item_count": unmaterialized,
                "per_category_cap_count": int(story["per_category_cap_count"] or 0),
                "identity_version": STORY_IDENTITY_VERSION,
                "classifier_version": CLASSIFIER_VERSION,
                "importance_version": IMPORTANCE_VERSION,
            },
            "brief": {
                "status": brief_status,
                "reasons": brief_reasons,
                "public_state": brief["state"],
                "publication_id": (brief["publication"]["publication_id"] if brief["publication"] else None),
                "last_known_good_age_ms": (
                    max(0, now_ms - int(brief["last_known_good_published_at_ms"]))
                    if brief["last_known_good_published_at_ms"] is not None
                    else None
                ),
                "last_error": brief["last_error"],
                "latest_provider_outcome": (
                    {
                        "publication_id": brief["history"][0]["publication_id"],
                        "provider": brief["history"][0]["provider"],
                        "model": brief["history"][0]["model"],
                        "status": brief["history"][0]["status"],
                    }
                    if brief["history"]
                    else None
                ),
                "top8_fingerprint": top8_fingerprint,
                "current_publication_matches_top8": fingerprint_current,
            },
        }
        statuses = [ingest_status, story_status, brief_status]
        overall = (
            "unavailable"
            if all(status == "unavailable" for status in statuses)
            else "degraded"
            if any(status != "healthy" for status in statuses)
            else "healthy"
        )
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
        "status": str(row["status"]),
    }


__all__ = ["NewsRepository", "deterministic_id"]
