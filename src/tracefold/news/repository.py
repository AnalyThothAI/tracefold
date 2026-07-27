from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any, cast

from psycopg.types.json import Jsonb

from tracefold.news.briefing import plan_brief_selection
from tracefold.news.identity import (
    ANCHORED_CANDIDATE_WINDOW_MS,
    LEXICAL_CANDIDATE_WINDOW_MS,
    NAMED_EVENT_CANDIDATE_WINDOW_MS,
    admit_feed_entry,
    article_id_for,
    article_revision_id,
    classify_member_semantics,
    confirmed_url_reuse,
    decide_story,
    deterministic_id,
    extract_identity_features,
    identity_decision_id,
    project_story,
    sha256_json,
    story_id_for_seed,
)
from tracefold.news.models import (
    ARTICLE_IDENTITY_VERSION,
    SOURCE_REGISTRY_VERSION,
    STORY_IDENTITY_VERSION,
    STORY_LIFECYCLE_VERSION,
    STORY_SCORING_VERSION,
    ArticleIdentityFeatures,
    BriefEvidenceBundle,
    EvidencePosture,
    NewsFeedEntry,
    NewsPageFetch,
    NewsPublicationContract,
    NewsSourceDefinition,
    StoryAnalysisEvidence,
)

_PROJECTOR_LOCK_KEY = 727_301_982
_BRIEF_PLANNER_LOCK_KEY = 727_301_983


class NewsRepository:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    # Source registry and acquisition -------------------------------------------------

    def sync_sources(
        self,
        sources: Sequence[NewsSourceDefinition],
        *,
        now_ms: int,
    ) -> None:
        source_ids = [source.source_id for source in sources]
        for source in sources:
            self.conn.execute(
                """
                INSERT INTO news_sources (
                  source_id,
                  name,
                  feed_url,
                  source_domain,
                  source_role,
                  trust_tier,
                  source_chain_id,
                  publisher_organization_id,
                  parent_organization_id,
                  canonical_domains,
                  known_relationships,
                  source_quality_factors,
                  registry_version,
                  coverage_tags,
                  default_language,
                  enabled,
                  refresh_interval_seconds,
                  next_fetch_at_ms,
                  created_at_ms,
                  updated_at_ms
                )
                VALUES (
                  %(source_id)s,
                  %(name)s,
                  %(feed_url)s,
                  %(source_domain)s,
                  %(source_role)s,
                  %(trust_tier)s,
                  %(source_chain_id)s,
                  %(publisher_organization_id)s,
                  %(parent_organization_id)s,
                  %(canonical_domains)s,
                  %(known_relationships)s,
                  %(source_quality_factors)s,
                  %(registry_version)s,
                  %(coverage_tags)s,
                  %(default_language)s,
                  %(enabled)s,
                  %(refresh_interval_seconds)s,
                  %(next_fetch_at_ms)s,
                  %(created_at_ms)s,
                  %(updated_at_ms)s
                )
                ON CONFLICT (source_id) DO UPDATE SET
                  name = EXCLUDED.name,
                  feed_url = EXCLUDED.feed_url,
                  source_domain = EXCLUDED.source_domain,
                  source_role = EXCLUDED.source_role,
                  trust_tier = EXCLUDED.trust_tier,
                  source_chain_id = EXCLUDED.source_chain_id,
                  publisher_organization_id = EXCLUDED.publisher_organization_id,
                  parent_organization_id = EXCLUDED.parent_organization_id,
                  canonical_domains = EXCLUDED.canonical_domains,
                  known_relationships = EXCLUDED.known_relationships,
                  source_quality_factors = EXCLUDED.source_quality_factors,
                  registry_version = EXCLUDED.registry_version,
                  coverage_tags = EXCLUDED.coverage_tags,
                  default_language = EXCLUDED.default_language,
                  enabled = EXCLUDED.enabled,
                  refresh_interval_seconds = EXCLUDED.refresh_interval_seconds,
                  next_fetch_at_ms = CASE
                    WHEN news_sources.feed_url IS DISTINCT FROM EXCLUDED.feed_url
                      OR news_sources.enabled IS DISTINCT FROM EXCLUDED.enabled
                    THEN %(now_ms)s
                    ELSE news_sources.next_fetch_at_ms
                  END,
                  updated_at_ms = EXCLUDED.updated_at_ms
                """,
                {
                    **source.model_dump(
                        exclude={
                            "canonical_domains",
                            "known_relationships",
                            "source_quality_factors",
                            "coverage_tags",
                        }
                    ),
                    "canonical_domains": Jsonb(list(source.canonical_domains)),
                    "known_relationships": Jsonb(list(source.known_relationships)),
                    "source_quality_factors": Jsonb(dict(source.source_quality_factors)),
                    "coverage_tags": Jsonb(list(source.coverage_tags)),
                    "registry_version": SOURCE_REGISTRY_VERSION,
                    "next_fetch_at_ms": now_ms,
                    "created_at_ms": now_ms,
                    "updated_at_ms": now_ms,
                    "now_ms": now_ms,
                },
            )
        if source_ids:
            self.conn.execute(
                """
                UPDATE news_sources
                   SET enabled = false,
                       updated_at_ms = %s
                 WHERE enabled
                   AND NOT (source_id = ANY(%s))
                """,
                (now_ms, source_ids),
            )
        else:
            self.conn.execute(
                "UPDATE news_sources SET enabled = false, updated_at_ms = %s WHERE enabled",
                (now_ms,),
            )

    def claim_due_sources(
        self,
        *,
        now_ms: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT *
              FROM news_sources
             WHERE enabled
               AND next_fetch_at_ms <= %s
             ORDER BY next_fetch_at_ms, source_id
             FOR UPDATE SKIP LOCKED
             LIMIT %s
            """,
            (now_ms, limit),
        ).fetchall()
        claimed: list[dict[str, Any]] = []
        for row in rows:
            source = dict(row)
            self.conn.execute(
                """
                UPDATE news_sources
                   SET last_fetch_started_at_ms = %s,
                       next_fetch_at_ms = %s,
                       updated_at_ms = %s
                 WHERE source_id = %s
                """,
                (
                    now_ms,
                    now_ms + int(source["refresh_interval_seconds"]) * 1000,
                    now_ms,
                    source["source_id"],
                ),
            )
            claimed.append(source)
        return claimed

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
    ) -> dict[str, Any]:
        receipt_id = deterministic_id(
            "news-fetch-receipt",
            source.source_id,
            started_at_ms,
        )
        self.conn.execute(
            """
            INSERT INTO news_fetch_receipts (
              fetch_receipt_id,
              source_id,
              started_at_ms,
              finished_at_ms,
              http_status,
              not_modified,
              created_at_ms
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (fetch_receipt_id) DO NOTHING
            """,
            (
                receipt_id,
                source.source_id,
                started_at_ms,
                finished_at_ms,
                status_code,
                not_modified,
                finished_at_ms,
            ),
        )
        rejection_counts: defaultdict[str, int] = defaultdict(int)
        observation_ids: list[str] = []
        entries_admitted = 0
        duplicate_seen_count = 0
        articles_inserted = 0
        revisions_inserted = 0
        observations_inserted = 0

        for entry in entries:
            observation, rejection = admit_feed_entry(
                source=source,
                entry=entry,
                observed_at_ms=finished_at_ms,
            )
            if rejection is not None:
                rejection_counts[rejection] += 1
                continue
            if observation is None:
                rejection_counts["admission_internal_error"] += 1
                continue
            existing = self.conn.execute(
                """
                SELECT observation_id
                  FROM news_feed_observations
                 WHERE source_id = %s
                   AND source_entry_key = %s
                   AND observation_revision_hash = %s
                """,
                (
                    observation.source_id,
                    observation.source_entry_key,
                    observation.observation_revision_hash,
                ),
            ).fetchone()
            if existing is not None:
                duplicate_seen_count += 1
                continue
            self.conn.execute(
                """
                INSERT INTO news_feed_observations (
                  observation_id,
                  source_id,
                  fetch_receipt_id,
                  source_entry_key,
                  observation_revision_hash,
                  source_guid,
                  raw_url,
                  normalized_url,
                  title,
                  summary,
                  source_published_at_ms,
                  observed_at_ms,
                  language,
                  raw_entry,
                  created_at_ms
                )
                VALUES (
                  %(observation_id)s,
                  %(source_id)s,
                  %(fetch_receipt_id)s,
                  %(source_entry_key)s,
                  %(observation_revision_hash)s,
                  %(source_guid)s,
                  %(raw_url)s,
                  %(normalized_url)s,
                  %(title)s,
                  %(summary)s,
                  %(source_published_at_ms)s,
                  %(observed_at_ms)s,
                  %(language)s,
                  %(raw_entry)s,
                  %(created_at_ms)s
                )
                """,
                {
                    **observation.model_dump(exclude={"raw_entry"}),
                    "fetch_receipt_id": receipt_id,
                    "raw_entry": Jsonb(observation.raw_entry),
                    "created_at_ms": finished_at_ms,
                },
            )
            observations_inserted += 1
            observation_ids.append(observation.observation_id)
            entries_admitted += 1
            article_result = self._persist_observation_as_article(
                source=source,
                observation=observation.model_dump(),
                now_ms=finished_at_ms,
            )
            articles_inserted += _int(article_result["article_inserted"])
            revisions_inserted += _int(article_result["revision_inserted"])

        self.conn.execute(
            """
            UPDATE news_fetch_receipts
               SET entries_seen = %s,
                   entries_admitted = %s,
                   duplicate_seen_count = %s,
                   rejection_counts = %s,
                   observation_ids = %s,
                   etag = %s,
                   last_modified = %s
             WHERE fetch_receipt_id = %s
            """,
            (
                len(entries),
                entries_admitted,
                duplicate_seen_count,
                Jsonb(dict(sorted(rejection_counts.items()))),
                Jsonb(observation_ids),
                etag,
                last_modified,
                receipt_id,
            ),
        )
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
        return {
            "fetch_receipt_id": receipt_id,
            "entries_seen": len(entries),
            "entries_admitted": entries_admitted,
            "duplicate_seen_count": duplicate_seen_count,
            "rejection_counts": dict(sorted(rejection_counts.items())),
            "observations_inserted": observations_inserted,
            "articles_inserted": articles_inserted,
            "revisions_inserted": revisions_inserted,
        }

    def record_fetch_failure(
        self,
        *,
        source_id: str,
        started_at_ms: int,
        finished_at_ms: int,
        error: object,
        status_code: int | None = None,
    ) -> None:
        receipt_id = deterministic_id(
            "news-fetch-receipt",
            source_id,
            started_at_ms,
        )
        error_code = f"{type(error).__name__}"
        error_detail = _bounded_error(error)
        self.conn.execute(
            """
            INSERT INTO news_fetch_receipts (
              fetch_receipt_id,
              source_id,
              started_at_ms,
              finished_at_ms,
              http_status,
              error_code,
              error_detail,
              created_at_ms
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (fetch_receipt_id) DO UPDATE SET
              finished_at_ms = EXCLUDED.finished_at_ms,
              http_status = EXCLUDED.http_status,
              error_code = EXCLUDED.error_code,
              error_detail = EXCLUDED.error_detail
            """,
            (
                receipt_id,
                source_id,
                started_at_ms,
                finished_at_ms,
                status_code,
                error_code,
                error_detail,
                finished_at_ms,
            ),
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
            (
                finished_at_ms,
                status_code,
                f"{error_code}:{error_detail}",
                finished_at_ms,
                source_id,
            ),
        )

    def _persist_observation_as_article(
        self,
        *,
        source: NewsSourceDefinition,
        observation: Mapping[str, object],
        now_ms: int,
    ) -> dict[str, object]:
        publisher = str(source.publisher_organization_id or source.source_chain_id)
        normalized_url = str(observation["normalized_url"])
        current = self.conn.execute(
            """
            SELECT *
              FROM news_articles
             WHERE publisher_organization_id = %s
               AND canonical_url = %s
               AND identity_status = 'active'
             ORDER BY created_at_ms DESC, article_id DESC
             LIMIT 1
            """,
            (publisher, normalized_url),
        ).fetchone()
        article_inserted = current is None
        if current is None:
            article_id, incarnation_key = article_id_for(
                publisher_organization_id=publisher,
                normalized_url=normalized_url,
                first_observation_id=str(observation["observation_id"]),
            )
            self.conn.execute(
                """
                INSERT INTO news_articles (
                  article_id,
                  publisher_organization_id,
                  canonical_url,
                  incarnation_key,
                  first_observation_id,
                  first_seen_at_ms,
                  identity_version,
                  identity_status,
                  created_at_ms,
                  updated_at_ms
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'active', %s, %s)
                """,
                (
                    article_id,
                    publisher,
                    normalized_url,
                    incarnation_key,
                    observation["observation_id"],
                    observation["observed_at_ms"],
                    ARTICLE_IDENTITY_VERSION,
                    now_ms,
                    now_ms,
                ),
            )
            revision_number = 1
            material_change_kind = "initial"
        else:
            article_id = str(current["article_id"])
            current_revision = self.conn.execute(
                """
                SELECT *
                  FROM news_article_revisions
                 WHERE article_id = %s
                   AND is_current
                """,
                (article_id,),
            ).fetchone()
            if current_revision is None:
                raise RuntimeError("news_current_article_revision_missing")
            if confirmed_url_reuse(
                current_title=str(current_revision["title"]),
                current_snippet=str(current_revision["snippet"]),
                current_source_published_at_ms=_int(current_revision["source_published_at_ms"]),
                current_language=str(current_revision["language"]),
                new_title=str(observation["title"]),
                new_snippet=str(observation["summary"]),
                new_source_published_at_ms=_int(observation["source_published_at_ms"]),
                new_language=str(observation["language"]),
            ):
                self.conn.execute(
                    """
                    UPDATE news_articles
                       SET identity_status = 'ended',
                           updated_at_ms = %s
                     WHERE article_id = %s
                    """,
                    (now_ms, article_id),
                )
                article_id, incarnation_key = article_id_for(
                    publisher_organization_id=publisher,
                    normalized_url=normalized_url,
                    first_observation_id=str(observation["observation_id"]),
                )
                self.conn.execute(
                    """
                    INSERT INTO news_articles (
                      article_id,
                      publisher_organization_id,
                      canonical_url,
                      incarnation_key,
                      first_observation_id,
                      first_seen_at_ms,
                      identity_version,
                      identity_status,
                      created_at_ms,
                      updated_at_ms
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'active', %s, %s)
                    """,
                    (
                        article_id,
                        publisher,
                        normalized_url,
                        incarnation_key,
                        observation["observation_id"],
                        observation["observed_at_ms"],
                        ARTICLE_IDENTITY_VERSION,
                        now_ms,
                        now_ms,
                    ),
                )
                article_inserted = True
                revision_number = 1
                material_change_kind = "url_reuse"
            else:
                revision_number = int(current_revision["revision_number"]) + 1
                material_change_kind = _material_change_kind(
                    current_revision=dict(current_revision),
                    observation=observation,
                )

        content_payload = {
            "title": str(observation["title"]),
            "snippet": str(observation["summary"]),
            "source_published_at_ms": _int(observation["source_published_at_ms"]),
            "language": str(observation["language"]),
        }
        content_hash = sha256_json(content_payload)
        duplicate_revision = self.conn.execute(
            """
            SELECT revision_id
              FROM news_article_revisions
             WHERE article_id = %s
               AND content_hash = %s
            """,
            (article_id, content_hash),
        ).fetchone()
        if duplicate_revision is not None:
            return {
                "article_id": article_id,
                "revision_id": str(duplicate_revision["revision_id"]),
                "article_inserted": article_inserted,
                "revision_inserted": False,
            }

        revision_id = article_revision_id(
            article_id=article_id,
            content_hash=content_hash,
        )
        self.conn.execute(
            "UPDATE news_article_revisions SET is_current = false WHERE article_id = %s AND is_current",
            (article_id,),
        )
        self.conn.execute(
            """
            INSERT INTO news_article_revisions (
              revision_id,
              article_id,
              observation_id,
              revision_number,
              title,
              snippet,
              source_published_at_ms,
              observed_at_ms,
              language,
              content_hash,
              material_change_kind,
              is_current,
              raw_entry,
              created_at_ms
            )
            VALUES (
              %(revision_id)s,
              %(article_id)s,
              %(observation_id)s,
              %(revision_number)s,
              %(title)s,
              %(snippet)s,
              %(source_published_at_ms)s,
              %(observed_at_ms)s,
              %(language)s,
              %(content_hash)s,
              %(material_change_kind)s,
              true,
              %(raw_entry)s,
              %(created_at_ms)s
            )
            """,
            {
                "revision_id": revision_id,
                "article_id": article_id,
                "observation_id": observation["observation_id"],
                "revision_number": revision_number,
                "title": observation["title"],
                "snippet": observation["summary"],
                "source_published_at_ms": observation["source_published_at_ms"],
                "observed_at_ms": observation["observed_at_ms"],
                "language": observation["language"],
                "content_hash": content_hash,
                "material_change_kind": material_change_kind,
                "raw_entry": Jsonb(dict(_mapping(observation.get("raw_entry")))),
                "created_at_ms": now_ms,
            },
        )
        self.conn.execute(
            "UPDATE news_articles SET updated_at_ms = %s WHERE article_id = %s",
            (now_ms, article_id),
        )
        return {
            "article_id": article_id,
            "revision_id": revision_id,
            "article_inserted": article_inserted,
            "revision_inserted": True,
        }

    def claim_page_enrichment(
        self,
        *,
        now_ms: int,
        limit: int,
        minimum_impact_score: int,
        extractor_version: str,
        lease_ms: int,
        max_attempts: int,
    ) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT
              stories.story_id,
              revisions.article_id,
              revisions.revision_id,
              articles.canonical_url,
              snapshots.content_snapshot_id,
              snapshots.status AS snapshot_status,
              snapshots.attempt_count,
              snapshots.lease_expires_at_ms,
              snapshots.next_attempt_at_ms
            FROM news_stories AS stories
            JOIN news_article_revisions AS revisions
              ON revisions.revision_id = stories.representative_revision_id
            JOIN news_articles AS articles ON articles.article_id = revisions.article_id
            LEFT JOIN news_article_content_snapshots AS snapshots
              ON snapshots.revision_id = revisions.revision_id
             AND snapshots.extractor_version = %s
             AND snapshots.source_url = articles.canonical_url
            WHERE stories.impact_score >= %s
              AND stories.identity_status = 'stable'
              AND (
                snapshots.content_snapshot_id IS NULL
                OR (
                  snapshots.status = 'failed'
                  AND snapshots.next_attempt_at_ms <= %s
                  AND snapshots.attempt_count < %s
                )
                OR (
                  snapshots.status = 'pending'
                  AND snapshots.lease_expires_at_ms <= %s
                  AND snapshots.attempt_count < %s
                )
              )
            ORDER BY
              stories.impact_score DESC,
              stories.priority_score DESC,
              stories.story_id
            FOR UPDATE OF revisions SKIP LOCKED
            LIMIT %s
            """,
            (
                extractor_version,
                minimum_impact_score,
                now_ms,
                max_attempts,
                now_ms,
                max_attempts,
                limit,
            ),
        ).fetchall()
        claimed: list[dict[str, Any]] = []
        for row in rows:
            snapshot_id = str(
                row["content_snapshot_id"]
                or deterministic_id(
                    "news-content-snapshot",
                    row["revision_id"],
                    extractor_version,
                    row["canonical_url"],
                )
            )
            attempt_count = int(row["attempt_count"] or 0) + 1
            lease_token = deterministic_id(
                "news-page-lease",
                snapshot_id,
                attempt_count,
                now_ms,
            )
            self.conn.execute(
                """
                INSERT INTO news_article_content_snapshots (
                  content_snapshot_id, article_id, revision_id, requested_at_ms,
                  status, extractor_version, source_url, attempt_count, lease_token,
                  lease_expires_at_ms, next_attempt_at_ms, created_at_ms, updated_at_ms
                )
                VALUES (%s, %s, %s, %s, 'pending', %s, %s, 1, %s, %s, 0, %s, %s)
                ON CONFLICT (revision_id, extractor_version, source_url) DO UPDATE SET
                  status = 'pending',
                  requested_at_ms = EXCLUDED.requested_at_ms,
                  attempt_count = news_article_content_snapshots.attempt_count + 1,
                  lease_token = EXCLUDED.lease_token,
                  lease_expires_at_ms = EXCLUDED.lease_expires_at_ms,
                  updated_at_ms = EXCLUDED.updated_at_ms
                """,
                (
                    snapshot_id,
                    row["article_id"],
                    row["revision_id"],
                    now_ms,
                    extractor_version,
                    row["canonical_url"],
                    lease_token,
                    now_ms + lease_ms,
                    now_ms,
                    now_ms,
                ),
            )
            claimed.append(
                {
                    "content_snapshot_id": snapshot_id,
                    "story_id": str(row["story_id"]),
                    "article_id": str(row["article_id"]),
                    "revision_id": str(row["revision_id"]),
                    "source_url": str(row["canonical_url"]),
                    "lease_token": lease_token,
                }
            )
        return claimed

    def complete_page_enrichment(
        self,
        *,
        content_snapshot_id: str,
        lease_token: str,
        result: NewsPageFetch,
        retry_ms: int,
        now_ms: int,
    ) -> None:
        completed = self.conn.execute(
            """
            UPDATE news_article_content_snapshots
               SET fetched_at_ms = %s,
                   status = %s,
                   http_status = %s,
                   content_type = %s,
                   content_hash = %s,
                   extracted_text = %s,
                   byte_count = %s,
                   failure_reason = %s,
                   final_url = %s,
                   lease_expires_at_ms = 0,
                   next_attempt_at_ms = CASE WHEN %s = 'failed' THEN %s ELSE 0 END,
                   updated_at_ms = %s
             WHERE content_snapshot_id = %s
               AND lease_token = %s
               AND status = 'pending'
            """,
            (
                result.fetched_at_ms,
                result.status,
                result.http_status,
                result.content_type,
                result.content_hash,
                result.extracted_text,
                result.byte_count,
                result.failure_reason,
                result.final_url,
                result.status,
                now_ms + retry_ms,
                now_ms,
                content_snapshot_id,
                lease_token,
            ),
        )
        if completed.rowcount != 1 or result.status not in {"available", "truncated"}:
            return
        story = self.conn.execute(
            """
            SELECT stories.*
              FROM news_article_content_snapshots AS snapshots
              JOIN news_story_memberships AS memberships
                ON memberships.revision_id = snapshots.revision_id
               AND memberships.membership_kind = 'primary'
              JOIN news_stories AS stories ON stories.story_id = memberships.story_id
             WHERE snapshots.content_snapshot_id = %s
            """,
            (content_snapshot_id,),
        ).fetchone()
        if story is not None:
            self._ensure_automatic_analysis_request(
                story=dict(story),
                reason={"content_snapshot_available": content_snapshot_id},
                now_ms=now_ms,
            )

    # Story projection ---------------------------------------------------------------

    def reset_story_projection(self) -> dict[str, int]:
        """Delete rebuildable News products while preserving material facts."""

        ordered_tables = (
            "news_brief_activation_analysis",
            "news_brief_active",
            "news_brief_activations",
            "news_brief_proposals",
            "news_story_analysis_current",
            "news_brief_publications",
            "news_story_analysis_publications",
            "news_ai_current_targets",
            "news_ai_attempts",
            "news_story_analysis_requests",
            "news_brief_selections",
            "news_narrative_grouping_snapshots",
            "news_story_material_events",
            "news_story_identity_decisions",
            "news_story_memberships",
            "news_story_profiles",
            "news_stories",
            "news_article_identity_features",
            "news_story_projection_checkpoints",
        )
        counts: dict[str, int] = {}
        for table in ordered_tables:
            result = self.conn.execute(f"DELETE FROM {table}")
            counts[table] = int(result.rowcount)
        return counts

    def project_pending_revisions(
        self,
        *,
        now_ms: int,
        limit: int,
    ) -> dict[str, int]:
        lock = self.conn.execute(
            "SELECT pg_try_advisory_xact_lock(%s) AS locked",
            (_PROJECTOR_LOCK_KEY,),
        ).fetchone()
        if lock is None or not bool(lock["locked"]):
            return {"processed": 0, "created": 0, "joined": 0, "revised": 0, "ambiguous": 0}
        checkpoint = self.conn.execute(
            """
            SELECT last_observed_at_ms, last_revision_id
              FROM news_story_projection_checkpoints
             WHERE identity_version = %s
             FOR UPDATE
            """,
            (STORY_IDENTITY_VERSION,),
        ).fetchone()
        if checkpoint is None:
            self.conn.execute(
                """
                INSERT INTO news_story_projection_checkpoints (
                  identity_version,
                  last_observed_at_ms,
                  last_revision_id,
                  updated_at_ms
                )
                VALUES (%s, 0, '', %s)
                """,
                (STORY_IDENTITY_VERSION, now_ms),
            )
            last_observed_at_ms = 0
            last_revision_id = ""
        else:
            last_observed_at_ms = int(checkpoint["last_observed_at_ms"])
            last_revision_id = str(checkpoint["last_revision_id"])
        rows = self.conn.execute(
            """
            SELECT
              revisions.*,
              articles.canonical_url,
              articles.publisher_organization_id,
              articles.identity_status AS article_identity_status,
              observations.source_id,
              sources.name AS source_name,
              sources.source_domain,
              sources.source_role,
              sources.trust_tier,
              sources.source_chain_id,
              sources.publisher_organization_id AS source_publisher_organization_id,
              sources.parent_organization_id,
              sources.known_relationships,
              sources.source_quality_factors
            FROM news_article_revisions AS revisions
            JOIN news_articles AS articles ON articles.article_id = revisions.article_id
            JOIN news_feed_observations AS observations
              ON observations.observation_id = revisions.observation_id
            JOIN news_sources AS sources ON sources.source_id = observations.source_id
            WHERE (revisions.observed_at_ms, revisions.revision_id) > (%s, %s)
            ORDER BY revisions.observed_at_ms, revisions.revision_id
            LIMIT %s
            """,
            (last_observed_at_ms, last_revision_id, limit),
        ).fetchall()
        counts = {"processed": 0, "created": 0, "joined": 0, "revised": 0, "ambiguous": 0}
        for raw_row in rows:
            row = dict(raw_row)
            outcome = self._project_revision(row=row, now_ms=now_ms)
            counts["processed"] += 1
            counts[outcome] += 1
            last_observed_at_ms = int(row["observed_at_ms"])
            last_revision_id = str(row["revision_id"])
        if rows:
            self.conn.execute(
                """
                UPDATE news_story_projection_checkpoints
                   SET last_observed_at_ms = %s,
                       last_revision_id = %s,
                       updated_at_ms = %s
                 WHERE identity_version = %s
                """,
                (
                    last_observed_at_ms,
                    last_revision_id,
                    now_ms,
                    STORY_IDENTITY_VERSION,
                ),
            )
        return counts

    def _project_revision(
        self,
        *,
        row: Mapping[str, object],
        now_ms: int,
    ) -> str:
        features = extract_identity_features(
            revision_id=str(row["revision_id"]),
            article_id=str(row["article_id"]),
            title=str(row["title"]),
            snippet=str(row.get("snippet") or ""),
            language=str(row["language"]),
        )
        self._persist_identity_features(features=features, now_ms=now_ms)
        existing_membership = self.conn.execute(
            """
            SELECT *
              FROM news_story_memberships
             WHERE article_id = %s
               AND membership_kind = 'primary'
            """,
            (row["article_id"],),
        ).fetchone()
        if existing_membership is not None:
            story_id = str(existing_membership["story_id"])
            candidates = self._candidate_payloads(
                features=features,
                observed_at_ms=_int(row["observed_at_ms"]),
                restrict_story_id=story_id,
            )
            decision = decide_story(article=features, candidates=candidates)
            if decision.verdict in {"reject_conflict", "ambiguous_new_story", "no_candidate_new_story"}:
                self._record_identity_decision(
                    row=row,
                    decision=decision.model_copy(
                        update={
                            "verdict": "revision_identity_ambiguous",
                            "selected_story_id": story_id,
                            "match_method": "revision_identity_check",
                        }
                    ),
                    now_ms=now_ms,
                )
                self.conn.execute(
                    """
                    UPDATE news_articles
                       SET identity_status = 'revision_identity_ambiguous',
                           updated_at_ms = %s
                     WHERE article_id = %s
                    """,
                    (now_ms, row["article_id"]),
                )
                return "ambiguous"
            members = self._story_member_rows(story_id=story_id)
            semantics = classify_member_semantics(
                source=row,
                revision=row,
                features=features,
                existing_members=[member for member in members if str(member["article_id"]) != str(row["article_id"])],
                is_seed=False,
            )
            if str(row.get("material_change_kind")) == "source_time":
                semantics = semantics.model_copy(
                    update={
                        "content_form": str(existing_membership["content_form"]),
                        "origin_relation": str(existing_membership["origin_relation"]),
                        "development_relation": str(existing_membership["development_relation"]),
                        "epistemic_use": str(existing_membership["epistemic_use"]),
                        "reporting_origin_id": existing_membership["reporting_origin_id"],
                        "origin_confidence": float(existing_membership["origin_confidence"]),
                        "reason": {
                            **dict(_mapping(existing_membership["semantics_reason"])),
                            "source_time_only_revision": True,
                        },
                    }
                )
            self._update_membership_revision(
                story_id=story_id,
                row=row,
                features=features,
                semantics=semantics,
                now_ms=now_ms,
            )
            compatible = decision.model_copy(
                update={
                    "verdict": "revision_compatible",
                    "selected_story_id": story_id,
                    "match_method": "revision_identity_check",
                }
            )
            self._record_identity_decision(row=row, decision=compatible, now_ms=now_ms)
            self._reproject_story(
                story_id=story_id,
                triggering_revision_id=str(row["revision_id"]),
                now_ms=now_ms,
            )
            return "revised"

        candidates = self._candidate_payloads(
            features=features,
            observed_at_ms=_int(row["observed_at_ms"]),
        )
        decision = decide_story(article=features, candidates=candidates)
        if decision.selected_story_id is None:
            story_id = story_id_for_seed(str(row["article_id"]))
            semantics = classify_member_semantics(
                source=row,
                revision=row,
                features=features,
                existing_members=[],
                is_seed=True,
            )
            member = self._member_projection_row(
                row=row,
                features=features,
                semantics=semantics,
                membership_kind="primary",
            )
            projection = project_story((member,), now_ms=now_ms)
            self._insert_story(
                story_id=story_id,
                seed_article_id=str(row["article_id"]),
                projection=projection,
                now_ms=now_ms,
            )
            self._insert_membership(
                story_id=story_id,
                row=row,
                features=features,
                semantics=semantics,
                verdict="seed",
                match_method="seed",
                match_score=1.0,
                runner_up_margin=1.0,
                match_reason={
                    "identity_version": STORY_IDENTITY_VERSION,
                    "source_verdict": decision.verdict,
                },
                now_ms=now_ms,
            )
            self._upsert_story_profile(
                story_id=story_id,
                projection=projection,
                now_ms=now_ms,
            )
            recorded_decision = decision.model_copy(update={"selected_story_id": story_id})
            self._record_identity_decision(
                row=row,
                decision=recorded_decision,
                now_ms=now_ms,
            )
            self._insert_material_event(
                story_id=story_id,
                revision_id=str(row["revision_id"]),
                event_kind="first_report",
                factors={
                    "identity_verdict": decision.verdict,
                    "material_evidence_hash": projection.material_evidence_hash,
                },
                occurred_at_ms=_int(row["observed_at_ms"]),
            )
            self._maybe_request_automatic_analysis(
                story_id=story_id,
                previous_material_hash=None,
                projection=projection,
                now_ms=now_ms,
            )
            return "created"

        story_id = decision.selected_story_id
        members = self._story_member_rows(story_id=story_id)
        semantics = classify_member_semantics(
            source=row,
            revision=row,
            features=features,
            existing_members=members,
            is_seed=False,
        )
        self._insert_membership(
            story_id=story_id,
            row=row,
            features=features,
            semantics=semantics,
            verdict=decision.verdict,
            match_method=decision.match_method,
            match_score=decision.match_score,
            runner_up_margin=decision.runner_up_margin,
            match_reason=decision.reason,
            now_ms=now_ms,
        )
        self._record_identity_decision(row=row, decision=decision, now_ms=now_ms)
        self._reproject_story(
            story_id=story_id,
            triggering_revision_id=str(row["revision_id"]),
            now_ms=now_ms,
        )
        return "joined"

    def _persist_identity_features(
        self,
        *,
        features: ArticleIdentityFeatures,
        now_ms: int,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO news_article_identity_features (
              revision_id,
              identity_version,
              article_id,
              language,
              normalized_title,
              normalized_lead,
              content_fingerprint,
              lexical_signature,
              event_key,
              named_event_keys,
              features,
              extraction_receipt,
              feature_hash,
              created_at_ms
            )
            VALUES (
              %(revision_id)s,
              %(identity_version)s,
              %(article_id)s,
              %(language)s,
              %(normalized_title)s,
              %(normalized_lead)s,
              %(content_fingerprint)s,
              %(lexical_signature)s,
              %(event_key)s,
              %(named_event_keys)s,
              %(features)s,
              %(extraction_receipt)s,
              %(feature_hash)s,
              %(created_at_ms)s
            )
            ON CONFLICT (revision_id, identity_version) DO UPDATE SET
              feature_hash = EXCLUDED.feature_hash,
              normalized_title = EXCLUDED.normalized_title,
              normalized_lead = EXCLUDED.normalized_lead,
              content_fingerprint = EXCLUDED.content_fingerprint,
              lexical_signature = EXCLUDED.lexical_signature,
              event_key = EXCLUDED.event_key,
              named_event_keys = EXCLUDED.named_event_keys,
              features = EXCLUDED.features,
              extraction_receipt = EXCLUDED.extraction_receipt
            """,
            {
                **features.model_dump(
                    exclude={
                        "named_event_keys",
                        "entities",
                        "actions",
                        "locations",
                        "stages",
                        "quantities",
                        "tokens",
                        "bigrams",
                        "chargrams",
                        "extraction_receipt",
                    }
                ),
                "named_event_keys": Jsonb(list(features.named_event_keys)),
                "features": Jsonb(features.storage_features()),
                "extraction_receipt": Jsonb(features.extraction_receipt),
                "created_at_ms": now_ms,
            },
        )

    def _candidate_payloads(
        self,
        *,
        features: ArticleIdentityFeatures,
        observed_at_ms: int,
        restrict_story_id: str | None = None,
    ) -> list[dict[str, object]]:
        if restrict_story_id is not None:
            channel_hits = {restrict_story_id: {"revision_story"}}
        else:
            channel_hits = self._candidate_story_channels(
                features=features,
                observed_at_ms=observed_at_ms,
            )
        if not channel_hits:
            return []
        candidate_ids = sorted(channel_hits)
        rows = self.conn.execute(
            """
            SELECT
              stories.story_id,
              stories.last_material_evidence_at_ms,
              profiles.profile
            FROM news_stories AS stories
            JOIN news_story_profiles AS profiles ON profiles.story_id = stories.story_id
            WHERE stories.identity_version = %s
              AND stories.story_id = ANY(%s)
            ORDER BY stories.last_material_evidence_at_ms DESC, stories.story_id
            """,
            (STORY_IDENTITY_VERSION, candidate_ids),
        ).fetchall()
        feature_rows = self.conn.execute(
            """
            SELECT
              memberships.story_id,
              features.*,
              revisions.observed_at_ms
            FROM news_story_memberships AS memberships
            JOIN news_article_identity_features AS features
              ON features.revision_id = memberships.revision_id
             AND features.identity_version = %s
            JOIN news_article_revisions AS revisions
              ON revisions.revision_id = memberships.revision_id
            WHERE memberships.story_id = ANY(%s)
              AND memberships.membership_kind = 'primary'
            ORDER BY memberships.story_id, revisions.observed_at_ms, revisions.revision_id
            """,
            (ARTICLE_IDENTITY_VERSION, candidate_ids),
        ).fetchall()
        features_by_story: defaultdict[str, list[dict[str, object]]] = defaultdict(list)
        for feature_row in feature_rows:
            features_by_story[str(feature_row["story_id"])].append(dict(feature_row))
        candidates: list[dict[str, object]] = []
        for row in rows:
            story_id = str(row["story_id"])
            member_features = features_by_story.get(story_id, [])
            matching_event_key = next(
                (
                    str(member["event_key"])
                    for member in member_features
                    if str(member["event_key"]) == features.event_key
                ),
                str(member_features[0]["event_key"]) if member_features else "",
            )
            candidates.append(
                {
                    "story_id": story_id,
                    "profile": dict(_mapping(row["profile"])),
                    "member_features": member_features,
                    "channel_hits": sorted(channel_hits[story_id]),
                    "event_key": matching_event_key,
                }
            )
        return candidates

    def _candidate_story_channels(
        self,
        *,
        features: ArticleIdentityFeatures,
        observed_at_ms: int,
    ) -> dict[str, set[str]]:
        rows = self.conn.execute(
            """
            WITH content_candidates AS (
              SELECT memberships.story_id, 'content_fingerprint'::text AS channel
                FROM news_article_identity_features AS candidate_features
                JOIN news_article_revisions AS revisions
                  ON revisions.revision_id = candidate_features.revision_id
                JOIN news_story_memberships AS memberships
                  ON memberships.revision_id = candidate_features.revision_id
                 AND memberships.membership_kind = 'primary'
               WHERE candidate_features.identity_version = %s
                 AND candidate_features.content_fingerprint = %s
                 AND revisions.observed_at_ms BETWEEN %s AND %s
               ORDER BY revisions.observed_at_ms DESC, candidate_features.revision_id
               LIMIT 50
            ),
            exact_title_candidates AS (
              SELECT memberships.story_id, 'normalized_exact_title'::text AS channel
                FROM news_article_identity_features AS candidate_features
                JOIN news_article_revisions AS revisions
                  ON revisions.revision_id = candidate_features.revision_id
                JOIN news_story_memberships AS memberships
                  ON memberships.revision_id = candidate_features.revision_id
                 AND memberships.membership_kind = 'primary'
               WHERE candidate_features.identity_version = %s
                 AND candidate_features.language = %s
                 AND candidate_features.normalized_title = %s
                 AND candidate_features.normalized_title <> ''
                 AND revisions.observed_at_ms BETWEEN %s AND %s
               ORDER BY revisions.observed_at_ms DESC, candidate_features.revision_id
               LIMIT 100
            ),
            title_containment_candidates AS (
              SELECT memberships.story_id, 'title_containment'::text AS channel
                FROM news_article_identity_features AS candidate_features
                JOIN news_article_revisions AS revisions
                  ON revisions.revision_id = candidate_features.revision_id
                JOIN news_story_memberships AS memberships
                  ON memberships.revision_id = candidate_features.revision_id
                 AND memberships.membership_kind = 'primary'
               WHERE candidate_features.identity_version = %s
                 AND candidate_features.language = %s
                 AND least(
                       length(candidate_features.normalized_title),
                       length(%s)
                     ) >= 20
                 AND (
                   candidate_features.normalized_title LIKE '%%' || %s || '%%'
                   OR %s LIKE '%%' || candidate_features.normalized_title || '%%'
                 )
                 AND revisions.observed_at_ms BETWEEN %s AND %s
               ORDER BY revisions.observed_at_ms DESC, candidate_features.revision_id
               LIMIT 100
            ),
            event_candidates AS (
              SELECT memberships.story_id, 'event_anchor'::text AS channel
                FROM news_article_identity_features AS candidate_features
                JOIN news_article_revisions AS revisions
                  ON revisions.revision_id = candidate_features.revision_id
                JOIN news_story_memberships AS memberships
                  ON memberships.revision_id = candidate_features.revision_id
                 AND memberships.membership_kind = 'primary'
               WHERE candidate_features.identity_version = %s
                 AND candidate_features.event_key = %s
                 AND revisions.observed_at_ms BETWEEN %s AND %s
               ORDER BY revisions.observed_at_ms DESC, candidate_features.revision_id
               LIMIT 100
            ),
            lexical_candidates AS (
              SELECT memberships.story_id, 'same_language_lexical'::text AS channel
                FROM news_article_identity_features AS candidate_features
                JOIN news_article_revisions AS revisions
                  ON revisions.revision_id = candidate_features.revision_id
                JOIN news_story_memberships AS memberships
                  ON memberships.revision_id = candidate_features.revision_id
                 AND memberships.membership_kind = 'primary'
               WHERE candidate_features.identity_version = %s
                 AND candidate_features.language = %s
                 AND candidate_features.normalized_title %% %s
                 AND revisions.observed_at_ms BETWEEN %s AND %s
               ORDER BY
                 similarity(candidate_features.normalized_title, %s) DESC,
                 revisions.observed_at_ms DESC,
                 candidate_features.revision_id
               LIMIT 100
            ),
            named_event_candidates AS (
              SELECT memberships.story_id, 'named_event'::text AS channel
                FROM news_article_identity_features AS candidate_features
                JOIN news_article_revisions AS revisions
                  ON revisions.revision_id = candidate_features.revision_id
                JOIN news_story_memberships AS memberships
                  ON memberships.revision_id = candidate_features.revision_id
                 AND memberships.membership_kind = 'primary'
               WHERE candidate_features.identity_version = %s
                 AND cardinality(%s::text[]) > 0
                 AND candidate_features.named_event_keys ?| %s::text[]
                 AND revisions.observed_at_ms BETWEEN %s AND %s
               ORDER BY revisions.observed_at_ms DESC, candidate_features.revision_id
               LIMIT 100
            ),
            combined AS (
              SELECT * FROM content_candidates
              UNION ALL
              SELECT * FROM exact_title_candidates
              UNION ALL
              SELECT * FROM title_containment_candidates
              UNION ALL
              SELECT * FROM event_candidates
              UNION ALL
              SELECT * FROM lexical_candidates
              UNION ALL
              SELECT * FROM named_event_candidates
            )
            SELECT story_id, array_agg(DISTINCT channel ORDER BY channel) AS channels
              FROM combined
             GROUP BY story_id
             ORDER BY story_id
             LIMIT 250
            """,
            (
                ARTICLE_IDENTITY_VERSION,
                features.content_fingerprint,
                observed_at_ms - ANCHORED_CANDIDATE_WINDOW_MS,
                observed_at_ms + ANCHORED_CANDIDATE_WINDOW_MS,
                ARTICLE_IDENTITY_VERSION,
                features.language,
                features.normalized_title,
                observed_at_ms - LEXICAL_CANDIDATE_WINDOW_MS,
                observed_at_ms + LEXICAL_CANDIDATE_WINDOW_MS,
                ARTICLE_IDENTITY_VERSION,
                features.language,
                features.normalized_title,
                features.normalized_title,
                features.normalized_title,
                observed_at_ms - LEXICAL_CANDIDATE_WINDOW_MS,
                observed_at_ms + LEXICAL_CANDIDATE_WINDOW_MS,
                ARTICLE_IDENTITY_VERSION,
                features.event_key,
                observed_at_ms - ANCHORED_CANDIDATE_WINDOW_MS,
                observed_at_ms + ANCHORED_CANDIDATE_WINDOW_MS,
                ARTICLE_IDENTITY_VERSION,
                features.language,
                features.normalized_title,
                observed_at_ms - LEXICAL_CANDIDATE_WINDOW_MS,
                observed_at_ms + LEXICAL_CANDIDATE_WINDOW_MS,
                features.normalized_title,
                ARTICLE_IDENTITY_VERSION,
                list(features.named_event_keys),
                list(features.named_event_keys),
                observed_at_ms - NAMED_EVENT_CANDIDATE_WINDOW_MS,
                observed_at_ms + NAMED_EVENT_CANDIDATE_WINDOW_MS,
            ),
        ).fetchall()
        return {str(row["story_id"]): {str(channel) for channel in _sequence(row["channels"])} for row in rows}

    def _story_feature_rows(self, *, story_id: str) -> list[dict[str, object]]:
        rows = self.conn.execute(
            """
            SELECT
              features.*,
              revisions.observed_at_ms
            FROM news_story_memberships AS memberships
            JOIN news_article_identity_features AS features
              ON features.revision_id = memberships.revision_id
             AND features.identity_version = %s
            JOIN news_article_revisions AS revisions
              ON revisions.revision_id = memberships.revision_id
            WHERE memberships.story_id = %s
              AND memberships.membership_kind = 'primary'
            ORDER BY revisions.observed_at_ms, revisions.revision_id
            """,
            (ARTICLE_IDENTITY_VERSION, story_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def _story_member_rows(self, *, story_id: str) -> list[dict[str, object]]:
        rows = self.conn.execute(
            """
            SELECT
              memberships.*,
              revisions.title,
              revisions.snippet,
              revisions.source_published_at_ms,
              revisions.observed_at_ms,
              revisions.language,
              revisions.content_hash,
              articles.canonical_url,
              observations.source_id,
              sources.name AS source_name,
              sources.source_domain,
              sources.source_role,
              sources.trust_tier,
              sources.source_chain_id,
              sources.publisher_organization_id,
              sources.parent_organization_id,
              features.normalized_title,
              features.normalized_lead,
              features.content_fingerprint,
              features.lexical_signature,
              features.event_key,
              features.named_event_keys,
              features.features,
              features.feature_hash,
              content.content_snapshot_id,
              content.status AS content_snapshot_status,
              content.content_hash AS snapshot_content_hash,
              content.extracted_text,
              content.failure_reason AS content_failure_reason,
              content.fetched_at_ms AS content_fetched_at_ms
            FROM news_story_memberships AS memberships
            JOIN news_article_revisions AS revisions
              ON revisions.revision_id = memberships.revision_id
            JOIN news_articles AS articles ON articles.article_id = memberships.article_id
            JOIN news_feed_observations AS observations
              ON observations.observation_id = revisions.observation_id
            JOIN news_sources AS sources ON sources.source_id = observations.source_id
            JOIN news_article_identity_features AS features
              ON features.revision_id = memberships.revision_id
             AND features.identity_version = %s
            LEFT JOIN LATERAL (
              SELECT snapshots.*
                FROM news_article_content_snapshots AS snapshots
               WHERE snapshots.revision_id = revisions.revision_id
               ORDER BY
                 CASE snapshots.status
                   WHEN 'available' THEN 0
                   WHEN 'truncated' THEN 1
                   ELSE 2
                 END,
                 snapshots.updated_at_ms DESC,
                 snapshots.content_snapshot_id
               LIMIT 1
            ) AS content ON true
            WHERE memberships.story_id = %s
            ORDER BY revisions.observed_at_ms, revisions.revision_id
            """,
            (ARTICLE_IDENTITY_VERSION, story_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def _story_evidence_rows(self, *, story_id: str) -> list[dict[str, object]]:
        rows = self.conn.execute(
            """
            SELECT
              memberships.membership_kind,
              memberships.content_form,
              memberships.origin_relation,
              memberships.development_relation,
              memberships.epistemic_use,
              memberships.reporting_origin_id,
              memberships.origin_confidence,
              revisions.article_id,
              revisions.revision_id,
              revisions.revision_number,
              revisions.title,
              revisions.snippet,
              revisions.source_published_at_ms,
              revisions.observed_at_ms,
              revisions.language,
              revisions.content_hash,
              revisions.material_change_kind,
              articles.canonical_url,
              observations.source_id,
              sources.name AS source_name,
              sources.source_domain,
              sources.source_role,
              sources.trust_tier,
              sources.source_chain_id,
              sources.publisher_organization_id,
              content.content_snapshot_id,
              content.status AS content_snapshot_status,
              content.content_hash AS snapshot_content_hash,
              content.extracted_text,
              content.fetched_at_ms AS content_fetched_at_ms
            FROM news_story_memberships AS memberships
            JOIN news_article_revisions AS revisions
              ON revisions.article_id = memberships.article_id
            JOIN news_articles AS articles ON articles.article_id = revisions.article_id
            JOIN news_feed_observations AS observations
              ON observations.observation_id = revisions.observation_id
            JOIN news_sources AS sources ON sources.source_id = observations.source_id
            JOIN news_article_identity_features AS features
              ON features.revision_id = revisions.revision_id
             AND features.identity_version = %s
            LEFT JOIN LATERAL (
              SELECT snapshots.*
                FROM news_article_content_snapshots AS snapshots
               WHERE snapshots.revision_id = revisions.revision_id
                 AND snapshots.status IN ('available', 'truncated')
               ORDER BY
                 CASE snapshots.status WHEN 'available' THEN 0 ELSE 1 END,
                 snapshots.updated_at_ms DESC,
                 snapshots.content_snapshot_id
               LIMIT 1
            ) AS content ON true
            WHERE memberships.story_id = %s
              AND memberships.membership_kind = 'primary'
              AND memberships.epistemic_use = 'fact_evidence'
            ORDER BY revisions.observed_at_ms, revisions.revision_id
            """,
            (ARTICLE_IDENTITY_VERSION, story_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def _member_projection_row(
        self,
        *,
        row: Mapping[str, object],
        features: ArticleIdentityFeatures,
        semantics: Any,
        membership_kind: str,
    ) -> dict[str, object]:
        return {
            **dict(row),
            "membership_kind": membership_kind,
            "content_form": semantics.content_form,
            "origin_relation": semantics.origin_relation,
            "development_relation": semantics.development_relation,
            "epistemic_use": semantics.epistemic_use,
            "reporting_origin_id": semantics.reporting_origin_id,
            "origin_confidence": semantics.origin_confidence,
            "normalized_title": features.normalized_title,
            "normalized_lead": features.normalized_lead,
            "content_fingerprint": features.content_fingerprint,
            "lexical_signature": features.lexical_signature,
            "event_key": features.event_key,
            "named_event_keys": list(features.named_event_keys),
            "features": features.storage_features(),
            "feature_hash": features.feature_hash,
        }

    def _insert_story(
        self,
        *,
        story_id: str,
        seed_article_id: str,
        projection: Any,
        now_ms: int,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO news_stories (
              story_id,
              seed_article_id,
              representative_revision_id,
              identity_version,
              identity_status,
              title,
              snippet,
              languages,
              event_core,
              first_seen_at_ms,
              last_material_evidence_at_ms,
              material_evolution_state,
              lifecycle,
              breaking,
              lifecycle_version,
              impact_profile,
              priority_profile,
              impact_score,
              priority_score,
              scoring_version,
              evidence_posture,
              evidence_factors,
              article_count,
              primary_member_count,
              contextual_member_count,
              reporting_origin_count,
              independent_origin_count,
              syndicated_article_count,
              material_evidence_hash,
              presentation_state_hash,
              brief_eligible,
              brief_eligibility_reason,
              created_at_ms,
              updated_at_ms
            )
            VALUES (
              %(story_id)s,
              %(seed_article_id)s,
              %(representative_revision_id)s,
              %(identity_version)s,
              'stable',
              %(title)s,
              %(snippet)s,
              %(languages)s,
              %(event_core)s,
              %(first_seen_at_ms)s,
              %(last_material_evidence_at_ms)s,
              %(material_evolution_state)s,
              %(lifecycle)s,
              %(breaking)s,
              %(lifecycle_version)s,
              %(impact_profile)s,
              %(priority_profile)s,
              %(impact_score)s,
              %(priority_score)s,
              %(scoring_version)s,
              %(evidence_posture)s,
              %(evidence_factors)s,
              %(article_count)s,
              %(primary_member_count)s,
              %(contextual_member_count)s,
              %(reporting_origin_count)s,
              %(independent_origin_count)s,
              %(syndicated_article_count)s,
              %(material_evidence_hash)s,
              %(presentation_state_hash)s,
              %(brief_eligible)s,
              %(brief_eligibility_reason)s,
              %(created_at_ms)s,
              %(updated_at_ms)s
            )
            """,
            self._projection_params(
                story_id=story_id,
                seed_article_id=seed_article_id,
                projection=projection,
                now_ms=now_ms,
            ),
        )

    def _insert_membership(
        self,
        *,
        story_id: str,
        row: Mapping[str, object],
        features: ArticleIdentityFeatures,
        semantics: Any,
        verdict: str,
        match_method: str,
        match_score: float,
        runner_up_margin: float,
        match_reason: Mapping[str, object],
        now_ms: int,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO news_story_memberships (
              story_id,
              article_id,
              revision_id,
              membership_kind,
              identity_version,
              verdict,
              match_method,
              match_score,
              runner_up_margin,
              match_reason,
              content_form,
              origin_relation,
              development_relation,
              epistemic_use,
              reporting_origin_id,
              origin_confidence,
              semantics_reason,
              admitted_at_ms,
              updated_at_ms
            )
            VALUES (
              %(story_id)s,
              %(article_id)s,
              %(revision_id)s,
              'primary',
              %(identity_version)s,
              %(verdict)s,
              %(match_method)s,
              %(match_score)s,
              %(runner_up_margin)s,
              %(match_reason)s,
              %(content_form)s,
              %(origin_relation)s,
              %(development_relation)s,
              %(epistemic_use)s,
              %(reporting_origin_id)s,
              %(origin_confidence)s,
              %(semantics_reason)s,
              %(admitted_at_ms)s,
              %(updated_at_ms)s
            )
            """,
            {
                "story_id": story_id,
                "article_id": row["article_id"],
                "revision_id": row["revision_id"],
                "identity_version": STORY_IDENTITY_VERSION,
                "verdict": verdict,
                "match_method": match_method,
                "match_score": match_score,
                "runner_up_margin": runner_up_margin,
                "match_reason": Jsonb(dict(match_reason)),
                "content_form": semantics.content_form,
                "origin_relation": semantics.origin_relation,
                "development_relation": semantics.development_relation,
                "epistemic_use": semantics.epistemic_use,
                "reporting_origin_id": semantics.reporting_origin_id,
                "origin_confidence": semantics.origin_confidence,
                "semantics_reason": Jsonb(dict(semantics.reason)),
                "admitted_at_ms": row["observed_at_ms"],
                "updated_at_ms": now_ms,
            },
        )

    def _update_membership_revision(
        self,
        *,
        story_id: str,
        row: Mapping[str, object],
        features: ArticleIdentityFeatures,
        semantics: Any,
        now_ms: int,
    ) -> None:
        del features
        self.conn.execute(
            """
            UPDATE news_story_memberships
               SET revision_id = %(revision_id)s,
                   verdict = 'accept_scored',
                   match_method = 'revision_compatible',
                   match_score = 1,
                   runner_up_margin = 1,
                   match_reason = %(match_reason)s,
                   content_form = %(content_form)s,
                   origin_relation = %(origin_relation)s,
                   development_relation = %(development_relation)s,
                   epistemic_use = %(epistemic_use)s,
                   reporting_origin_id = %(reporting_origin_id)s,
                   origin_confidence = %(origin_confidence)s,
                   semantics_reason = %(semantics_reason)s,
                   updated_at_ms = %(updated_at_ms)s
             WHERE story_id = %(story_id)s
               AND article_id = %(article_id)s
               AND membership_kind = 'primary'
            """,
            {
                "revision_id": row["revision_id"],
                "match_reason": Jsonb({"revision_compatible": True}),
                "content_form": semantics.content_form,
                "origin_relation": semantics.origin_relation,
                "development_relation": semantics.development_relation,
                "epistemic_use": semantics.epistemic_use,
                "reporting_origin_id": semantics.reporting_origin_id,
                "origin_confidence": semantics.origin_confidence,
                "semantics_reason": Jsonb(dict(semantics.reason)),
                "updated_at_ms": now_ms,
                "story_id": story_id,
                "article_id": row["article_id"],
            },
        )

    def _reproject_story(
        self,
        *,
        story_id: str,
        triggering_revision_id: str,
        now_ms: int,
    ) -> None:
        previous = self.conn.execute(
            "SELECT * FROM news_stories WHERE story_id = %s FOR UPDATE",
            (story_id,),
        ).fetchone()
        if previous is None:
            raise RuntimeError("news_story_projection_missing")
        members = self._story_member_rows(story_id=story_id)
        projection = project_story(members, now_ms=now_ms, previous=dict(previous))
        self.conn.execute(
            """
            UPDATE news_stories
               SET representative_revision_id = %(representative_revision_id)s,
                   title = %(title)s,
                   snippet = %(snippet)s,
                   languages = %(languages)s,
                   event_core = %(event_core)s,
                   first_seen_at_ms = %(first_seen_at_ms)s,
                   last_material_evidence_at_ms = %(last_material_evidence_at_ms)s,
                   material_evolution_state = %(material_evolution_state)s,
                   lifecycle = %(lifecycle)s,
                   breaking = %(breaking)s,
                   lifecycle_version = %(lifecycle_version)s,
                   impact_profile = %(impact_profile)s,
                   priority_profile = %(priority_profile)s,
                   impact_score = %(impact_score)s,
                   priority_score = %(priority_score)s,
                   scoring_version = %(scoring_version)s,
                   evidence_posture = %(evidence_posture)s,
                   evidence_factors = %(evidence_factors)s,
                   article_count = %(article_count)s,
                   primary_member_count = %(primary_member_count)s,
                   contextual_member_count = %(contextual_member_count)s,
                   reporting_origin_count = %(reporting_origin_count)s,
                   independent_origin_count = %(independent_origin_count)s,
                   syndicated_article_count = %(syndicated_article_count)s,
                   material_evidence_hash = %(material_evidence_hash)s,
                   presentation_state_hash = %(presentation_state_hash)s,
                   brief_eligible = %(brief_eligible)s,
                   brief_eligibility_reason = %(brief_eligibility_reason)s,
                   updated_at_ms = %(updated_at_ms)s
             WHERE story_id = %(story_id)s
            """,
            self._projection_params(
                story_id=story_id,
                seed_article_id=str(previous["seed_article_id"]),
                projection=projection,
                now_ms=now_ms,
            ),
        )
        self._upsert_story_profile(
            story_id=story_id,
            projection=projection,
            now_ms=now_ms,
        )
        material_changed = str(previous["material_evidence_hash"]) != projection.material_evidence_hash
        if material_changed:
            event_kind = projection.material_evolution_state
            if event_kind not in {
                "first_report",
                "new_independent_origin",
                "material_follow_up",
                "material_correction",
                "conflict_detected",
                "conflict_resolved",
                "retraction",
            }:
                event_kind = "material_follow_up"
            self._insert_material_event(
                story_id=story_id,
                revision_id=triggering_revision_id,
                event_kind=event_kind,
                factors={
                    "previous_material_evidence_hash": previous["material_evidence_hash"],
                    "material_evidence_hash": projection.material_evidence_hash,
                },
                occurred_at_ms=projection.last_material_evidence_at_ms,
            )
        self._maybe_request_automatic_analysis(
            story_id=story_id,
            previous_material_hash=str(previous["material_evidence_hash"]),
            projection=projection,
            now_ms=now_ms,
        )

    def refresh_story_presentation(
        self,
        *,
        now_ms: int,
        limit: int,
    ) -> int:
        rows = self.conn.execute(
            """
            SELECT story_id
              FROM news_stories
             ORDER BY last_material_evidence_at_ms DESC, story_id
             LIMIT %s
            """,
            (limit,),
        ).fetchall()
        changed = 0
        for row in rows:
            story_id = str(row["story_id"])
            previous = self.conn.execute(
                "SELECT * FROM news_stories WHERE story_id = %s",
                (story_id,),
            ).fetchone()
            if previous is None:
                continue
            projection = project_story(
                self._story_member_rows(story_id=story_id),
                now_ms=now_ms,
                previous=dict(previous),
            )
            if str(previous["presentation_state_hash"]) == projection.presentation_state_hash:
                continue
            self.conn.execute(
                """
                UPDATE news_stories
                   SET lifecycle = %s,
                       breaking = %s,
                       priority_profile = %s,
                       priority_score = %s,
                       presentation_state_hash = %s,
                       brief_eligible = %s,
                       brief_eligibility_reason = %s,
                       updated_at_ms = %s
                 WHERE story_id = %s
                """,
                (
                    projection.lifecycle,
                    projection.breaking,
                    Jsonb(projection.priority_profile),
                    projection.priority_score,
                    projection.presentation_state_hash,
                    projection.brief_eligible,
                    Jsonb(projection.brief_eligibility_reason),
                    now_ms,
                    story_id,
                ),
            )
            changed += 1
        return changed

    def _projection_params(
        self,
        *,
        story_id: str,
        seed_article_id: str,
        projection: Any,
        now_ms: int,
    ) -> dict[str, object]:
        return {
            "story_id": story_id,
            "seed_article_id": seed_article_id,
            "representative_revision_id": projection.representative_revision_id,
            "identity_version": STORY_IDENTITY_VERSION,
            "title": projection.title,
            "snippet": projection.snippet,
            "languages": Jsonb(list(projection.languages)),
            "event_core": Jsonb(projection.event_core),
            "first_seen_at_ms": projection.first_seen_at_ms,
            "last_material_evidence_at_ms": projection.last_material_evidence_at_ms,
            "material_evolution_state": projection.material_evolution_state,
            "lifecycle": projection.lifecycle,
            "breaking": projection.breaking,
            "lifecycle_version": STORY_LIFECYCLE_VERSION,
            "impact_profile": Jsonb(projection.impact_profile),
            "priority_profile": Jsonb(projection.priority_profile),
            "impact_score": projection.impact_score,
            "priority_score": projection.priority_score,
            "scoring_version": STORY_SCORING_VERSION,
            "evidence_posture": projection.evidence_posture,
            "evidence_factors": Jsonb(projection.evidence_factors),
            "article_count": projection.article_count,
            "primary_member_count": projection.primary_member_count,
            "contextual_member_count": projection.contextual_member_count,
            "reporting_origin_count": projection.reporting_origin_count,
            "independent_origin_count": projection.independent_origin_count,
            "syndicated_article_count": projection.syndicated_article_count,
            "material_evidence_hash": projection.material_evidence_hash,
            "presentation_state_hash": projection.presentation_state_hash,
            "brief_eligible": projection.brief_eligible,
            "brief_eligibility_reason": Jsonb(projection.brief_eligibility_reason),
            "created_at_ms": now_ms,
            "updated_at_ms": now_ms,
        }

    def _upsert_story_profile(
        self,
        *,
        story_id: str,
        projection: Any,
        now_ms: int,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO news_story_profiles (
              story_id,
              identity_version,
              profile,
              profile_hash,
              updated_at_ms
            )
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (story_id) DO UPDATE SET
              identity_version = EXCLUDED.identity_version,
              profile = EXCLUDED.profile,
              profile_hash = EXCLUDED.profile_hash,
              updated_at_ms = EXCLUDED.updated_at_ms
            """,
            (
                story_id,
                STORY_IDENTITY_VERSION,
                Jsonb(projection.profile),
                projection.profile_hash,
                now_ms,
            ),
        )

    def _record_identity_decision(
        self,
        *,
        row: Mapping[str, object],
        decision: Any,
        now_ms: int,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO news_story_identity_decisions (
              decision_id,
              revision_id,
              article_id,
              identity_version,
              selected_story_id,
              verdict,
              candidates,
              decision_reason,
              decided_at_ms
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (revision_id, identity_version) DO UPDATE SET
              selected_story_id = EXCLUDED.selected_story_id,
              verdict = EXCLUDED.verdict,
              candidates = EXCLUDED.candidates,
              decision_reason = EXCLUDED.decision_reason,
              decided_at_ms = EXCLUDED.decided_at_ms
            """,
            (
                identity_decision_id(str(row["revision_id"])),
                row["revision_id"],
                row["article_id"],
                STORY_IDENTITY_VERSION,
                decision.selected_story_id,
                decision.verdict,
                Jsonb([candidate.model_dump(mode="json") for candidate in decision.candidates]),
                Jsonb(
                    {
                        "match_method": decision.match_method,
                        "match_score": decision.match_score,
                        "runner_up_margin": decision.runner_up_margin,
                        "reason": decision.reason,
                    }
                ),
                now_ms,
            ),
        )

    def _insert_material_event(
        self,
        *,
        story_id: str,
        revision_id: str | None,
        event_kind: str,
        factors: Mapping[str, object],
        occurred_at_ms: int,
    ) -> None:
        event_id = deterministic_id(
            "story-material-event",
            story_id,
            revision_id or "",
            event_kind,
        )
        self.conn.execute(
            """
            INSERT INTO news_story_material_events (
              material_event_id,
              story_id,
              revision_id,
              event_kind,
              event_factors,
              occurred_at_ms
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (story_id, revision_id, event_kind) DO NOTHING
            """,
            (
                event_id,
                story_id,
                revision_id,
                event_kind,
                Jsonb(dict(factors)),
                occurred_at_ms,
            ),
        )

    def _maybe_request_automatic_analysis(
        self,
        *,
        story_id: str,
        previous_material_hash: str | None,
        projection: Any,
        now_ms: int,
    ) -> None:
        if previous_material_hash == projection.material_evidence_hash:
            return
        story = {
            "story_id": story_id,
            "material_evidence_hash": projection.material_evidence_hash,
            "impact_score": projection.impact_score,
            "evidence_posture": projection.evidence_posture,
        }
        self._ensure_automatic_analysis_request(
            story=story,
            reason={
                "impact_score": projection.impact_score,
                "material_evolution_state": projection.material_evolution_state,
            },
            now_ms=now_ms,
        )

    def _ensure_automatic_analysis_request(
        self,
        *,
        story: Mapping[str, object],
        reason: Mapping[str, object],
        now_ms: int,
    ) -> None:
        story_id = str(story["story_id"])
        material_evidence_hash = str(story["material_evidence_hash"])
        if _int(story.get("impact_score")) < 75 or str(story.get("evidence_posture")) == "withdrawn":
            return
        evidence = self.story_analysis_evidence(story_id=story_id)
        if not _story_analysis_evidence_sufficient(evidence):
            return
        request_id = deterministic_id(
            "story-analysis-request",
            story_id,
            material_evidence_hash,
            "automatic",
        )
        self.conn.execute(
            """
            INSERT INTO news_story_analysis_requests (
              request_id,
              story_id,
              material_evidence_hash,
              request_kind,
              reason,
              status,
              requested_at_ms,
              updated_at_ms
            )
            VALUES (%s, %s, %s, 'automatic', %s, 'pending', %s, %s)
            ON CONFLICT (story_id, material_evidence_hash, request_kind) DO NOTHING
            """,
            (
                request_id,
                story_id,
                material_evidence_hash,
                Jsonb(dict(reason)),
                now_ms,
                now_ms,
            ),
        )

    # Public Story reads --------------------------------------------------------------

    def list_story_rows(
        self,
        *,
        limit: int,
        cursor: tuple[int, int, str] | tuple[int, str] | None,
        view: str,
        q: str | None,
        evidence_posture: str | None,
        source: str | None,
    ) -> list[dict[str, Any]]:
        where = ["true"]
        params: list[object] = []
        if cursor is not None:
            if view == "latest":
                where.append("(stories.last_material_evidence_at_ms, stories.story_id) < (%s, %s)")
            else:
                where.append(
                    "(stories.priority_score, stories.last_material_evidence_at_ms, stories.story_id) < (%s, %s, %s)"
                )
            params.extend(cursor)
        if q:
            where.append(
                """
                (
                  stories.title ILIKE %s
                  OR stories.snippet ILIKE %s
                  OR EXISTS (
                    SELECT 1
                    FROM news_story_memberships AS q_memberships
                    JOIN news_article_revisions AS q_revisions
                      ON q_revisions.revision_id = q_memberships.revision_id
                    WHERE q_memberships.story_id = stories.story_id
                      AND (q_revisions.title ILIKE %s OR q_revisions.snippet ILIKE %s)
                  )
                )
                """
            )
            pattern = f"%{q}%"
            params.extend((pattern, pattern, pattern, pattern))
        if evidence_posture:
            where.append("stories.evidence_posture = %s")
            params.append(evidence_posture)
        if source:
            where.append(
                """
                EXISTS (
                  SELECT 1
                  FROM news_story_memberships AS source_memberships
                  JOIN news_article_revisions AS source_revisions
                    ON source_revisions.revision_id = source_memberships.revision_id
                  JOIN news_feed_observations AS source_observations
                    ON source_observations.observation_id = source_revisions.observation_id
                  JOIN news_sources AS source_rows
                    ON source_rows.source_id = source_observations.source_id
                  WHERE source_memberships.story_id = stories.story_id
                    AND (
                      source_rows.source_id = %s
                      OR source_rows.name ILIKE %s
                      OR source_rows.source_domain ILIKE %s
                    )
                )
                """
            )
            params.extend((source, f"%{source}%", f"%{source}%"))
        params.append(limit)
        order_by = (
            "stories.last_material_evidence_at_ms DESC, stories.story_id DESC"
            if view == "latest"
            else ("stories.priority_score DESC, stories.last_material_evidence_at_ms DESC, stories.story_id DESC")
        )
        rows = self.conn.execute(
            f"""
            SELECT
              stories.*,
              revisions.article_id AS representative_article_id,
              revisions.title AS representative_title,
              revisions.source_published_at_ms AS representative_published_at_ms,
              observations.source_id AS representative_source_id,
              sources.name AS representative_source_name,
              sources.source_domain AS representative_source_domain,
              (
                SELECT count(DISTINCT source_observations.source_id)
                FROM news_story_memberships AS count_memberships
                JOIN news_article_revisions AS count_revisions
                  ON count_revisions.revision_id = count_memberships.revision_id
                JOIN news_feed_observations AS source_observations
                  ON source_observations.observation_id = count_revisions.observation_id
                WHERE count_memberships.story_id = stories.story_id
              ) AS source_count,
              current_analysis.publication_id AS analysis_publication_id,
              current_analysis.publication_payload AS analysis_payload,
              current_analysis.published_at_ms AS analysis_published_at_ms
            FROM news_stories AS stories
            JOIN news_article_revisions AS revisions
              ON revisions.revision_id = stories.representative_revision_id
            JOIN news_feed_observations AS observations
              ON observations.observation_id = revisions.observation_id
            JOIN news_sources AS sources ON sources.source_id = observations.source_id
            LEFT JOIN LATERAL (
              SELECT
                publications.publication_id,
                publications.payload AS publication_payload,
                publications.published_at_ms
              FROM news_story_analysis_current AS current_rows
              JOIN news_story_analysis_publications AS publications
                ON publications.publication_id = current_rows.publication_id
              WHERE current_rows.story_id = stories.story_id
              LIMIT 1
            ) AS current_analysis ON true
            WHERE {" AND ".join(where)}
            ORDER BY {order_by}
            LIMIT %s
            """,
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_story(self, *, story_id: str) -> dict[str, Any] | None:
        story = self.conn.execute(
            "SELECT * FROM news_stories WHERE story_id = %s",
            (story_id,),
        ).fetchone()
        if story is None:
            return None
        result = dict(story)
        result["memberships"] = self._story_public_memberships(story_id=story_id)
        result["articles"] = self._story_public_articles(story_id=story_id)
        result["identity_decisions"] = [
            dict(row)
            for row in self.conn.execute(
                """
                SELECT decisions.*
                  FROM news_story_identity_decisions AS decisions
                 WHERE decisions.selected_story_id = %s
                    OR decisions.article_id IN (
                      SELECT article_id
                        FROM news_story_memberships
                       WHERE story_id = %s
                    )
                 ORDER BY decisions.decided_at_ms, decisions.decision_id
                """,
                (story_id, story_id),
            ).fetchall()
        ]
        result["material_events"] = [
            dict(row)
            for row in self.conn.execute(
                """
                SELECT *
                  FROM news_story_material_events
                 WHERE story_id = %s
                 ORDER BY occurred_at_ms,
                          revision_id NULLS FIRST,
                          material_event_id
                """,
                (story_id,),
            ).fetchall()
        ]
        result["selection_audit"] = [
            dict(row)
            for row in self.conn.execute(
                """
                SELECT
                  selections.selection_id,
                  selections.selection_fingerprint,
                  selections.policy_version,
                  selections.evidence_cutoff_at_ms,
                  selections.critical,
                  selections.verified_critical,
                  activations.activation_id,
                  activations.activated_at_ms,
                  decision.value AS decision
                FROM news_brief_selections AS selections
                CROSS JOIN LATERAL jsonb_array_elements(selections.decisions) AS decision(value)
                LEFT JOIN news_brief_activations AS activations
                  ON activations.selection_id = selections.selection_id
                WHERE decision.value ->> 'story_id' = %s
                ORDER BY activations.activation_sequence DESC NULLS LAST,
                         selections.created_at_ms DESC,
                         selections.selection_id DESC
                LIMIT 50
                """,
                (story_id,),
            ).fetchall()
        ]
        result["analysis_publications"] = [
            dict(row)
            for row in self.conn.execute(
                """
                SELECT *
                  FROM news_story_analysis_publications
                 WHERE story_id = %s
                 ORDER BY published_at_ms DESC, publication_id DESC
                """,
                (story_id,),
            ).fetchall()
        ]
        result["current_analysis_publication"] = self.conn.execute(
            """
            SELECT publications.*
              FROM news_story_analysis_current AS current_rows
              JOIN news_story_analysis_publications AS publications
                ON publications.publication_id = current_rows.publication_id
             WHERE current_rows.story_id = %s
            """,
            (story_id,),
        ).fetchone()
        result["analysis_request"] = self.conn.execute(
            """
            SELECT *
              FROM news_story_analysis_requests
             WHERE story_id = %s
               AND material_evidence_hash = %s
             ORDER BY requested_at_ms DESC, request_id DESC
             LIMIT 1
            """,
            (story_id, story["material_evidence_hash"]),
        ).fetchone()
        return result

    def _story_public_memberships(self, *, story_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT *
              FROM news_story_memberships
             WHERE story_id = %s
             ORDER BY membership_kind, admitted_at_ms, article_id
            """,
            (story_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def _story_public_articles(self, *, story_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT
              articles.*,
              revisions.revision_id,
              revisions.revision_number,
              revisions.title,
              revisions.snippet,
              revisions.source_published_at_ms,
              revisions.observed_at_ms,
              revisions.language,
              revisions.content_hash,
              revisions.material_change_kind,
              revisions.is_current,
              observations.observation_id,
              observations.source_id,
              observations.raw_url,
              observations.source_entry_key,
              sources.name AS source_name,
              sources.source_domain,
              sources.source_role,
              sources.trust_tier,
              sources.source_chain_id,
              sources.publisher_organization_id AS source_publisher_organization_id,
              memberships.membership_kind,
              memberships.content_form,
              memberships.origin_relation,
              memberships.development_relation,
              memberships.epistemic_use,
              memberships.reporting_origin_id,
              memberships.origin_confidence,
              content.content_snapshot_id,
              content.status AS content_snapshot_status,
              content.content_hash AS snapshot_content_hash,
              content.fetched_at_ms AS content_fetched_at_ms,
              content.failure_reason AS content_failure_reason,
              content.extractor_version AS content_extractor_version,
              content.byte_count AS content_byte_count
            FROM news_story_memberships AS memberships
            JOIN news_articles AS articles ON articles.article_id = memberships.article_id
            JOIN news_article_revisions AS revisions ON revisions.article_id = articles.article_id
            JOIN news_feed_observations AS observations
              ON observations.observation_id = revisions.observation_id
            JOIN news_sources AS sources ON sources.source_id = observations.source_id
            LEFT JOIN LATERAL (
              SELECT snapshots.*
                FROM news_article_content_snapshots AS snapshots
               WHERE snapshots.revision_id = revisions.revision_id
               ORDER BY
                 CASE snapshots.status
                   WHEN 'available' THEN 0
                   WHEN 'truncated' THEN 1
                   ELSE 2
                 END,
                 snapshots.updated_at_ms DESC,
                 snapshots.content_snapshot_id
               LIMIT 1
            ) AS content ON true
            WHERE memberships.story_id = %s
            ORDER BY articles.first_seen_at_ms, articles.article_id, revisions.revision_number
            """,
            (story_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_sources(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT
              sources.*,
              latest_receipt.fetch_receipt_id AS latest_fetch_receipt_id,
              latest_receipt.entries_seen AS latest_entries_seen,
              latest_receipt.entries_admitted AS latest_entries_admitted,
              latest_receipt.duplicate_seen_count AS latest_duplicate_seen_count,
              latest_receipt.rejection_counts AS latest_rejection_counts,
              latest_receipt.error_code AS latest_error_code
            FROM news_sources AS sources
            LEFT JOIN LATERAL (
              SELECT *
                FROM news_fetch_receipts AS receipts
               WHERE receipts.source_id = sources.source_id
               ORDER BY receipts.finished_at_ms DESC, receipts.fetch_receipt_id DESC
               LIMIT 1
            ) AS latest_receipt ON true
            ORDER BY sources.name, sources.source_id
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def health_snapshot(self, *, now_ms: int) -> dict[str, Any]:
        """Measure News business invariants independently from worker heartbeat."""

        source_reasons: list[dict[str, Any]] = []
        source_rows = self.conn.execute(
            """
            SELECT source_id, refresh_interval_seconds, next_fetch_at_ms,
                   consecutive_failures, last_success_at_ms
              FROM news_sources
             WHERE enabled
             ORDER BY source_id
            """
        ).fetchall()
        for row in source_rows:
            interval_ms = int(row["refresh_interval_seconds"]) * 1_000
            overdue_ms = max(0, now_ms - int(row["next_fetch_at_ms"]))
            overdue_cycles = overdue_ms / interval_ms if interval_ms else 0.0
            if overdue_cycles >= 5:
                source_reasons.append(
                    _health_reason(
                        code="source_fetch_overdue",
                        status="failed",
                        measured_ms=overdue_ms,
                        threshold_ms=interval_ms * 5,
                        source_id=str(row["source_id"]),
                    )
                )
            elif overdue_cycles >= 2:
                source_reasons.append(
                    _health_reason(
                        code="source_fetch_overdue",
                        status="degraded",
                        measured_ms=overdue_ms,
                        threshold_ms=interval_ms * 2,
                        source_id=str(row["source_id"]),
                    )
                )
            if int(row["consecutive_failures"]) >= 5:
                source_reasons.append(
                    _health_reason(
                        code="source_consecutive_failures",
                        status="failed",
                        measured=int(row["consecutive_failures"]),
                        threshold=5,
                        source_id=str(row["source_id"]),
                    )
                )
            elif int(row["consecutive_failures"]) >= 2:
                source_reasons.append(
                    _health_reason(
                        code="source_consecutive_failures",
                        status="degraded",
                        measured=int(row["consecutive_failures"]),
                        threshold=2,
                        source_id=str(row["source_id"]),
                    )
                )

        material_reasons: list[dict[str, Any]] = []
        orphan_count = int(
            self.conn.execute(
                """
                WITH observations_without_revision AS MATERIALIZED (
                  SELECT
                    observations.observation_id,
                    observations.source_id,
                    observations.normalized_url,
                    observations.title,
                    observations.summary,
                    observations.source_published_at_ms,
                    observations.language
                  FROM news_feed_observations AS observations
                  LEFT JOIN news_article_revisions AS direct_revisions
                    ON direct_revisions.observation_id = observations.observation_id
                 WHERE direct_revisions.revision_id IS NULL
                )
                SELECT count(*) AS count
                  FROM observations_without_revision AS observations
                  JOIN news_sources AS sources
                    ON sources.source_id = observations.source_id
                 WHERE NOT EXISTS (
                         SELECT 1
                           FROM news_articles AS articles
                           JOIN news_article_revisions AS revisions
                             ON revisions.article_id = articles.article_id
                            AND revisions.title = observations.title
                            AND revisions.snippet = observations.summary
                            AND revisions.source_published_at_ms =
                                observations.source_published_at_ms
                            AND revisions.language = observations.language
                          WHERE articles.publisher_organization_id = COALESCE(
                                  sources.publisher_organization_id,
                                  sources.source_chain_id
                                )
                            AND articles.canonical_url = observations.normalized_url
                       )
                """
            ).fetchone()["count"]
        )
        if orphan_count:
            material_reasons.append(
                _health_reason(
                    code="observation_revision_orphan",
                    status="degraded",
                    measured=orphan_count,
                    threshold=0,
                )
            )
        current_revision_violations = int(
            self.conn.execute(
                """
                SELECT count(*) AS count
                  FROM (
                    SELECT articles.article_id
                      FROM news_articles AS articles
                      LEFT JOIN news_article_revisions AS revisions
                        ON revisions.article_id = articles.article_id
                       AND revisions.is_current
                     GROUP BY articles.article_id
                    HAVING count(revisions.revision_id) <> 1
                  ) AS violations
                """
            ).fetchone()["count"]
        )
        if current_revision_violations:
            material_reasons.append(
                _health_reason(
                    code="article_current_revision_invariant",
                    status="failed",
                    measured=current_revision_violations,
                    threshold=0,
                )
            )
        unprojected = self.conn.execute(
            """
            SELECT count(*) AS count, min(revisions.observed_at_ms) AS oldest_at_ms
              FROM news_article_revisions AS revisions
              LEFT JOIN news_article_identity_features AS features
                ON features.revision_id = revisions.revision_id
               AND features.identity_version = %s
             WHERE features.revision_id IS NULL
            """,
            (ARTICLE_IDENTITY_VERSION,),
        ).fetchone()
        projection_backlog = int(unprojected["count"])
        projection_lag_ms = (
            max(0, now_ms - int(unprojected["oldest_at_ms"])) if unprojected["oldest_at_ms"] is not None else 0
        )
        if projection_lag_ms > 120_000:
            material_reasons.append(
                _health_reason(
                    code="story_projection_lag",
                    status="failed",
                    measured_ms=projection_lag_ms,
                    threshold_ms=120_000,
                    backlog=projection_backlog,
                )
            )
        elif projection_lag_ms > 30_000:
            material_reasons.append(
                _health_reason(
                    code="story_projection_lag",
                    status="degraded",
                    measured_ms=projection_lag_ms,
                    threshold_ms=30_000,
                    backlog=projection_backlog,
                )
            )
        membership_violations = int(
            self.conn.execute(
                """
                SELECT count(*) AS count
                  FROM (
                    SELECT feature_articles.article_id
                      FROM (
                        SELECT DISTINCT article_id
                          FROM news_article_identity_features
                         WHERE identity_version = %s
                      ) AS feature_articles
                      LEFT JOIN news_story_memberships AS memberships
                        ON memberships.article_id = feature_articles.article_id
                       AND memberships.membership_kind = 'primary'
                     GROUP BY feature_articles.article_id
                    HAVING count(memberships.story_id) <> 1
                  ) AS violations
                """,
                (ARTICLE_IDENTITY_VERSION,),
            ).fetchone()["count"]
        )
        if membership_violations:
            material_reasons.append(
                _health_reason(
                    code="primary_membership_invariant",
                    status="failed",
                    measured=membership_violations,
                    threshold=0,
                )
            )
        story_counter_violations = int(
            self.conn.execute(
                """
                SELECT count(*) AS count
                  FROM (
                    SELECT stories.story_id
                      FROM news_stories AS stories
                      LEFT JOIN news_story_memberships AS memberships
                        ON memberships.story_id = stories.story_id
                     GROUP BY stories.story_id, stories.article_count,
                              stories.primary_member_count,
                              stories.contextual_member_count
                    HAVING stories.article_count <> count(DISTINCT memberships.article_id)
                       OR stories.primary_member_count
                          <> count(*) FILTER (
                               WHERE memberships.membership_kind = 'primary'
                             )
                       OR stories.contextual_member_count
                          <> count(*) FILTER (
                               WHERE memberships.membership_kind = 'contextual'
                             )
                  ) AS violations
                """
            ).fetchone()["count"]
        )
        if story_counter_violations:
            material_reasons.append(
                _health_reason(
                    code="story_counter_invariant",
                    status="failed",
                    measured=story_counter_violations,
                    threshold=0,
                )
            )
        story_material_hash_violations = int(
            self.conn.execute(
                """
                SELECT count(*) AS count
                  FROM news_stories AS stories
                  LEFT JOIN LATERAL (
                    SELECT events.event_factors
                      FROM news_story_material_events AS events
                     WHERE events.story_id = stories.story_id
                     ORDER BY events.occurred_at_ms DESC,
                              events.revision_id DESC NULLS LAST,
                              events.material_event_id DESC
                     LIMIT 1
                  ) AS latest_event ON true
                 WHERE latest_event.event_factors IS NULL
                    OR COALESCE(
                         latest_event.event_factors->>'material_evidence_hash',
                         ''
                       ) <> stories.material_evidence_hash
                """
            ).fetchone()["count"]
        )
        if story_material_hash_violations:
            material_reasons.append(
                _health_reason(
                    code="story_material_hash_closure",
                    status="failed",
                    measured=story_material_hash_violations,
                    threshold=0,
                )
            )
        story_projection_closure_violations = int(
            self.conn.execute(
                """
                SELECT count(*) AS count
                  FROM news_stories AS stories
                  LEFT JOIN news_story_profiles AS profiles
                    ON profiles.story_id = stories.story_id
                   AND profiles.identity_version = stories.identity_version
                  LEFT JOIN news_story_memberships AS representative
                    ON representative.story_id = stories.story_id
                   AND representative.revision_id = stories.representative_revision_id
                   AND representative.membership_kind = 'primary'
                 WHERE profiles.story_id IS NULL
                    OR representative.story_id IS NULL
                """
            ).fetchone()["count"]
        )
        if story_projection_closure_violations:
            material_reasons.append(
                _health_reason(
                    code="story_projection_closure",
                    status="failed",
                    measured=story_projection_closure_violations,
                    threshold=0,
                )
            )

        brief_reasons: list[dict[str, Any]] = []
        matured = self.conn.execute(
            """
            SELECT proposal_id, lane, first_proposed_at_ms, activation_due_at_ms
              FROM news_brief_proposals
             WHERE status = 'pending' AND activation_due_at_ms <= %s
             ORDER BY activation_due_at_ms, proposal_id
             LIMIT 1
            """,
            (now_ms,),
        ).fetchone()
        if matured is not None:
            mismatch_ms = max(0, now_ms - int(matured["activation_due_at_ms"]))
            proposal_age_ms = max(0, now_ms - int(matured["first_proposed_at_ms"]))
            brief_reasons.append(
                _health_reason(
                    code="planner_active_mismatch",
                    status="failed" if mismatch_ms > 60_000 else "degraded",
                    measured_ms=mismatch_ms,
                    threshold_ms=60_000 if mismatch_ms > 60_000 else 0,
                    proposal_id=str(matured["proposal_id"]),
                    lane=str(matured["lane"]),
                    proposal_age_ms=proposal_age_ms,
                )
            )
            proposal_slo_ms = {
                "ordinary": (180_000, 300_000),
                "verified_critical": (60_000, 120_000),
                "rectification": (45_000, 90_000),
            }
            degraded_after_ms, failed_after_ms = proposal_slo_ms[str(matured["lane"])]
            if proposal_age_ms > failed_after_ms:
                brief_reasons.append(
                    _health_reason(
                        code="proposal_activation_lag",
                        status="failed",
                        measured_ms=proposal_age_ms,
                        threshold_ms=failed_after_ms,
                        proposal_id=str(matured["proposal_id"]),
                        lane=str(matured["lane"]),
                    )
                )
            elif proposal_age_ms > degraded_after_ms:
                brief_reasons.append(
                    _health_reason(
                        code="proposal_activation_lag",
                        status="degraded",
                        measured_ms=proposal_age_ms,
                        threshold_ms=degraded_after_ms,
                        proposal_id=str(matured["proposal_id"]),
                        lane=str(matured["lane"]),
                    )
                )
        publication_mismatch = self.conn.execute(
            """
            SELECT activations.activation_id
              FROM news_brief_active AS active_rows
              JOIN news_brief_activations AS activations
                ON activations.activation_id = active_rows.activation_id
              JOIN news_brief_selections AS selections
                ON selections.selection_id = activations.selection_id
              JOIN news_brief_activation_analysis AS attached
                ON attached.activation_id = activations.activation_id
              JOIN news_brief_publications AS publications
                ON publications.publication_id = attached.publication_id
             WHERE active_rows.singleton_key
               AND attached.superseded_at_ms IS NULL
               AND (
                 publications.selection_id <> activations.selection_id
                 OR publications.synthesis_input_hash <> selections.synthesis_input_hash
                 OR NOT EXISTS (
                   SELECT 1
                     FROM news_ai_current_targets AS current_targets
                    WHERE current_targets.publication_kind = 'brief'
                      AND current_targets.target_id = activations.activation_id
                      AND current_targets.evidence_hash =
                          publications.synthesis_input_hash
                      AND current_targets.model = publications.model
                      AND current_targets.prompt_version =
                          publications.prompt_version
                      AND current_targets.workflow_version =
                          publications.workflow_version
                      AND current_targets.schema_version =
                          publications.schema_version
                      AND current_targets.locale = publications.locale
                 )
               )
             LIMIT 1
            """
        ).fetchone()
        if publication_mismatch is not None:
            brief_reasons.append(
                _health_reason(
                    code="active_publication_mismatch",
                    status="failed",
                    measured=1,
                    threshold=0,
                    activation_id=str(publication_mismatch["activation_id"]),
                )
            )

        public_reasons: list[dict[str, Any]] = []
        active_closure = self.conn.execute(
            """
            SELECT
              (SELECT count(*) FROM news_brief_active) AS active_rows,
              (
                SELECT count(*)
                  FROM news_brief_active AS active_rows
                  JOIN news_brief_activations AS activations
                    ON activations.activation_id = active_rows.activation_id
                  JOIN news_brief_selections AS selections
                    ON selections.selection_id = activations.selection_id
              ) AS closed_rows
            """
        ).fetchone()
        if int(active_closure["active_rows"]) != int(active_closure["closed_rows"]):
            public_reasons.append(
                _health_reason(
                    code="public_active_pointer_unclosed",
                    status="failed",
                    measured=int(active_closure["active_rows"]) - int(active_closure["closed_rows"]),
                    threshold=0,
                )
            )
        active_contract = self.conn.execute(
            """
            SELECT activations.activation_id, activations.selection_id,
                   selections.selection_fingerprint,
                   selections.synthesis_input_hash,
                   selections.selected_story_ids,
                   selections.evidence_bundle
              FROM news_brief_active AS active_rows
              JOIN news_brief_activations AS activations
                ON activations.activation_id = active_rows.activation_id
              JOIN news_brief_selections AS selections
                ON selections.selection_id = activations.selection_id
             WHERE active_rows.singleton_key
            """
        ).fetchone()
        if active_contract is not None:
            try:
                active_bundle = BriefEvidenceBundle.model_validate(active_contract["evidence_bundle"])
                active_mismatch = (
                    active_bundle.selection_id != str(active_contract["selection_id"])
                    or active_bundle.selection_fingerprint != str(active_contract["selection_fingerprint"])
                    or active_bundle.synthesis_input_hash != str(active_contract["synthesis_input_hash"])
                    or sha256_json(active_bundle.synthesis_input()) != str(active_contract["synthesis_input_hash"])
                    or [str(story.get("story_id") or "") for story in active_bundle.stories]
                    != list(active_contract["selected_story_ids"])
                )
            except ValueError:
                active_mismatch = True
            if active_mismatch:
                public_reasons.append(
                    _health_reason(
                        code="public_active_contract_mismatch",
                        status="failed",
                        measured=1,
                        threshold=0,
                        activation_id=str(active_contract["activation_id"]),
                    )
                )

        ai_reasons: list[dict[str, Any]] = []
        active_ai = self.conn.execute(
            """
            SELECT attempts.*, current_targets.desired_at_ms
              FROM news_brief_active AS active_rows
              JOIN news_ai_current_targets AS current_targets
                ON current_targets.publication_kind = 'brief'
               AND current_targets.target_id = active_rows.activation_id
              JOIN news_ai_attempts AS attempts
                ON attempts.publication_kind = current_targets.publication_kind
               AND attempts.target_id = current_targets.target_id
               AND attempts.evidence_hash = current_targets.evidence_hash
               AND attempts.model = current_targets.model
               AND attempts.prompt_version = current_targets.prompt_version
               AND attempts.workflow_version = current_targets.workflow_version
               AND attempts.schema_version = current_targets.schema_version
               AND attempts.locale = current_targets.locale
             LIMIT 1
            """
        ).fetchone()
        if active_ai is not None:
            queue_age_ms = max(0, now_ms - int(active_ai["desired_at_ms"]))
            if (
                str(active_ai["status"]) == "failed"
                and int(active_ai["next_attempt_at_ms"]) >= 9_000_000_000_000_000_000
            ):
                ai_reasons.append(
                    _health_reason(
                        code="ai_terminal_failure",
                        status="failed",
                        measured=int(active_ai["attempt_count"]),
                        threshold=int(active_ai["attempt_count"]),
                    )
                )
            elif str(active_ai["status"]) == "running" and int(active_ai["lease_expires_at_ms"]) < now_ms:
                lease_overdue_ms = now_ms - int(active_ai["lease_expires_at_ms"])
                ai_reasons.append(
                    _health_reason(
                        code="ai_lease_expired",
                        status="failed" if lease_overdue_ms > 300_000 else "degraded",
                        measured_ms=lease_overdue_ms,
                        threshold_ms=300_000 if lease_overdue_ms > 300_000 else 0,
                    )
                )
            elif str(active_ai["status"]) != "available" and queue_age_ms > 300_000:
                ai_reasons.append(
                    _health_reason(
                        code="ai_queue_age",
                        status="degraded",
                        measured_ms=queue_age_ms,
                        threshold_ms=300_000,
                    )
                )
        else:
            unattached_active = self.conn.execute(
                """
                SELECT activations.activation_id, activations.activated_at_ms
                  FROM news_brief_active AS active_rows
                  JOIN news_brief_activations AS activations
                    ON activations.activation_id = active_rows.activation_id
                  JOIN news_brief_selections AS selections
                    ON selections.selection_id = activations.selection_id
                 WHERE active_rows.singleton_key
                   AND jsonb_array_length(selections.selected_story_ids) > 0
                   AND NOT EXISTS (
                     SELECT 1
                      FROM news_brief_activation_analysis AS attached
                      WHERE attached.activation_id = activations.activation_id
                        AND attached.superseded_at_ms IS NULL
                   )
                """
            ).fetchone()
            if unattached_active is not None:
                queue_age_ms = max(
                    0,
                    now_ms - int(unattached_active["activated_at_ms"]),
                )
                if queue_age_ms > 300_000:
                    ai_reasons.append(
                        _health_reason(
                            code="ai_queue_age",
                            status="degraded",
                            measured_ms=queue_age_ms,
                            threshold_ms=300_000,
                            activation_id=str(unattached_active["activation_id"]),
                        )
                    )

        layers = {
            "source": _health_layer(source_reasons, enabled_source_count=len(source_rows)),
            "material": _health_layer(
                material_reasons,
                observation_orphan_count=orphan_count,
                current_revision_violation_count=current_revision_violations,
                projection_backlog=projection_backlog,
                projection_lag_ms=projection_lag_ms,
                primary_membership_violation_count=membership_violations,
                story_counter_violation_count=story_counter_violations,
                story_material_hash_violation_count=story_material_hash_violations,
                story_projection_closure_violation_count=story_projection_closure_violations,
                story_api_visibility_lag_ms=0 if not story_projection_closure_violations else None,
            ),
            "brief": _health_layer(brief_reasons),
            "public": _health_layer(
                public_reasons,
                active_api_visibility_lag_ms=0 if not public_reasons else None,
            ),
            "ai": _health_layer(ai_reasons),
        }
        statuses = {str(layer["status"]) for layer in layers.values()}
        status = "failed" if "failed" in statuses else "degraded" if "degraded" in statuses else "running"
        return {
            "status": status,
            "reasons": [reason for layer in layers.values() for reason in _sequence(layer["reasons"])],
            "layers": layers,
            "measured_at_ms": now_ms,
        }

    # Story analysis evidence and on-demand request ----------------------------------

    def request_story_analysis(
        self,
        *,
        story_id: str,
        now_ms: int,
    ) -> dict[str, Any] | None:
        story = self.conn.execute(
            "SELECT * FROM news_stories WHERE story_id = %s",
            (story_id,),
        ).fetchone()
        if story is None:
            return None
        request_id = deterministic_id(
            "story-analysis-request",
            story_id,
            story["material_evidence_hash"],
            "on_demand",
        )
        evidence = self.story_analysis_evidence(story_id=story_id)
        status = "pending" if _story_analysis_evidence_sufficient(evidence) else "insufficient"
        self.conn.execute(
            """
            INSERT INTO news_story_analysis_requests (
              request_id,
              story_id,
              material_evidence_hash,
              request_kind,
              reason,
              status,
              requested_at_ms,
              updated_at_ms
            )
            VALUES (%s, %s, %s, 'on_demand', %s, %s, %s, %s)
            ON CONFLICT (story_id, material_evidence_hash, request_kind) DO UPDATE SET
              status = CASE
                WHEN news_story_analysis_requests.status = 'published'
                THEN news_story_analysis_requests.status
                ELSE EXCLUDED.status
              END,
              updated_at_ms = EXCLUDED.updated_at_ms
            """,
            (
                request_id,
                story_id,
                story["material_evidence_hash"],
                Jsonb({"requested_by": "user"}),
                status,
                now_ms,
                now_ms,
            ),
        )
        persisted = self.conn.execute(
            """
            SELECT request_id, story_id, material_evidence_hash, status
              FROM news_story_analysis_requests
             WHERE story_id = %s
               AND material_evidence_hash = %s
               AND request_kind = 'on_demand'
            """,
            (story_id, story["material_evidence_hash"]),
        ).fetchone()
        if persisted is None:
            raise RuntimeError("news_story_analysis_request_missing_after_upsert")
        return {
            "request_id": str(persisted["request_id"]),
            "story_id": str(persisted["story_id"]),
            "material_evidence_hash": str(persisted["material_evidence_hash"]),
            "status": str(persisted["status"]),
        }

    def story_analysis_evidence(self, *, story_id: str) -> StoryAnalysisEvidence:
        story = self.conn.execute(
            "SELECT * FROM news_stories WHERE story_id = %s",
            (story_id,),
        ).fetchone()
        if story is None:
            raise ValueError("news_story_not_found")
        articles = [_evidence_article(row) for row in self._story_evidence_rows(story_id=story_id)]
        return StoryAnalysisEvidence(
            story_id=story_id,
            material_evidence_hash=str(story["material_evidence_hash"]),
            title=str(story["title"]),
            snippet=str(story["snippet"]),
            event_core=dict(_mapping(story["event_core"])),
            evidence_posture=cast(EvidencePosture, str(story["evidence_posture"])),
            evidence_factors=dict(_mapping(story["evidence_factors"])),
            impact_profile=dict(_mapping(story["impact_profile"])),
            material_change=str(story["material_evolution_state"]),
            articles=tuple(articles),
        )

    # Brief and AI methods are implemented below to keep one PostgreSQL owner. -------

    def plan_global_brief(
        self,
        *,
        now_ms: int,
        candidate_limit: int,
        debounce_ms: int,
        critical_debounce_ms: int,
    ) -> dict[str, Any]:
        lock = self.conn.execute(
            "SELECT pg_try_advisory_xact_lock(%s) AS locked",
            (_BRIEF_PLANNER_LOCK_KEY,),
        ).fetchone()
        if lock is None or not bool(lock["locked"]):
            return {
                "status": "planner_busy",
                "selected_story_count": 0,
                "changed": False,
            }
        stories = self.eligible_story_rows(limit=candidate_limit)
        for story in stories:
            last_brief_at_ms = int(story.get("last_brief_published_at_ms") or 0)
            coverage_age_ms = max(0, now_ms - last_brief_at_ms) if last_brief_at_ms else 0
            story["recent_brief_coverage_penalty"] = (
                20
                if last_brief_at_ms and coverage_age_ms <= 6 * 60 * 60 * 1000
                else 10
                if last_brief_at_ms and coverage_age_ms <= 24 * 60 * 60 * 1000
                else 0
            )
            story["evidence_articles"] = [
                _evidence_article(row) for row in self._story_evidence_rows(story_id=str(story["story_id"]))
            ]
        grouping, selection, bundle = plan_brief_selection(stories, cutoff_at_ms=now_ms)
        self.conn.execute(
            """
            INSERT INTO news_narrative_grouping_snapshots (
              grouping_snapshot_id, input_hash, policy_version, embedding_model,
              fallback_used, groups, receipt, cutoff_at_ms, created_at_ms
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (input_hash, policy_version, embedding_model) DO NOTHING
            """,
            (
                grouping["grouping_snapshot_id"],
                grouping["input_hash"],
                grouping["policy_version"],
                grouping["embedding_model"],
                grouping["fallback_used"],
                Jsonb(grouping["groups"]),
                Jsonb(grouping["receipt"]),
                grouping["cutoff_at_ms"],
                now_ms,
            ),
        )
        self.conn.execute(
            """
            INSERT INTO news_brief_selections (
              selection_id, selection_fingerprint, grouping_snapshot_id,
              policy_version, evidence_cutoff_at_ms, selected_story_ids,
              decisions, critical, verified_critical, synthesis_input_hash,
              evidence_bundle, created_at_ms
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (selection_fingerprint) DO NOTHING
            """,
            (
                selection["selection_id"],
                selection["selection_fingerprint"],
                selection["grouping_snapshot_id"],
                selection["policy_version"],
                selection["evidence_cutoff_at_ms"],
                Jsonb(selection["selected_story_ids"]),
                Jsonb(selection["decisions"]),
                selection["critical"],
                selection["verified_critical"],
                selection["synthesis_input_hash"],
                Jsonb(bundle.model_dump(mode="json")),
                now_ms,
            ),
        )
        persisted_selection = self.conn.execute(
            """
            SELECT *
              FROM news_brief_selections
             WHERE selection_fingerprint = %s
            """,
            (selection["selection_fingerprint"],),
        ).fetchone()
        if persisted_selection is None:
            raise RuntimeError("news_brief_selection_missing_after_insert")
        selection_id = str(persisted_selection["selection_id"])
        active = self.conn.execute(
            """
            SELECT activations.*, selections.selection_fingerprint,
                   selections.evidence_bundle
              FROM news_brief_active AS active_rows
              JOIN news_brief_activations AS activations
                ON activations.activation_id = active_rows.activation_id
              JOIN news_brief_selections AS selections
                ON selections.selection_id = activations.selection_id
             WHERE active_rows.singleton_key
             FOR UPDATE OF active_rows
            """
        ).fetchone()
        pending = self.conn.execute(
            """
            SELECT *
              FROM news_brief_proposals
             WHERE status = 'pending'
             FOR UPDATE
            """
        ).fetchone()
        if active is not None and str(active["selection_id"]) == selection_id:
            if pending is not None:
                self.conn.execute(
                    """
                    UPDATE news_brief_proposals
                       SET status = 'cancelled',
                           resolved_at_ms = %s,
                           updated_at_ms = %s,
                           reason = reason || %s
                     WHERE proposal_id = %s
                    """,
                    (
                        now_ms,
                        now_ms,
                        Jsonb({"resolution": "planner_returned_to_active"}),
                        pending["proposal_id"],
                    ),
                )
            return {
                "selection_id": selection_id,
                "selection_fingerprint": selection["selection_fingerprint"],
                "activation_id": str(active["activation_id"]),
                "status": "active",
                "selected_story_count": len(bundle.stories),
                "changed": False,
            }
        if pending is not None and str(pending["selection_id"]) != selection_id:
            self.conn.execute(
                """
                UPDATE news_brief_proposals
                   SET status = 'superseded',
                       resolved_at_ms = %s,
                       updated_at_ms = %s,
                       reason = reason || %s
                 WHERE proposal_id = %s
                """,
                (
                    now_ms,
                    now_ms,
                    Jsonb({"resolution": "different_candidate_observed"}),
                    pending["proposal_id"],
                ),
            )
            pending = None
        if pending is None:
            lane = _brief_proposal_lane(
                active_bundle=(dict(_mapping(active["evidence_bundle"])) if active is not None else None),
                candidate_bundle=bundle.model_dump(mode="json"),
                verified_critical=bool(selection["verified_critical"]),
                rectification=(
                    self._active_brief_requires_rectification(dict(_mapping(active["evidence_bundle"])))
                    if active is not None
                    else False
                ),
            )
            delay = (
                0 if lane == "rectification" else critical_debounce_ms if lane == "verified_critical" else debounce_ms
            )
            proposal_id = deterministic_id(
                "news-brief-proposal",
                selection_id,
                now_ms,
            )
            pending = self.conn.execute(
                """
                INSERT INTO news_brief_proposals (
                  proposal_id, selection_id, lane, status,
                  first_proposed_at_ms, last_observed_at_ms,
                  activation_due_at_ms, reason, created_at_ms, updated_at_ms
                )
                VALUES (%s, %s, %s, 'pending', %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    proposal_id,
                    selection_id,
                    lane,
                    now_ms,
                    now_ms,
                    now_ms + delay,
                    Jsonb({"planner_selection_fingerprint": selection["selection_fingerprint"]}),
                    now_ms,
                    now_ms,
                ),
            ).fetchone()
        else:
            pending = self.conn.execute(
                """
                UPDATE news_brief_proposals
                   SET last_observed_at_ms = %s,
                       updated_at_ms = %s
                 WHERE proposal_id = %s
                   AND status = 'pending'
                RETURNING *
                """,
                (now_ms, now_ms, pending["proposal_id"]),
            ).fetchone()
        if pending is None:
            raise RuntimeError("news_brief_pending_proposal_missing")
        activation_id = None
        if int(pending["activation_due_at_ms"]) <= now_ms:
            activation_id = self._activate_brief_proposal(
                proposal_id=str(pending["proposal_id"]),
                now_ms=now_ms,
            )
        return {
            "selection_id": selection_id,
            "selection_fingerprint": selection["selection_fingerprint"],
            "proposal_id": str(pending["proposal_id"]),
            "activation_id": activation_id,
            "status": "active" if activation_id else "pending",
            "lane": str(pending["lane"]),
            "selected_story_count": len(bundle.stories),
            "changed": True,
        }

    def _activate_brief_proposal(self, *, proposal_id: str, now_ms: int) -> str:
        proposal = self.conn.execute(
            """
            SELECT *
              FROM news_brief_proposals
             WHERE proposal_id = %s
               AND status = 'pending'
               AND activation_due_at_ms <= %s
             FOR UPDATE
            """,
            (proposal_id, now_ms),
        ).fetchone()
        if proposal is None:
            raise RuntimeError("news_brief_proposal_not_activatable")
        sequence = int(
            self.conn.execute(
                "SELECT coalesce(max(activation_sequence), 0) + 1 AS value FROM news_brief_activations"
            ).fetchone()["value"]
        )
        activation_id = deterministic_id(
            "news-brief-activation",
            sequence,
            proposal["selection_id"],
        )
        self.conn.execute(
            """
            INSERT INTO news_brief_activations (
              activation_id, activation_sequence, selection_id,
              proposal_id, lane, activated_at_ms
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                activation_id,
                sequence,
                proposal["selection_id"],
                proposal_id,
                proposal["lane"],
                now_ms,
            ),
        )
        self.conn.execute(
            """
            UPDATE news_brief_proposals
               SET status = 'activated',
                   resolved_at_ms = %s,
                   updated_at_ms = %s
             WHERE proposal_id = %s
            """,
            (now_ms, now_ms, proposal_id),
        )
        self.conn.execute(
            """
            INSERT INTO news_brief_active (singleton_key, activation_id, updated_at_ms)
            VALUES (true, %s, %s)
            ON CONFLICT (singleton_key) DO UPDATE SET
              activation_id = EXCLUDED.activation_id,
              updated_at_ms = EXCLUDED.updated_at_ms
            """,
            (activation_id, now_ms),
        )
        return activation_id

    def _active_brief_requires_rectification(
        self,
        active_bundle: Mapping[str, object],
    ) -> bool:
        active_hashes = {
            str(story.get("story_id") or ""): str(story.get("material_evidence_hash") or "")
            for story in _sequence(active_bundle.get("stories"))
            if isinstance(story, Mapping) and str(story.get("story_id") or "")
        }
        if not active_hashes:
            return False
        rows = self.conn.execute(
            """
            SELECT story_id, material_evidence_hash, evidence_posture,
                   material_evolution_state
              FROM news_stories
             WHERE story_id = ANY(%s)
            """,
            (sorted(active_hashes),),
        ).fetchall()
        return any(
            str(row["material_evidence_hash"]) != active_hashes[str(row["story_id"])]
            and (
                str(row["evidence_posture"]) in {"contested", "corrected", "withdrawn"}
                or str(row["material_evolution_state"]) in {"material_correction", "conflict_detected", "retraction"}
            )
            for row in rows
        )

    def claim_ai_work(
        self,
        *,
        brief_contract: NewsPublicationContract,
        story_contract: NewsPublicationContract,
        now_ms: int,
        limit: int,
        lease_ms: int,
        max_attempts: int,
    ) -> list[tuple[str, str, str, BriefEvidenceBundle | StoryAnalysisEvidence]]:
        candidates: list[tuple[int, str, str, str, BriefEvidenceBundle | StoryAnalysisEvidence]] = []
        brief_rows = self.conn.execute(
            """
            SELECT activations.activation_id, activations.activated_at_ms,
                   selections.*
              FROM news_brief_active AS active_rows
              JOIN news_brief_activations AS activations
                ON activations.activation_id = active_rows.activation_id
              JOIN news_brief_selections AS selections
                ON selections.selection_id = activations.selection_id
             WHERE active_rows.singleton_key
               AND jsonb_array_length(selections.selected_story_ids) > 0
               AND NOT EXISTS (
                 SELECT 1
                   FROM news_brief_activation_analysis AS attached
                   JOIN news_brief_publications AS publications
                     ON publications.publication_id = attached.publication_id
                  WHERE attached.activation_id = activations.activation_id
                    AND attached.superseded_at_ms IS NULL
                    AND publications.model = %s
                    AND publications.prompt_version = %s
                    AND publications.workflow_version = %s
                    AND publications.schema_version = %s
                    AND publications.locale = %s
               )
             LIMIT %s
            """,
            (
                brief_contract.model,
                brief_contract.prompt_version,
                brief_contract.workflow_version,
                brief_contract.schema_version,
                brief_contract.locale,
                limit,
            ),
        ).fetchall()
        for row in brief_rows:
            brief_evidence = BriefEvidenceBundle.model_validate(row["evidence_bundle"])
            candidates.append(
                (
                    0 if bool(row["verified_critical"]) else 1,
                    "brief",
                    str(row["activation_id"]),
                    str(row["synthesis_input_hash"]),
                    brief_evidence,
                )
            )
        remaining = max(0, limit - len(candidates))
        if remaining:
            request_rows = self.conn.execute(
                """
                SELECT requests.*, stories.impact_score
                  FROM news_story_analysis_requests AS requests
                  JOIN news_stories AS stories ON stories.story_id = requests.story_id
                 WHERE (
                   requests.status IN ('pending', 'claimed', 'failed')
                   OR (
                     requests.status = 'published'
                     AND NOT EXISTS (
                       SELECT 1
                         FROM news_story_analysis_current AS current_rows
                         JOIN news_story_analysis_publications AS publications
                           ON publications.publication_id = current_rows.publication_id
                        WHERE current_rows.story_id = requests.story_id
                          AND publications.story_id = requests.story_id
                          AND publications.material_evidence_hash =
                              requests.material_evidence_hash
                          AND publications.model = %s
                          AND publications.prompt_version = %s
                          AND publications.workflow_version = %s
                          AND publications.schema_version = %s
                          AND publications.locale = %s
                     )
                   )
                 )
                 ORDER BY
                   CASE WHEN requests.request_kind = 'automatic' THEN 0 ELSE 1 END,
                   stories.impact_score DESC,
                   requests.requested_at_ms,
                   requests.request_id
                 FOR UPDATE OF requests SKIP LOCKED
                 LIMIT %s
                """,
                (
                    story_contract.model,
                    story_contract.prompt_version,
                    story_contract.workflow_version,
                    story_contract.schema_version,
                    story_contract.locale,
                    remaining,
                ),
            ).fetchall()
            for row in request_rows:
                story_evidence = self.story_analysis_evidence(story_id=str(row["story_id"]))
                candidates.append(
                    (
                        2 if str(row["request_kind"]) == "automatic" else 3,
                        "story_analysis",
                        str(row["story_id"]),
                        str(row["material_evidence_hash"]),
                        story_evidence,
                    )
                )
        claimed: list[tuple[str, str, str, BriefEvidenceBundle | StoryAnalysisEvidence]] = []
        for _, kind, target_id, evidence_hash, evidence in sorted(candidates)[:limit]:
            contract = brief_contract if kind == "brief" else story_contract
            attempt_key = deterministic_id(
                "news-ai-attempt",
                kind,
                target_id,
                evidence_hash,
                contract.model,
                contract.prompt_version,
                contract.workflow_version,
                contract.schema_version,
                contract.locale,
            )
            self.conn.execute(
                """
                INSERT INTO news_ai_current_targets (
                  publication_kind, target_id, evidence_hash, model,
                  prompt_version, workflow_version, schema_version, locale,
                  desired_at_ms
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (publication_kind, target_id) DO UPDATE SET
                  evidence_hash = EXCLUDED.evidence_hash,
                  model = EXCLUDED.model,
                  prompt_version = EXCLUDED.prompt_version,
                  workflow_version = EXCLUDED.workflow_version,
                  schema_version = EXCLUDED.schema_version,
                  locale = EXCLUDED.locale,
                  desired_at_ms = CASE
                    WHEN ROW(
                      news_ai_current_targets.evidence_hash,
                      news_ai_current_targets.model,
                      news_ai_current_targets.prompt_version,
                      news_ai_current_targets.workflow_version,
                      news_ai_current_targets.schema_version,
                      news_ai_current_targets.locale
                    ) IS DISTINCT FROM ROW(
                      EXCLUDED.evidence_hash,
                      EXCLUDED.model,
                      EXCLUDED.prompt_version,
                      EXCLUDED.workflow_version,
                      EXCLUDED.schema_version,
                      EXCLUDED.locale
                    )
                    THEN EXCLUDED.desired_at_ms
                    ELSE news_ai_current_targets.desired_at_ms
                  END
                """,
                (
                    kind,
                    target_id,
                    evidence_hash,
                    contract.model,
                    contract.prompt_version,
                    contract.workflow_version,
                    contract.schema_version,
                    contract.locale,
                    now_ms,
                ),
            )
            if kind == "brief":
                self.conn.execute(
                    """
                    UPDATE news_brief_activation_analysis AS attached
                       SET superseded_at_ms = GREATEST(%s, attached.attached_at_ms)
                      FROM news_brief_publications AS publications
                     WHERE attached.activation_id = %s
                       AND attached.publication_id = publications.publication_id
                       AND attached.superseded_at_ms IS NULL
                       AND (
                         publications.model <> %s
                         OR publications.prompt_version <> %s
                         OR publications.workflow_version <> %s
                         OR publications.schema_version <> %s
                         OR publications.locale <> %s
                       )
                    """,
                    (
                        now_ms,
                        target_id,
                        contract.model,
                        contract.prompt_version,
                        contract.workflow_version,
                        contract.schema_version,
                        contract.locale,
                    ),
                )
            else:
                self.conn.execute(
                    """
                    DELETE FROM news_story_analysis_current AS current_rows
                     USING news_story_analysis_publications AS publications
                     WHERE current_rows.story_id = %s
                       AND current_rows.publication_id = publications.publication_id
                       AND (
                         publications.model <> %s
                         OR publications.prompt_version <> %s
                         OR publications.workflow_version <> %s
                         OR publications.schema_version <> %s
                         OR publications.locale <> %s
                       )
                    """,
                    (
                        target_id,
                        contract.model,
                        contract.prompt_version,
                        contract.workflow_version,
                        contract.schema_version,
                        contract.locale,
                    ),
                )
            publication_id = self._existing_publication_id(
                publication_kind=kind,
                target_id=target_id,
                evidence_hash=evidence_hash,
                contract=contract,
            )
            if publication_id is not None:
                self._mark_cached_target_published(
                    publication_kind=kind,
                    target_id=target_id,
                    evidence_hash=evidence_hash,
                    publication_id=publication_id,
                    now_ms=now_ms,
                )
                continue
            attempt = self.conn.execute(
                "SELECT * FROM news_ai_attempts WHERE attempt_key = %s FOR UPDATE",
                (attempt_key,),
            ).fetchone()
            if attempt is not None:
                if str(attempt["status"]) == "available":
                    continue
                if str(attempt["status"]) == "running" and int(attempt["lease_expires_at_ms"]) > now_ms:
                    continue
                if int(attempt["attempt_count"]) >= max_attempts:
                    self.conn.execute(
                        """
                        UPDATE news_ai_attempts
                           SET status = 'failed',
                               lease_expires_at_ms = 0,
                               next_attempt_at_ms = %s,
                               last_error = 'max_attempts_exhausted',
                               updated_at_ms = %s
                         WHERE attempt_key = %s
                        """,
                        (9_223_372_036_854_775_000, now_ms, attempt_key),
                    )
                    if kind == "story_analysis":
                        self.conn.execute(
                            """
                            UPDATE news_story_analysis_requests
                               SET status = 'failed', updated_at_ms = %s
                             WHERE story_id = %s
                               AND material_evidence_hash = %s
                            """,
                            (now_ms, target_id, evidence_hash),
                        )
                    continue
                if int(attempt["next_attempt_at_ms"]) > now_ms:
                    continue
                attempt_count = int(attempt["attempt_count"]) + 1
                lease_token = deterministic_id(
                    "news-ai-lease",
                    attempt_key,
                    attempt_count,
                    now_ms,
                )
                self.conn.execute(
                    """
                    UPDATE news_ai_attempts
                       SET status = 'running',
                           attempt_count = %s,
                           lease_token = %s,
                           lease_expires_at_ms = %s,
                           updated_at_ms = %s
                     WHERE attempt_key = %s
                    """,
                    (
                        attempt_count,
                        lease_token,
                        now_ms + lease_ms,
                        now_ms,
                        attempt_key,
                    ),
                )
            else:
                lease_token = deterministic_id(
                    "news-ai-lease",
                    attempt_key,
                    1,
                    now_ms,
                )
                self.conn.execute(
                    """
                    INSERT INTO news_ai_attempts (
                      attempt_key, publication_kind, target_id, evidence_hash,
                      model, prompt_version, workflow_version, schema_version, locale,
                      status, attempt_count, lease_token, lease_expires_at_ms, next_attempt_at_ms,
                      requested_at_ms, updated_at_ms
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                            'running', 1, %s, %s, 0, %s, %s)
                    """,
                    (
                        attempt_key,
                        kind,
                        target_id,
                        evidence_hash,
                        contract.model,
                        contract.prompt_version,
                        contract.workflow_version,
                        contract.schema_version,
                        contract.locale,
                        lease_token,
                        now_ms + lease_ms,
                        now_ms,
                        now_ms,
                    ),
                )
            if kind == "story_analysis":
                self.conn.execute(
                    """
                    UPDATE news_story_analysis_requests
                       SET status = 'claimed', updated_at_ms = %s
                     WHERE story_id = %s AND material_evidence_hash = %s
                    """,
                    (now_ms, target_id, evidence_hash),
                )
            claimed.append((kind, attempt_key, lease_token, evidence))
        return claimed

    def complete_ai_publication(
        self,
        *,
        publication_kind: str,
        attempt_key: str,
        lease_token: str,
        evidence: BriefEvidenceBundle | StoryAnalysisEvidence,
        contract: NewsPublicationContract,
        payload: Mapping[str, Any],
        evidence_references: Sequence[str],
        receipt: Mapping[str, Any],
        published_at_ms: int,
        repair_count: int,
    ) -> str:
        if publication_kind not in {"brief", "story_analysis"}:
            raise ValueError("news_publication_kind_invalid")
        evidence_hash = (
            evidence.synthesis_input_hash
            if isinstance(evidence, BriefEvidenceBundle)
            else evidence.material_evidence_hash
        )
        active_attempt = self.conn.execute(
            """
            SELECT publication_kind, target_id, evidence_hash, model,
                   prompt_version, workflow_version, schema_version, locale
              FROM news_ai_attempts
             WHERE attempt_key = %s
               AND lease_token = %s
               AND status = 'running'
             FOR UPDATE
            """,
            (attempt_key, lease_token),
        ).fetchone()
        if active_attempt is None:
            raise RuntimeError("news_ai_attempt_lease_lost")
        target_id = str(active_attempt["target_id"])
        if isinstance(evidence, StoryAnalysisEvidence) and target_id != evidence.story_id:
            raise RuntimeError("news_ai_attempt_story_target_mismatch")
        expected_attempt = (
            publication_kind,
            target_id,
            evidence_hash,
            contract.model,
            contract.prompt_version,
            contract.workflow_version,
            contract.schema_version,
            contract.locale,
        )
        actual_attempt = tuple(
            str(active_attempt[field])
            for field in (
                "publication_kind",
                "target_id",
                "evidence_hash",
                "model",
                "prompt_version",
                "workflow_version",
                "schema_version",
                "locale",
            )
        )
        if actual_attempt != expected_attempt:
            raise RuntimeError("news_ai_attempt_contract_mismatch")
        current_target = self.conn.execute(
            """
            SELECT publication_kind, target_id, evidence_hash, model,
                   prompt_version, workflow_version, schema_version, locale
              FROM news_ai_current_targets
             WHERE publication_kind = %s
               AND target_id = %s
             FOR UPDATE
            """,
            (publication_kind, target_id),
        ).fetchone()
        target_is_current = (
            current_target is not None
            and tuple(
                str(current_target[field])
                for field in (
                    "publication_kind",
                    "target_id",
                    "evidence_hash",
                    "model",
                    "prompt_version",
                    "workflow_version",
                    "schema_version",
                    "locale",
                )
            )
            == expected_attempt
        )
        publication_id = publication_key_for(
            publication_kind=publication_kind,
            target_id=target_id,
            evidence_hash=evidence_hash,
            contract=contract,
        )
        if publication_kind == "brief":
            if not isinstance(evidence, BriefEvidenceBundle):
                raise ValueError("news_brief_evidence_required")
            self.conn.execute(
                """
                INSERT INTO news_brief_publications (
                  publication_id, selection_id, synthesis_input_hash,
                  evidence_cutoff_at_ms, model, prompt_version,
                  workflow_version, schema_version, locale, payload,
                  evidence_references, receipt, published_at_ms
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (publication_id) DO NOTHING
                """,
                (
                    publication_id,
                    evidence.selection_id,
                    evidence.synthesis_input_hash,
                    evidence.evidence_cutoff_at_ms,
                    contract.model,
                    contract.prompt_version,
                    contract.workflow_version,
                    contract.schema_version,
                    contract.locale,
                    Jsonb(dict(payload)),
                    Jsonb(list(evidence_references)),
                    Jsonb(dict(receipt)),
                    published_at_ms,
                ),
            )
            self.conn.execute(
                """
                UPDATE news_brief_activation_analysis AS attached
                   SET superseded_at_ms = GREATEST(%s, attached.attached_at_ms)
                 WHERE attached.activation_id = %s
                   AND attached.publication_id <> %s
                   AND attached.superseded_at_ms IS NULL
                   AND %s
                   AND EXISTS (
                     SELECT 1
                       FROM news_brief_active AS active_rows
                       JOIN news_brief_activations AS activations
                         ON activations.activation_id = active_rows.activation_id
                      WHERE active_rows.singleton_key
                        AND activations.activation_id = %s
                        AND activations.selection_id = %s
                   )
                """,
                (
                    published_at_ms,
                    target_id,
                    publication_id,
                    target_is_current,
                    target_id,
                    evidence.selection_id,
                ),
            )
            self.conn.execute(
                """
                INSERT INTO news_brief_activation_analysis (
                  activation_id, publication_id, attachment_kind, attached_at_ms,
                  superseded_at_ms
                )
                SELECT %s, %s, 'generated', %s, NULL
                 WHERE %s
                   AND EXISTS (
                   SELECT 1
                     FROM news_brief_active AS active_rows
                     JOIN news_brief_activations AS activations
                       ON activations.activation_id = active_rows.activation_id
                    WHERE active_rows.singleton_key
                      AND activations.activation_id = %s
                      AND activations.selection_id = %s
                 )
                ON CONFLICT (activation_id, publication_id) DO UPDATE SET
                  attachment_kind = EXCLUDED.attachment_kind,
                  attached_at_ms = EXCLUDED.attached_at_ms,
                  superseded_at_ms = NULL
                """,
                (
                    target_id,
                    publication_id,
                    published_at_ms,
                    target_is_current,
                    target_id,
                    evidence.selection_id,
                ),
            )
        else:
            if not isinstance(evidence, StoryAnalysisEvidence):
                raise ValueError("news_story_analysis_evidence_required")
            self.conn.execute(
                """
                INSERT INTO news_story_analysis_publications (
                  publication_id, story_id, material_evidence_hash, model,
                  prompt_version, workflow_version, schema_version, locale,
                  payload, evidence_references, receipt, published_at_ms
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (publication_id) DO NOTHING
                """,
                (
                    publication_id,
                    evidence.story_id,
                    evidence.material_evidence_hash,
                    contract.model,
                    contract.prompt_version,
                    contract.workflow_version,
                    contract.schema_version,
                    contract.locale,
                    Jsonb(dict(payload)),
                    Jsonb(list(evidence_references)),
                    Jsonb(dict(receipt)),
                    published_at_ms,
                ),
            )
            current_story = self.conn.execute(
                "SELECT material_evidence_hash FROM news_stories WHERE story_id = %s",
                (evidence.story_id,),
            ).fetchone()
            if (
                target_is_current
                and current_story is not None
                and str(current_story["material_evidence_hash"]) == evidence.material_evidence_hash
            ):
                self.conn.execute(
                    """
                    INSERT INTO news_story_analysis_current (story_id, publication_id, updated_at_ms)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (story_id) DO UPDATE SET
                      publication_id = EXCLUDED.publication_id,
                      updated_at_ms = EXCLUDED.updated_at_ms
                    """,
                    (evidence.story_id, publication_id, published_at_ms),
                )
            if target_is_current:
                self.conn.execute(
                    """
                    UPDATE news_story_analysis_requests
                       SET status = 'published', updated_at_ms = %s
                     WHERE story_id = %s AND material_evidence_hash = %s
                    """,
                    (published_at_ms, evidence.story_id, evidence.material_evidence_hash),
                )
        self.conn.execute(
            """
            UPDATE news_ai_attempts
               SET status = 'available',
                   repair_count = %s,
                   lease_expires_at_ms = 0,
                   validation_errors = '[]'::jsonb,
                   last_error = NULL,
                   updated_at_ms = %s
             WHERE attempt_key = %s
               AND lease_token = %s
               AND status = 'running'
            """,
            (repair_count, published_at_ms, attempt_key, lease_token),
        )
        return publication_id

    def fail_ai_attempt(
        self,
        *,
        attempt_key: str,
        lease_token: str,
        now_ms: int,
        error: object,
        validation_errors: Sequence[str] = (),
        retry_ms: int,
        terminal: bool,
        repair_count: int = 0,
    ) -> None:
        row = self.conn.execute(
            """
            SELECT publication_kind, target_id, evidence_hash, model,
                   prompt_version, workflow_version, schema_version, locale
              FROM news_ai_attempts
             WHERE attempt_key = %s
               AND lease_token = %s
               AND status = 'running'
             FOR UPDATE
            """,
            (attempt_key, lease_token),
        ).fetchone()
        if row is None:
            return
        self.conn.execute(
            """
            UPDATE news_ai_attempts
               SET status = 'failed',
                   repair_count = %s,
                   lease_expires_at_ms = 0,
                   next_attempt_at_ms = %s,
                   validation_errors = %s,
                   last_error = %s,
                   updated_at_ms = %s
             WHERE attempt_key = %s
               AND lease_token = %s
               AND status = 'running'
            """,
            (
                repair_count,
                9_223_372_036_854_775_000 if terminal else now_ms + retry_ms,
                Jsonb(list(validation_errors)),
                _bounded_error(error),
                now_ms,
                attempt_key,
                lease_token,
            ),
        )
        if str(row["publication_kind"]) == "story_analysis":
            self.conn.execute(
                """
                UPDATE news_story_analysis_requests
                   SET status = 'failed', updated_at_ms = %s
                 WHERE story_id = %s AND material_evidence_hash = %s
                   AND EXISTS (
                     SELECT 1
                       FROM news_ai_current_targets AS current_targets
                      WHERE current_targets.publication_kind = 'story_analysis'
                        AND current_targets.target_id = %s
                        AND current_targets.evidence_hash = %s
                        AND current_targets.model = %s
                        AND current_targets.prompt_version = %s
                        AND current_targets.workflow_version = %s
                        AND current_targets.schema_version = %s
                        AND current_targets.locale = %s
                   )
                """,
                (
                    now_ms,
                    row["target_id"],
                    row["evidence_hash"],
                    row["target_id"],
                    row["evidence_hash"],
                    row["model"],
                    row["prompt_version"],
                    row["workflow_version"],
                    row["schema_version"],
                    row["locale"],
                ),
            )

    def get_current_brief(self) -> dict[str, Any]:
        active_selection = self.conn.execute(
            """
            SELECT activations.activation_id, activations.activation_sequence,
                   activations.lane, activations.activated_at_ms,
                   selections.*, groupings.groups AS narrative_groups
              FROM news_brief_active AS active_rows
              JOIN news_brief_activations AS activations
                ON activations.activation_id = active_rows.activation_id
              JOIN news_brief_selections AS selections
                ON selections.selection_id = activations.selection_id
              JOIN news_narrative_grouping_snapshots AS groupings
                ON groupings.grouping_snapshot_id = selections.grouping_snapshot_id
             WHERE active_rows.singleton_key
            """
        ).fetchone()
        analysis = None
        active_attempt = None
        if active_selection is not None:
            analysis = self.conn.execute(
                """
                SELECT publications.*, attached.attachment_kind,
                       attached.attached_at_ms,
                       activations.activation_id,
                       activations.activation_sequence,
                       activations.activated_at_ms,
                       selections.selection_fingerprint,
                       selections.selected_story_ids, selections.decisions,
                       selections.evidence_bundle,
                       groupings.groups AS narrative_groups
                  FROM news_brief_activation_analysis AS attached
                  JOIN news_brief_activations AS activations
                    ON activations.activation_id = attached.activation_id
                  JOIN news_brief_publications AS publications
                    ON publications.publication_id = attached.publication_id
                  JOIN news_brief_selections AS selections
                    ON selections.selection_id = publications.selection_id
                  JOIN news_narrative_grouping_snapshots AS groupings
                    ON groupings.grouping_snapshot_id = selections.grouping_snapshot_id
                 WHERE attached.activation_id = %s
                   AND attached.superseded_at_ms IS NULL
                 ORDER BY attached.attached_at_ms DESC, publications.publication_id DESC
                 LIMIT 1
                """,
                (active_selection["activation_id"],),
            ).fetchone()
            active_attempt = self.conn.execute(
                """
                SELECT attempts.status, attempts.attempt_count,
                       attempts.validation_errors, attempts.last_error,
                       attempts.requested_at_ms, attempts.updated_at_ms
                  FROM news_ai_current_targets AS current_targets
                  JOIN news_ai_attempts AS attempts
                    ON attempts.publication_kind =
                       current_targets.publication_kind
                   AND attempts.target_id = current_targets.target_id
                   AND attempts.evidence_hash = current_targets.evidence_hash
                   AND attempts.model = current_targets.model
                   AND attempts.prompt_version =
                       current_targets.prompt_version
                   AND attempts.workflow_version =
                       current_targets.workflow_version
                   AND attempts.schema_version =
                       current_targets.schema_version
                   AND attempts.locale = current_targets.locale
                 WHERE current_targets.publication_kind = 'brief'
                   AND current_targets.target_id = %s
                 LIMIT 1
                """,
                (active_selection["activation_id"],),
            ).fetchone()
        previous_publication = self.conn.execute(
            """
            SELECT publications.*, attached.attachment_kind,
                   attached.attached_at_ms, activations.activation_id,
                   activations.activation_sequence, activations.activated_at_ms,
                   selections.selection_fingerprint,
                   selections.selected_story_ids, selections.decisions,
                   selections.evidence_bundle,
                   groupings.groups AS narrative_groups
              FROM news_brief_activation_analysis AS attached
              JOIN news_brief_activations AS activations
                ON activations.activation_id = attached.activation_id
              JOIN news_brief_publications AS publications
                ON publications.publication_id = attached.publication_id
              JOIN news_brief_selections AS selections
                ON selections.selection_id = activations.selection_id
              JOIN news_narrative_grouping_snapshots AS groupings
                ON groupings.grouping_snapshot_id = selections.grouping_snapshot_id
             WHERE (%s::text IS NULL OR activations.activation_id <> %s)
             ORDER BY activations.activation_sequence DESC,
                      attached.attached_at_ms DESC,
                      publications.publication_id DESC
             LIMIT 1
            """,
            (
                active_selection["activation_id"] if active_selection is not None else None,
                active_selection["activation_id"] if active_selection is not None else None,
            ),
        ).fetchone()
        pending_proposal = self.conn.execute(
            """
            SELECT proposals.*, selections.selection_fingerprint,
                   selections.selected_story_ids
              FROM news_brief_proposals AS proposals
              JOIN news_brief_selections AS selections
                ON selections.selection_id = proposals.selection_id
             WHERE proposals.status = 'pending'
            """
        ).fetchone()
        latest_failure = self.conn.execute(
            """
            SELECT target_id AS activation_id, attempt_count, last_error,
                   validation_errors, requested_at_ms, updated_at_ms
              FROM news_ai_attempts
             WHERE publication_kind = 'brief' AND status = 'failed'
             ORDER BY updated_at_ms DESC, attempt_key DESC
             LIMIT 1
            """
        ).fetchone()
        return {
            "active_selection": dict(active_selection) if active_selection is not None else None,
            "analysis": dict(analysis) if analysis is not None else None,
            "active_attempt": dict(active_attempt) if active_attempt is not None else None,
            "previous_publication": (dict(previous_publication) if previous_publication is not None else None),
            "pending_proposal": dict(pending_proposal) if pending_proposal is not None else None,
            "latest_failure": dict(latest_failure) if latest_failure is not None else None,
        }

    def list_brief_publications(self, *, limit: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT publications.*, selections.selection_fingerprint,
                   selections.selected_story_ids, selections.decisions,
                   selections.evidence_bundle, groupings.groups AS narrative_groups
              FROM news_brief_publications AS publications
              JOIN news_brief_selections AS selections
                ON selections.selection_id = publications.selection_id
              JOIN news_narrative_grouping_snapshots AS groupings
                ON groupings.grouping_snapshot_id = selections.grouping_snapshot_id
             ORDER BY publications.published_at_ms DESC, publications.publication_id DESC
             LIMIT %s
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def _existing_publication_id(
        self,
        *,
        publication_kind: str,
        target_id: str,
        evidence_hash: str,
        contract: NewsPublicationContract,
    ) -> str | None:
        table = "news_brief_publications" if publication_kind == "brief" else "news_story_analysis_publications"
        target_predicate = "" if publication_kind == "brief" else "story_id = %s AND"
        evidence_column = "synthesis_input_hash" if publication_kind == "brief" else "material_evidence_hash"
        params: tuple[object, ...] = (evidence_hash,) if publication_kind == "brief" else (target_id, evidence_hash)
        row = self.conn.execute(
            f"""
            SELECT publication_id FROM {table}
             WHERE {target_predicate} {evidence_column} = %s
               AND model = %s AND prompt_version = %s
               AND workflow_version = %s AND schema_version = %s AND locale = %s
            """,
            (
                *params,
                contract.model,
                contract.prompt_version,
                contract.workflow_version,
                contract.schema_version,
                contract.locale,
            ),
        ).fetchone()
        return str(row["publication_id"]) if row is not None else None

    def _mark_cached_target_published(
        self,
        *,
        publication_kind: str,
        target_id: str,
        evidence_hash: str,
        publication_id: str,
        now_ms: int,
    ) -> None:
        if publication_kind == "brief":
            self.conn.execute(
                """
                UPDATE news_brief_activation_analysis AS attached
                   SET superseded_at_ms = GREATEST(%s, attached.attached_at_ms)
                 WHERE attached.activation_id = %s
                   AND attached.publication_id <> %s
                   AND attached.superseded_at_ms IS NULL
                   AND EXISTS (
                     SELECT 1
                       FROM news_brief_active AS active_rows
                       JOIN news_brief_activations AS activations
                         ON activations.activation_id = active_rows.activation_id
                       JOIN news_brief_selections AS selections
                         ON selections.selection_id = activations.selection_id
                       JOIN news_brief_publications AS publications
                         ON publications.publication_id = %s
                       JOIN news_ai_current_targets AS current_targets
                         ON current_targets.publication_kind = 'brief'
                        AND current_targets.target_id = activations.activation_id
                        AND current_targets.evidence_hash =
                            publications.synthesis_input_hash
                        AND current_targets.model = publications.model
                        AND current_targets.prompt_version =
                            publications.prompt_version
                        AND current_targets.workflow_version =
                            publications.workflow_version
                        AND current_targets.schema_version =
                            publications.schema_version
                        AND current_targets.locale = publications.locale
                      WHERE active_rows.singleton_key
                        AND activations.activation_id = %s
                        AND selections.synthesis_input_hash = %s
                        AND publications.synthesis_input_hash = %s
                   )
                """,
                (
                    now_ms,
                    target_id,
                    publication_id,
                    publication_id,
                    target_id,
                    evidence_hash,
                    evidence_hash,
                ),
            )
            self.conn.execute(
                """
                INSERT INTO news_brief_activation_analysis (
                  activation_id, publication_id, attachment_kind, attached_at_ms,
                  superseded_at_ms
                )
                SELECT activations.activation_id, %s, 'reused', %s, NULL
                  FROM news_brief_active AS active_rows
                  JOIN news_brief_activations AS activations
                    ON activations.activation_id = active_rows.activation_id
                  JOIN news_brief_selections AS selections
                    ON selections.selection_id = activations.selection_id
                  JOIN news_brief_publications AS publications
                    ON publications.publication_id = %s
                  JOIN news_ai_current_targets AS current_targets
                    ON current_targets.publication_kind = 'brief'
                   AND current_targets.target_id = activations.activation_id
                   AND current_targets.evidence_hash =
                       publications.synthesis_input_hash
                   AND current_targets.model = publications.model
                   AND current_targets.prompt_version = publications.prompt_version
                   AND current_targets.workflow_version = publications.workflow_version
                   AND current_targets.schema_version = publications.schema_version
                   AND current_targets.locale = publications.locale
                 WHERE active_rows.singleton_key
                   AND activations.activation_id = %s
                   AND selections.synthesis_input_hash = %s
                   AND publications.synthesis_input_hash = %s
                ON CONFLICT (activation_id, publication_id) DO UPDATE SET
                  attachment_kind = EXCLUDED.attachment_kind,
                  attached_at_ms = EXCLUDED.attached_at_ms,
                  superseded_at_ms = NULL
                """,
                (
                    publication_id,
                    now_ms,
                    publication_id,
                    target_id,
                    evidence_hash,
                    evidence_hash,
                ),
            )
        else:
            self.conn.execute(
                """
                UPDATE news_story_analysis_requests
                   SET status = 'published', updated_at_ms = %s
                 WHERE story_id = %s AND material_evidence_hash = %s
                """,
                (now_ms, target_id, evidence_hash),
            )
            current_story = self.conn.execute(
                """
                SELECT stories.material_evidence_hash,
                       EXISTS (
                         SELECT 1
                           FROM news_story_analysis_publications AS publications
                           JOIN news_ai_current_targets AS current_targets
                             ON current_targets.publication_kind = 'story_analysis'
                            AND current_targets.target_id = publications.story_id
                            AND current_targets.evidence_hash =
                                publications.material_evidence_hash
                            AND current_targets.model = publications.model
                            AND current_targets.prompt_version =
                                publications.prompt_version
                            AND current_targets.workflow_version =
                                publications.workflow_version
                            AND current_targets.schema_version =
                                publications.schema_version
                            AND current_targets.locale = publications.locale
                          WHERE publications.publication_id = %s
                            AND publications.story_id = stories.story_id
                       ) AS target_is_current
                  FROM news_stories AS stories
                 WHERE stories.story_id = %s
                """,
                (publication_id, target_id),
            ).fetchone()
            if (
                current_story is not None
                and bool(current_story["target_is_current"])
                and str(current_story["material_evidence_hash"]) == evidence_hash
            ):
                self.conn.execute(
                    """
                    INSERT INTO news_story_analysis_current (story_id, publication_id, updated_at_ms)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (story_id) DO UPDATE SET
                      publication_id = EXCLUDED.publication_id,
                      updated_at_ms = EXCLUDED.updated_at_ms
                    """,
                    (target_id, publication_id, now_ms),
                )

    def eligible_story_rows(self, *, limit: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT stories.*, coverage.last_brief_published_at_ms
              FROM news_stories AS stories
              LEFT JOIN LATERAL (
                SELECT max(publications.published_at_ms) AS last_brief_published_at_ms
                  FROM news_brief_publications AS publications
                  JOIN news_brief_selections AS selections
                    ON selections.selection_id = publications.selection_id
                 WHERE selections.selected_story_ids ? stories.story_id
              ) AS coverage ON true
             WHERE stories.brief_eligible
               AND stories.identity_status = 'stable'
             ORDER BY stories.priority_score DESC, stories.impact_score DESC,
                      stories.last_material_evidence_at_ms DESC, stories.story_id
             LIMIT %s
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def _material_change_kind(
    *,
    current_revision: Mapping[str, object],
    observation: Mapping[str, object],
) -> str:
    title_changed = str(current_revision["title"]) != str(observation["title"])
    summary_changed = str(current_revision["snippet"]) != str(observation["summary"])
    time_changed = _int(current_revision["source_published_at_ms"]) != _int(observation["source_published_at_ms"])
    combined = f"{observation['title']} {observation['summary']}".lower()
    if any(value in combined for value in ("correction", "corrected", "更正", "撤回")):
        return "correction"
    if title_changed:
        return "title"
    if summary_changed:
        return "summary"
    if time_changed:
        return "source_time"
    return "content"


def _evidence_article(row: Mapping[str, object]) -> dict[str, object]:
    evidence = {
        "evidence_ref": str(row["revision_id"]),
        "article_id": str(row["article_id"]),
        "revision_id": str(row["revision_id"]),
        "title": str(row["title"]),
        "snippet": str(row.get("snippet") or ""),
        "source_published_at_ms": _int(row["source_published_at_ms"]),
        "observed_at_ms": _int(row["observed_at_ms"]),
        "language": str(row["language"]),
        "canonical_url": str(row["canonical_url"]),
        "source_id": str(row["source_id"]),
        "source_name": str(row["source_name"]),
        "source_role": str(row["source_role"]),
        "trust_tier": str(row["trust_tier"]),
        "source_chain_id": str(row["source_chain_id"]),
        "publisher_organization_id": str(row["publisher_organization_id"]),
        "content_form": str(row["content_form"]),
        "origin_relation": str(row["origin_relation"]),
        "development_relation": str(row["development_relation"]),
        "epistemic_use": str(row["epistemic_use"]),
        "reporting_origin_id": row.get("reporting_origin_id"),
        "origin_confidence": _float(row["origin_confidence"]),
    }
    snapshot_status = str(row.get("content_snapshot_status") or "")
    if snapshot_status in {"available", "truncated"} and row.get("extracted_text"):
        evidence["content_snapshot"] = {
            "content_snapshot_id": str(row["content_snapshot_id"]),
            "status": snapshot_status,
            "content_hash": str(row.get("snapshot_content_hash") or ""),
            "fetched_at_ms": _int(row.get("content_fetched_at_ms")),
            "extracted_text": str(row["extracted_text"])[:8_000],
        }
    return evidence


def _story_analysis_evidence_sufficient(evidence: StoryAnalysisEvidence) -> bool:
    independent_origins = {
        str(article.get("reporting_origin_id") or "")
        for article in evidence.articles
        if str(article.get("reporting_origin_id") or "")
        and str(article.get("origin_relation")) in {"originating", "independent"}
        and _float(article.get("origin_confidence")) >= 0.7
    }
    if len(independent_origins) >= 2:
        return True
    return any(
        isinstance(article.get("content_snapshot"), Mapping)
        and len(str(_mapping(article.get("content_snapshot")).get("extracted_text") or "")) >= 1_500
        and (
            str(article.get("source_role")) == "official_authority"
            or str(article.get("trust_tier")) in {"authoritative", "trusted"}
        )
        for article in evidence.articles
    )


def _brief_proposal_lane(
    *,
    active_bundle: Mapping[str, object] | None,
    candidate_bundle: Mapping[str, object],
    verified_critical: bool,
    rectification: bool,
) -> str:
    if rectification:
        return "rectification"
    active_story_ids = {
        str(story.get("story_id") or "")
        for story in _sequence((active_bundle or {}).get("stories"))
        if isinstance(story, Mapping) and str(story.get("story_id") or "")
    }
    candidate_verified_critical_ids = {
        str(story.get("story_id") or "")
        for story in _sequence(candidate_bundle.get("stories"))
        if isinstance(story, Mapping)
        and int(story.get("impact_score") or 0) >= 90
        and str(story.get("evidence_posture")) in {"primary_source_confirmed", "independently_corroborated"}
    }
    if verified_critical and candidate_verified_critical_ids - active_story_ids:
        return "verified_critical"
    return "ordinary"


def _health_reason(
    *,
    code: str,
    status: str,
    measured_ms: int | None = None,
    threshold_ms: int | None = None,
    measured: int | None = None,
    threshold: int | None = None,
    **details: object,
) -> dict[str, Any]:
    return {
        "code": code,
        "status": status,
        "measured_ms": measured_ms,
        "threshold_ms": threshold_ms,
        "measured": measured,
        "threshold": threshold,
        "details": details,
    }


def _health_layer(reasons: Sequence[Mapping[str, object]], **measurements: object) -> dict[str, Any]:
    statuses = {str(reason.get("status") or "") for reason in reasons}
    status = "failed" if "failed" in statuses else "degraded" if "degraded" in statuses else "running"
    return {
        "status": status,
        "reasons": [dict(reason) for reason in reasons],
        "measurements": measurements,
    }


def publication_key_for(
    *,
    publication_kind: str,
    target_id: str,
    evidence_hash: str,
    contract: NewsPublicationContract,
) -> str:
    return deterministic_id(
        "news-publication",
        publication_kind,
        target_id if publication_kind != "brief" else "content-addressed",
        evidence_hash,
        contract.model,
        contract.prompt_version,
        contract.workflow_version,
        contract.schema_version,
        contract.locale,
    )


def _bounded_error(value: object, limit: int = 1000) -> str:
    normalized = " ".join(str(value or "").strip().split())
    return normalized[:limit] or "unknown_error"


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: object) -> Sequence[object]:
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return value
    return ()


def _int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def _float(value: object) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return 0.0


__all__ = ["NewsRepository", "publication_key_for"]
