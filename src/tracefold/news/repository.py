from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from typing import Any

from psycopg.types.json import Jsonb

from tracefold.news.identity import choose_story, normalize_feed_entry, project_story, story_id_for_anchor
from tracefold.news.models import (
    NEWS_ANALYSIS_PROMPT_VERSION,
    NEWS_ANALYSIS_SCHEMA_VERSION,
    NEWS_ANALYSIS_WORKFLOW_VERSION,
    STORY_IDENTITY_VERSION,
    STORY_IMPORTANCE_VERSION,
    STORY_LIFECYCLE_VERSION,
    NewsAnalysisContract,
    NewsAnalysisEvidence,
    NewsFeedEntry,
    NewsSourceDefinition,
    NewsStoryAnalysisDraft,
)

_STORY_CANDIDATE_WINDOW_MS = 72 * 60 * 60 * 1000


class NewsRepository:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def sync_sources(self, sources: Sequence[NewsSourceDefinition], *, now_ms: int) -> None:
        source_ids = [source.source_id for source in sources]
        for source in sources:
            self.conn.execute(
                """
                INSERT INTO news_sources (
                  source_id, name, feed_url, source_domain, source_role, trust_tier,
                  source_chain_id, coverage_tags, default_language, enabled,
                  refresh_interval_seconds, next_fetch_at_ms, created_at_ms, updated_at_ms
                )
                VALUES (
                  %(source_id)s, %(name)s, %(feed_url)s, %(source_domain)s, %(source_role)s, %(trust_tier)s,
                  %(source_chain_id)s, %(coverage_tags)s, %(default_language)s, %(enabled)s,
                  %(refresh_interval_seconds)s, %(next_fetch_at_ms)s, %(now_ms)s, %(now_ms)s
                )
                ON CONFLICT (source_id) DO UPDATE SET
                  name = EXCLUDED.name,
                  feed_url = EXCLUDED.feed_url,
                  source_domain = EXCLUDED.source_domain,
                  source_role = EXCLUDED.source_role,
                  trust_tier = EXCLUDED.trust_tier,
                  source_chain_id = EXCLUDED.source_chain_id,
                  coverage_tags = EXCLUDED.coverage_tags,
                  default_language = EXCLUDED.default_language,
                  enabled = EXCLUDED.enabled,
                  refresh_interval_seconds = EXCLUDED.refresh_interval_seconds,
                  next_fetch_at_ms = CASE
                    WHEN news_sources.feed_url IS DISTINCT FROM EXCLUDED.feed_url
                      OR news_sources.enabled IS DISTINCT FROM EXCLUDED.enabled
                    THEN %(next_fetch_at_ms)s
                    ELSE news_sources.next_fetch_at_ms
                  END,
                  updated_at_ms = EXCLUDED.updated_at_ms
                """,
                {
                    **source.model_dump(),
                    "coverage_tags": Jsonb(list(source.coverage_tags)),
                    "next_fetch_at_ms": now_ms if source.enabled else 0,
                    "now_ms": now_ms,
                },
            )
        if source_ids:
            self.conn.execute(
                """
                UPDATE news_sources
                   SET enabled = FALSE,
                       updated_at_ms = %(now_ms)s
                 WHERE NOT (source_id = ANY(%(source_ids)s::text[]))
                   AND enabled
                """,
                {"source_ids": source_ids, "now_ms": now_ms},
            )
        else:
            self.conn.execute(
                "UPDATE news_sources SET enabled = FALSE, updated_at_ms = %s WHERE enabled",
                (now_ms,),
            )

    def claim_due_sources(self, *, now_ms: int, limit: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT source_id
              FROM news_sources
             WHERE enabled
               AND next_fetch_at_ms <= %(now_ms)s
             ORDER BY next_fetch_at_ms ASC, source_id ASC
             LIMIT %(limit)s
             FOR UPDATE SKIP LOCKED
            """,
            {"now_ms": now_ms, "limit": limit},
        ).fetchall()
        source_ids = [str(row["source_id"]) for row in rows]
        if not source_ids:
            return []
        claimed = self.conn.execute(
            """
            UPDATE news_sources
               SET last_fetch_started_at_ms = %(now_ms)s,
                   next_fetch_at_ms = %(now_ms)s + (refresh_interval_seconds * 1000),
                   updated_at_ms = %(now_ms)s
             WHERE source_id = ANY(%(source_ids)s::text[])
             RETURNING source_id, name, feed_url, source_domain, source_role, trust_tier,
                       source_chain_id, coverage_tags, default_language, enabled,
                       refresh_interval_seconds, etag, last_modified
            """,
            {"now_ms": now_ms, "source_ids": source_ids},
        ).fetchall()
        by_id = {str(row["source_id"]): dict(row) for row in claimed}
        return [by_id[source_id] for source_id in source_ids]

    def record_fetch_failure(
        self,
        *,
        source_id: str,
        now_ms: int,
        error: str,
        status_code: int | None = None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE news_sources
               SET last_fetch_finished_at_ms = %(now_ms)s,
                   last_http_status = %(status_code)s,
                   consecutive_failures = consecutive_failures + 1,
                   last_error = %(error)s,
                   updated_at_ms = %(now_ms)s
             WHERE source_id = %(source_id)s
            """,
            {
                "source_id": source_id,
                "now_ms": now_ms,
                "status_code": status_code,
                "error": _bounded_error(error),
            },
        )

    def record_fetch_success(
        self,
        *,
        source: NewsSourceDefinition,
        entries: Sequence[NewsFeedEntry],
        now_ms: int,
        status_code: int,
        etag: str | None,
        last_modified: str | None,
        not_modified: bool,
    ) -> dict[str, int]:
        self.conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended('tracefold:news-story-writer:v1', 0))"
        )
        inserted = 0
        changed = 0
        stories_created = 0
        memberships_created = 0
        if not not_modified:
            for entry in entries:
                article = normalize_feed_entry(source=source, entry=entry, observed_at_ms=now_ms)
                result = self._upsert_article(article)
                inserted += int(result["inserted"])
                changed += int(result["changed"])
                membership = self._membership_for_article(article.article_id)
                if membership is None:
                    match = choose_story(article, self._story_candidates(article))
                    if match is None:
                        story_id = story_id_for_anchor(article.article_id)
                        self._create_story(story_id=story_id, article=article, now_ms=now_ms)
                        stories_created += 1
                        match_method = "anchor"
                        match_score = 1.0
                        match_reason: Mapping[str, object] = {
                            "identity_version": STORY_IDENTITY_VERSION,
                            "anchor_article_id": article.article_id,
                        }
                    else:
                        story_id = match.story_id
                        match_method = match.match_method
                        match_score = match.match_score
                        match_reason = match.reason
                    self._add_membership(
                        story_id=story_id,
                        article_id=article.article_id,
                        match_method=match_method,
                        match_score=match_score,
                        match_reason=match_reason,
                        now_ms=now_ms,
                    )
                    memberships_created += 1
                    self._project_story(story_id=story_id, now_ms=now_ms)
                elif result["changed"]:
                    self._project_story(story_id=str(membership["story_id"]), now_ms=now_ms)
        self.conn.execute(
            """
            UPDATE news_sources
               SET last_fetch_finished_at_ms = %(now_ms)s,
                   last_success_at_ms = %(now_ms)s,
                   last_http_status = %(status_code)s,
                   etag = %(etag)s,
                   last_modified = %(last_modified)s,
                   consecutive_failures = 0,
                   last_error = NULL,
                   updated_at_ms = %(now_ms)s
             WHERE source_id = %(source_id)s
            """,
            {
                "source_id": source.source_id,
                "now_ms": now_ms,
                "status_code": status_code,
                "etag": etag,
                "last_modified": last_modified,
            },
        )
        return {
            "articles_inserted": inserted,
            "articles_changed": changed,
            "stories_created": stories_created,
            "memberships_created": memberships_created,
        }

    def refresh_story_time_state(self, *, now_ms: int, limit: int = 1000) -> int:
        rows = self.conn.execute(
            """
            SELECT story_id
             FROM news_stories
             WHERE next_state_refresh_at_ms <= %(now_ms)s
             ORDER BY next_state_refresh_at_ms ASC, story_id ASC
             LIMIT %(limit)s
             FOR UPDATE SKIP LOCKED
            """,
            {"now_ms": now_ms, "limit": limit},
        ).fetchall()
        updated = 0
        for row in rows:
            updated += int(self._project_story(story_id=str(row["story_id"]), now_ms=now_ms))
        return updated

    def claim_analysis_evidence(
        self,
        *,
        model: str,
        now_ms: int,
        limit: int,
        lease_ms: int,
        max_attempts: int,
        prompt_version: str = NEWS_ANALYSIS_PROMPT_VERSION,
        workflow_version: str = NEWS_ANALYSIS_WORKFLOW_VERSION,
        schema_version: str = NEWS_ANALYSIS_SCHEMA_VERSION,
    ) -> list[tuple[str, NewsAnalysisEvidence]]:
        rows = self.conn.execute(
            """
            SELECT stories.story_id, stories.evidence_set_hash
              FROM news_stories AS stories
             WHERE NOT EXISTS (
                     SELECT 1
                       FROM news_story_analyses AS analyses
                      WHERE analyses.story_id = stories.story_id
                        AND analyses.evidence_set_hash = stories.evidence_set_hash
                        AND analyses.model = %(model)s
                        AND analyses.prompt_version = %(prompt_version)s
                        AND analyses.workflow_version = %(workflow_version)s
                        AND analyses.schema_version = %(schema_version)s
                   )
               AND NOT EXISTS (
                     SELECT 1
                       FROM news_story_analysis_attempts AS attempts
                     WHERE attempts.story_id = stories.story_id
                        AND attempts.evidence_set_hash = stories.evidence_set_hash
                        AND attempts.model = %(model)s
                        AND attempts.prompt_version = %(prompt_version)s
                        AND attempts.workflow_version = %(workflow_version)s
                        AND attempts.schema_version = %(schema_version)s
                        AND (
                          attempts.attempt_count >= %(max_attempts)s
                          OR (
                            attempts.status = 'running'
                            AND attempts.lease_expires_at_ms > %(now_ms)s
                          )
                          OR attempts.next_attempt_at_ms > %(now_ms)s
                        )
                   )
             ORDER BY stories.importance_score DESC, stories.last_seen_at_ms DESC, stories.story_id ASC
             LIMIT %(limit)s
             FOR UPDATE OF stories SKIP LOCKED
            """,
            {
                "model": model,
                "prompt_version": prompt_version,
                "workflow_version": workflow_version,
                "schema_version": schema_version,
                "max_attempts": max_attempts,
                "now_ms": now_ms,
                "limit": limit,
            },
        ).fetchall()
        claimed: list[tuple[str, NewsAnalysisEvidence]] = []
        for row in rows:
            story_id = str(row["story_id"])
            evidence_set_hash = str(row["evidence_set_hash"])
            analysis_key = analysis_key_for(
                story_id=story_id,
                evidence_set_hash=evidence_set_hash,
                model=model,
                prompt_version=prompt_version,
                workflow_version=workflow_version,
                schema_version=schema_version,
            )
            self.conn.execute(
                """
                INSERT INTO news_story_analysis_attempts (
                  analysis_key, story_id, evidence_set_hash, model, prompt_version,
                  workflow_version, schema_version, status, attempt_count,
                  lease_expires_at_ms, next_attempt_at_ms, last_error, updated_at_ms
                )
                VALUES (
                  %(analysis_key)s, %(story_id)s, %(evidence_set_hash)s, %(model)s, %(prompt_version)s,
                  %(workflow_version)s, %(schema_version)s, 'running', 1,
                  %(lease_expires_at_ms)s, 0, NULL, %(now_ms)s
                )
                ON CONFLICT (analysis_key) DO UPDATE SET
                  status = 'running',
                  attempt_count = news_story_analysis_attempts.attempt_count + 1,
                  lease_expires_at_ms = EXCLUDED.lease_expires_at_ms,
                  next_attempt_at_ms = 0,
                  last_error = NULL,
                  updated_at_ms = EXCLUDED.updated_at_ms
                """,
                {
                    "analysis_key": analysis_key,
                    "story_id": story_id,
                    "evidence_set_hash": evidence_set_hash,
                    "model": model,
                    "prompt_version": prompt_version,
                    "workflow_version": workflow_version,
                    "schema_version": schema_version,
                    "lease_expires_at_ms": now_ms + lease_ms,
                    "now_ms": now_ms,
                },
            )
            claimed.append((analysis_key, self.analysis_evidence(story_id=story_id)))
        return claimed

    def analysis_evidence(self, *, story_id: str) -> NewsAnalysisEvidence:
        story = self.conn.execute(
            """
            SELECT story_id, evidence_set_hash, title, snippet, verification_status, phase,
                   importance_score, source_count, article_count, trusted_source_count,
                   independent_origin_count
              FROM news_stories
             WHERE story_id = %s
            """,
            (story_id,),
        ).fetchone()
        if story is None:
            raise ValueError("news_story_not_found")
        articles = self._story_articles(story_id)
        return NewsAnalysisEvidence(
            **dict(story),
            articles=tuple(_analysis_article(row) for row in articles),
        )

    def complete_analysis(
        self,
        *,
        analysis_key: str,
        evidence: NewsAnalysisEvidence,
        model: str,
        draft: NewsStoryAnalysisDraft,
        published_at_ms: int,
        receipt: Mapping[str, object],
        prompt_version: str = NEWS_ANALYSIS_PROMPT_VERSION,
        workflow_version: str = NEWS_ANALYSIS_WORKFLOW_VERSION,
        schema_version: str = NEWS_ANALYSIS_SCHEMA_VERSION,
    ) -> str:
        allowed_refs = {str(article["article_id"]) for article in evidence.articles}
        supplied_refs = set(draft.evidence_references)
        if not supplied_refs.issubset(allowed_refs):
            unknown = sorted(supplied_refs - allowed_refs)
            raise ValueError("news_story_analysis_unknown_evidence:" + ",".join(unknown))
        analysis_id = "analysis_" + analysis_key[:32]
        self.conn.execute(
            """
            INSERT INTO news_story_analyses (
              analysis_id, story_id, evidence_set_hash, model, prompt_version,
              workflow_version, schema_version, what_happened, why_it_matters,
              political_impact, economic_market_impact, confirmed_facts,
              disagreements_unknowns, next_checkpoint, evidence_references,
              receipt, published_at_ms
            )
            VALUES (
              %(analysis_id)s, %(story_id)s, %(evidence_set_hash)s, %(model)s, %(prompt_version)s,
              %(workflow_version)s, %(schema_version)s, %(what_happened)s, %(why_it_matters)s,
              %(political_impact)s, %(economic_market_impact)s, %(confirmed_facts)s,
              %(disagreements_unknowns)s, %(next_checkpoint)s, %(evidence_references)s,
              %(receipt)s, %(published_at_ms)s
            )
            ON CONFLICT (analysis_id) DO NOTHING
            """,
            {
                "analysis_id": analysis_id,
                "story_id": evidence.story_id,
                "evidence_set_hash": evidence.evidence_set_hash,
                "model": model,
                "prompt_version": prompt_version,
                "workflow_version": workflow_version,
                "schema_version": schema_version,
                "what_happened": draft.what_happened,
                "why_it_matters": draft.why_it_matters,
                "political_impact": draft.political_impact,
                "economic_market_impact": draft.economic_market_impact,
                "confirmed_facts": Jsonb(list(draft.confirmed_facts)),
                "disagreements_unknowns": Jsonb(list(draft.disagreements_unknowns)),
                "next_checkpoint": draft.next_checkpoint,
                "evidence_references": Jsonb(list(draft.evidence_references)),
                "receipt": Jsonb(dict(receipt)),
                "published_at_ms": published_at_ms,
            },
        )
        self.conn.execute(
            """
            UPDATE news_story_analysis_attempts
               SET status = 'available',
                   lease_expires_at_ms = 0,
                   next_attempt_at_ms = 0,
                   last_error = NULL,
                   updated_at_ms = %(published_at_ms)s
             WHERE analysis_key = %(analysis_key)s
            """,
            {"analysis_key": analysis_key, "published_at_ms": published_at_ms},
        )
        return analysis_id

    def fail_analysis(
        self,
        *,
        analysis_key: str,
        now_ms: int,
        error: str,
        retry_ms: int,
    ) -> None:
        self.conn.execute(
            """
            UPDATE news_story_analysis_attempts
               SET status = 'failed',
                   lease_expires_at_ms = 0,
                   next_attempt_at_ms = %(next_attempt_at_ms)s,
                   last_error = %(error)s,
                   updated_at_ms = %(now_ms)s
             WHERE analysis_key = %(analysis_key)s
            """,
            {
                "analysis_key": analysis_key,
                "now_ms": now_ms,
                "next_attempt_at_ms": now_ms + retry_ms,
                "error": _bounded_error(error),
            },
        )

    def list_story_rows(
        self,
        *,
        limit: int,
        cursor: tuple[int, str] | None,
        q: str | None,
        verification_status: str | None,
        source: str | None,
        analysis_contract: NewsAnalysisContract | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: dict[str, object] = {
            "limit": limit,
            **_analysis_contract_params(analysis_contract),
        }
        if cursor is not None:
            clauses.append("(stories.last_seen_at_ms, stories.story_id) < (%(cursor_ms)s, %(cursor_id)s)")
            params.update({"cursor_ms": cursor[0], "cursor_id": cursor[1]})
        if verification_status:
            clauses.append("stories.verification_status = %(verification_status)s")
            params["verification_status"] = verification_status
        if q:
            clauses.append(
                """
                (
                  stories.title ILIKE %(query)s
                  OR stories.snippet ILIKE %(query)s
                  OR EXISTS (
                    SELECT 1
                      FROM news_story_articles AS search_members
                      JOIN news_articles AS search_articles
                        ON search_articles.article_id = search_members.article_id
                     WHERE search_members.story_id = stories.story_id
                       AND (
                         search_articles.title ILIKE %(query)s
                         OR search_articles.snippet ILIKE %(query)s
                       )
                  )
                )
                """
            )
            params["query"] = f"%{q}%"
        if source:
            clauses.append(
                """
                EXISTS (
                  SELECT 1
                    FROM news_story_articles AS source_members
                    JOIN news_articles AS source_articles
                      ON source_articles.article_id = source_members.article_id
                    JOIN news_sources AS source_rows
                      ON source_rows.source_id = source_articles.source_id
                   WHERE source_members.story_id = stories.story_id
                     AND (
                       source_rows.source_id = %(source)s
                       OR source_rows.name ILIKE %(source_query)s
                     )
                )
                """
            )
            params.update({"source": source, "source_query": f"%{source}%"})
        where = "WHERE " + " AND ".join(f"({clause})" for clause in clauses) if clauses else ""
        rows = self.conn.execute(
            f"""
            SELECT stories.*,
                   jsonb_build_object(
                     'article_id', primary_articles.article_id,
                     'source_id', primary_articles.source_id,
                     'source_name', primary_sources.name,
                     'source_role', primary_sources.source_role,
                     'trust_tier', primary_sources.trust_tier,
                     'source_chain_id', primary_sources.source_chain_id,
                     'canonical_url', primary_articles.canonical_url,
                     'title', primary_articles.title,
                     'snippet', primary_articles.snippet,
                     'published_at_ms', primary_articles.published_at_ms,
                     'origin_url', primary_articles.origin_url,
                     'origin_domain', primary_articles.origin_domain,
                     'origin_name', primary_articles.origin_name,
                     'provenance_status', primary_articles.provenance_status
                   ) AS primary_article,
                   current_analysis.analysis_id,
                   current_analysis.why_it_matters AS short_conclusion,
                   current_analysis.published_at_ms AS analysis_published_at_ms,
                   current_attempt.status AS attempt_status
              FROM news_stories AS stories
              JOIN news_articles AS primary_articles
                ON primary_articles.article_id = stories.primary_article_id
              JOIN news_sources AS primary_sources
                ON primary_sources.source_id = primary_articles.source_id
              LEFT JOIN LATERAL (
                SELECT analyses.analysis_id, analyses.why_it_matters, analyses.published_at_ms
                  FROM news_story_analyses AS analyses
                 WHERE analyses.story_id = stories.story_id
                   AND analyses.evidence_set_hash = stories.evidence_set_hash
                   AND %(analysis_enabled)s
                   AND analyses.model = %(analysis_model)s
                   AND analyses.prompt_version = %(analysis_prompt_version)s
                   AND analyses.workflow_version = %(analysis_workflow_version)s
                   AND analyses.schema_version = %(analysis_schema_version)s
                 ORDER BY analyses.published_at_ms DESC, analyses.analysis_id DESC
                 LIMIT 1
              ) AS current_analysis ON TRUE
              LEFT JOIN LATERAL (
                SELECT attempts.status
                  FROM news_story_analysis_attempts AS attempts
                 WHERE attempts.story_id = stories.story_id
                   AND attempts.evidence_set_hash = stories.evidence_set_hash
                   AND %(analysis_enabled)s
                   AND attempts.model = %(analysis_model)s
                   AND attempts.prompt_version = %(analysis_prompt_version)s
                   AND attempts.workflow_version = %(analysis_workflow_version)s
                   AND attempts.schema_version = %(analysis_schema_version)s
                 ORDER BY attempts.updated_at_ms DESC, attempts.analysis_key DESC
                 LIMIT 1
              ) AS current_attempt ON TRUE
              {where}
             ORDER BY stories.last_seen_at_ms DESC, stories.story_id DESC
             LIMIT %(limit)s
            """,
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def get_story(
        self,
        *,
        story_id: str,
        analysis_contract: NewsAnalysisContract | None = None,
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT stories.*,
                   current_analysis.analysis_id,
                   current_analysis.model AS analysis_model,
                   current_analysis.prompt_version AS analysis_prompt_version,
                   current_analysis.workflow_version AS analysis_workflow_version,
                   current_analysis.schema_version AS analysis_schema_version,
                   current_analysis.what_happened,
                   current_analysis.why_it_matters,
                   current_analysis.political_impact,
                   current_analysis.economic_market_impact,
                   current_analysis.confirmed_facts,
                   current_analysis.disagreements_unknowns,
                   current_analysis.next_checkpoint,
                   current_analysis.evidence_references,
                   current_analysis.published_at_ms AS analysis_published_at_ms,
                   current_attempt.status AS attempt_status,
                   current_attempt.last_error AS analysis_last_error
              FROM news_stories AS stories
              LEFT JOIN LATERAL (
                SELECT analyses.*
                  FROM news_story_analyses AS analyses
                 WHERE analyses.story_id = stories.story_id
                   AND analyses.evidence_set_hash = stories.evidence_set_hash
                   AND %(analysis_enabled)s
                   AND analyses.model = %(analysis_model)s
                   AND analyses.prompt_version = %(analysis_prompt_version)s
                   AND analyses.workflow_version = %(analysis_workflow_version)s
                   AND analyses.schema_version = %(analysis_schema_version)s
                 ORDER BY analyses.published_at_ms DESC, analyses.analysis_id DESC
                 LIMIT 1
              ) AS current_analysis ON TRUE
              LEFT JOIN LATERAL (
                SELECT attempts.status, attempts.last_error
                  FROM news_story_analysis_attempts AS attempts
                 WHERE attempts.story_id = stories.story_id
                   AND attempts.evidence_set_hash = stories.evidence_set_hash
                   AND %(analysis_enabled)s
                   AND attempts.model = %(analysis_model)s
                   AND attempts.prompt_version = %(analysis_prompt_version)s
                   AND attempts.workflow_version = %(analysis_workflow_version)s
                   AND attempts.schema_version = %(analysis_schema_version)s
                 ORDER BY attempts.updated_at_ms DESC, attempts.analysis_key DESC
                 LIMIT 1
              ) AS current_attempt ON TRUE
             WHERE stories.story_id = %(story_id)s
            """,
            {
                "story_id": story_id,
                **_analysis_contract_params(analysis_contract),
            },
        ).fetchone()
        if row is None:
            return None
        payload = dict(row)
        payload["articles"] = [_public_article(article) for article in self._story_articles(story_id)]
        payload["memberships"] = [
            dict(item)
            for item in self.conn.execute(
                """
                SELECT article_id, match_method, match_score, identity_version, admitted_at_ms, match_reason
                  FROM news_story_articles
                 WHERE story_id = %s
                 ORDER BY admitted_at_ms ASC, article_id ASC
                """,
                (story_id,),
            ).fetchall()
        ]
        return payload

    def list_sources(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT source_id, name, feed_url, source_domain, source_role, trust_tier,
                   source_chain_id, coverage_tags, default_language, enabled,
                   refresh_interval_seconds, last_fetch_started_at_ms,
                   last_fetch_finished_at_ms, last_success_at_ms, last_http_status,
                   consecutive_failures, last_error, next_fetch_at_ms
              FROM news_sources
             ORDER BY enabled DESC,
                      CASE trust_tier
                        WHEN 'authoritative' THEN 1
                        WHEN 'trusted' THEN 2
                        WHEN 'standard' THEN 3
                        ELSE 4
                      END,
                      name ASC,
                      source_id ASC
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def _upsert_article(self, article: Any) -> dict[str, bool]:
        existing = self.conn.execute(
            "SELECT content_hash FROM news_articles WHERE article_id = %s",
            (article.article_id,),
        ).fetchone()
        inserted = existing is None
        changed = inserted or str(existing["content_hash"]) != article.content_hash
        self.conn.execute(
            """
            INSERT INTO news_articles (
              article_id, source_id, identity_version, identity_method, identity_key,
              source_guid, canonical_url, title, snippet, published_at_ms,
              first_seen_at_ms, last_seen_at_ms, language, origin_url, origin_domain,
              origin_name, provenance_status, content_hash, source_entry
            )
            VALUES (
              %(article_id)s, %(source_id)s, %(identity_version)s, %(identity_method)s, %(identity_key)s,
              %(source_guid)s, %(canonical_url)s, %(title)s, %(snippet)s, %(published_at_ms)s,
              %(first_seen_at_ms)s, %(last_seen_at_ms)s, %(language)s, %(origin_url)s, %(origin_domain)s,
              %(origin_name)s, %(provenance_status)s, %(content_hash)s, %(source_entry)s
            )
            ON CONFLICT (article_id) DO UPDATE SET
              source_guid = EXCLUDED.source_guid,
              canonical_url = EXCLUDED.canonical_url,
              title = EXCLUDED.title,
              snippet = EXCLUDED.snippet,
              published_at_ms = EXCLUDED.published_at_ms,
              last_seen_at_ms = GREATEST(news_articles.last_seen_at_ms, EXCLUDED.last_seen_at_ms),
              language = EXCLUDED.language,
              origin_url = EXCLUDED.origin_url,
              origin_domain = EXCLUDED.origin_domain,
              origin_name = EXCLUDED.origin_name,
              provenance_status = EXCLUDED.provenance_status,
              content_hash = EXCLUDED.content_hash,
              source_entry = EXCLUDED.source_entry
            """,
            {
                **article.model_dump(exclude={"source_entry"}),
                "source_entry": Jsonb(article.source_entry),
            },
        )
        return {"inserted": inserted, "changed": changed}

    def _membership_for_article(self, article_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT story_id FROM news_story_articles WHERE article_id = %s",
            (article_id,),
        ).fetchone()
        return None if row is None else dict(row)

    def _story_candidates(self, article: Any) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT story_id, title, snippet
              FROM news_stories
             WHERE language = %(language)s
               AND last_seen_at_ms BETWEEN %(window_start)s AND %(window_end)s
             ORDER BY GREATEST(
                        similarity(title, %(title)s),
                        similarity(title || ' ' || snippet, %(document)s)
                      ) DESC,
                      last_seen_at_ms DESC,
                      story_id ASC
             LIMIT 500
            """,
            {
                "language": article.language,
                "title": article.title,
                "document": f"{article.title} {article.snippet}",
                "window_start": article.published_at_ms - _STORY_CANDIDATE_WINDOW_MS,
                "window_end": article.published_at_ms + _STORY_CANDIDATE_WINDOW_MS,
            },
        ).fetchall()
        return [dict(row) for row in rows]

    def _create_story(self, *, story_id: str, article: Any, now_ms: int) -> None:
        self.conn.execute(
            """
            INSERT INTO news_stories (
              story_id, anchor_article_id, primary_article_id, language, title, snippet,
              first_seen_at_ms, last_seen_at_ms, source_count, article_count,
              trusted_source_count, independent_origin_count, verification_status,
              phase, lifecycle_version, importance_score, importance_version,
              importance_factors, identity_version, evidence_set_hash,
              next_state_refresh_at_ms, created_at_ms, updated_at_ms
            )
            VALUES (
              %(story_id)s, %(article_id)s, %(article_id)s, %(language)s, %(title)s, %(snippet)s,
              %(first_seen_at_ms)s, %(first_seen_at_ms)s, 1, 1, 0, 0, 'unverified',
              'breaking', %(lifecycle_version)s, 0, %(importance_version)s,
              '{}'::jsonb, %(identity_version)s, %(evidence_set_hash)s,
              %(now_ms)s, %(now_ms)s, %(now_ms)s
            )
            ON CONFLICT (story_id) DO NOTHING
            """,
            {
                "story_id": story_id,
                "article_id": article.article_id,
                "language": article.language,
                "title": article.title,
                "snippet": article.snippet,
                "first_seen_at_ms": article.first_seen_at_ms,
                "lifecycle_version": STORY_LIFECYCLE_VERSION,
                "importance_version": STORY_IMPORTANCE_VERSION,
                "identity_version": STORY_IDENTITY_VERSION,
                "evidence_set_hash": article.content_hash,
                "now_ms": now_ms,
            },
        )

    def _add_membership(
        self,
        *,
        story_id: str,
        article_id: str,
        match_method: str,
        match_score: float,
        match_reason: Mapping[str, object],
        now_ms: int,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO news_story_articles (
              story_id, article_id, match_method, match_score,
              identity_version, admitted_at_ms, match_reason
            )
            VALUES (
              %(story_id)s, %(article_id)s, %(match_method)s, %(match_score)s,
              %(identity_version)s, %(admitted_at_ms)s, %(match_reason)s
            )
            ON CONFLICT (article_id) DO NOTHING
            """,
            {
                "story_id": story_id,
                "article_id": article_id,
                "match_method": match_method,
                "match_score": match_score,
                "identity_version": STORY_IDENTITY_VERSION,
                "admitted_at_ms": now_ms,
                "match_reason": Jsonb(dict(match_reason)),
            },
        )

    def _project_story(self, *, story_id: str, now_ms: int) -> bool:
        articles = self._story_articles(story_id)
        projection = project_story(articles, now_ms=now_ms)
        row = self.conn.execute(
            """
            UPDATE news_stories
               SET primary_article_id = %(primary_article_id)s,
                   title = %(title)s,
                   snippet = %(snippet)s,
                   first_seen_at_ms = %(first_seen_at_ms)s,
                   last_seen_at_ms = %(last_seen_at_ms)s,
                   source_count = %(source_count)s,
                   article_count = %(article_count)s,
                   trusted_source_count = %(trusted_source_count)s,
                   independent_origin_count = %(independent_origin_count)s,
                   verification_status = %(verification_status)s,
                   phase = %(phase)s,
                   lifecycle_version = %(lifecycle_version)s,
                   importance_score = %(importance_score)s,
                   importance_version = %(importance_version)s,
                   importance_factors = %(importance_factors)s,
                   evidence_set_hash = %(evidence_set_hash)s,
                   next_state_refresh_at_ms = %(next_state_refresh_at_ms)s,
                   updated_at_ms = %(now_ms)s
             WHERE story_id = %(story_id)s
               AND (
                 primary_article_id, title, snippet, first_seen_at_ms, last_seen_at_ms,
                 source_count, article_count, trusted_source_count, independent_origin_count,
                 verification_status, phase, lifecycle_version, importance_score,
                 importance_version, importance_factors, evidence_set_hash,
                 next_state_refresh_at_ms
               ) IS DISTINCT FROM (
                 %(primary_article_id)s, %(title)s, %(snippet)s, %(first_seen_at_ms)s, %(last_seen_at_ms)s,
                 %(source_count)s, %(article_count)s, %(trusted_source_count)s, %(independent_origin_count)s,
                 %(verification_status)s, %(phase)s, %(lifecycle_version)s, %(importance_score)s,
                 %(importance_version)s, %(importance_factors)s, %(evidence_set_hash)s,
                 %(next_state_refresh_at_ms)s
               )
            RETURNING story_id
            """,
            {
                "story_id": story_id,
                **asdict(projection),
                "lifecycle_version": STORY_LIFECYCLE_VERSION,
                "importance_version": STORY_IMPORTANCE_VERSION,
                "importance_factors": Jsonb(projection.importance_factors),
                "now_ms": now_ms,
            },
        ).fetchone()
        return row is not None

    def _story_articles(self, story_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT articles.*, sources.name AS source_name, sources.source_domain,
                   sources.source_role, sources.trust_tier, sources.source_chain_id
              FROM news_story_articles AS memberships
              JOIN news_articles AS articles ON articles.article_id = memberships.article_id
              JOIN news_sources AS sources ON sources.source_id = articles.source_id
             WHERE memberships.story_id = %s
             ORDER BY articles.published_at_ms ASC, articles.article_id ASC
            """,
            (story_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def _analysis_contract_params(
    contract: NewsAnalysisContract | None,
) -> dict[str, object]:
    return {
        "analysis_enabled": contract is not None,
        "analysis_model": contract.model if contract is not None else "",
        "analysis_prompt_version": contract.prompt_version if contract is not None else "",
        "analysis_workflow_version": contract.workflow_version if contract is not None else "",
        "analysis_schema_version": contract.schema_version if contract is not None else "",
    }


def analysis_key_for(
    *,
    story_id: str,
    evidence_set_hash: str,
    model: str,
    prompt_version: str,
    workflow_version: str,
    schema_version: str,
) -> str:
    raw = "\n".join(
        (story_id, evidence_set_hash, model, prompt_version, workflow_version, schema_version)
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _analysis_article(row: Mapping[str, object]) -> dict[str, object]:
    return {
        "article_id": str(row["article_id"]),
        "title": str(row["title"]),
        "snippet": str(row["snippet"]),
        "published_at_ms": _required_int(row["published_at_ms"]),
        "first_seen_at_ms": _required_int(row["first_seen_at_ms"]),
        "source_id": str(row["source_id"]),
        "source_name": str(row["source_name"]),
        "source_role": str(row["source_role"]),
        "trust_tier": str(row["trust_tier"]),
        "source_chain_id": str(row["source_chain_id"]),
        "canonical_url": row.get("canonical_url"),
        "origin_url": row.get("origin_url"),
        "origin_domain": row.get("origin_domain"),
        "origin_name": row.get("origin_name"),
        "provenance_status": str(row["provenance_status"]),
    }


def _public_article(row: Mapping[str, object]) -> dict[str, object]:
    payload = _analysis_article(row)
    payload.update(
        {
            "language": str(row["language"]),
            "first_seen_at_ms": _required_int(row["first_seen_at_ms"]),
            "last_seen_at_ms": _required_int(row["last_seen_at_ms"]),
            "identity_method": str(row["identity_method"]),
            "identity_version": str(row["identity_version"]),
        }
    )
    return payload


def _bounded_error(value: object, limit: int = 1000) -> str:
    normalized = " ".join(str(value or "").strip().split())
    return normalized[:limit] or "unknown_error"


def _required_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("news_integer_required")
    return value


__all__ = ["NewsRepository", "analysis_key_for"]
