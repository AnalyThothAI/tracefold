"""News-owned material reads. Callers freeze and select outside transactions."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..evidence import CANDIDATE_MAX, ENTITY_MAX, RELATION_MAX, SIMILAR_MAX, DocumentResult, EvidenceQuery

ITEM_MATERIAL_COLUMNS = """item_id, source_artifact_id, canonical_url, reporting_origin, published_at_ms,
    provider_params_available_at_ms, provider_params_sha256, evidence_text, evidence_text_sha256,
    provider_params_conflict_sha256, provider_params_conflict_at_ms"""

# Each channel first returns lightweight identities. Text is loaded only for the
# final source/fact-deduplicated shortlist. These statements also serve EXPLAIN audits.
BACKGROUND_CANDIDATES_SQL = """
WITH explicit_unique AS (
  SELECT DISTINCT ON (COALESCE(NULLIF(i.source_artifact_id,''), NULLIF(i.canonical_url,''), i.item_id),
                       e.comparison_fingerprint) e.event_id, e.leader_item_id AS item_id,
         e.comparison_title, e.comparison_fingerprint,
         e.focus_fact_text AS leader_title, e.focus_fact_context AS leader_description, e.focus_fact_method,
         e.created_at_ms, i.source_artifact_id, i.canonical_url, 0 AS priority,
         1.0::real AS score, 'explicit_origin'::text AS retrieval_reason
    FROM news_events e JOIN news_items i ON i.item_id = e.leader_item_id
   WHERE e.event_id <> %(event_id)s AND e.updated_at_ms <= %(cutoff)s AND e.created_at_ms < %(cutoff)s
     AND i.provider_params_available_at_ms < %(cutoff)s AND i.market_kind IS NULL
     AND ((%(artifact)s <> '' AND i.source_artifact_id = %(artifact)s)
          OR (%(url)s <> '' AND i.canonical_url = %(url)s))
   ORDER BY COALESCE(NULLIF(i.source_artifact_id,''), NULLIF(i.canonical_url,''), i.item_id),
            e.comparison_fingerprint, e.created_at_ms DESC, e.event_id
), explicit AS (
  SELECT * FROM explicit_unique ORDER BY created_at_ms DESC, event_id LIMIT %(relation_max)s
), entity_unique AS (
  SELECT DISTINCT ON (COALESCE(NULLIF(i.source_artifact_id,''), NULLIF(i.canonical_url,''), i.item_id),
                       e.comparison_fingerprint) e.event_id, e.leader_item_id AS item_id,
         e.comparison_title, e.comparison_fingerprint,
         e.focus_fact_text AS leader_title, e.focus_fact_context AS leader_description, e.focus_fact_method,
         e.created_at_ms, i.source_artifact_id, i.canonical_url, 1 AS priority,
         similarity(e.comparison_title, %(title)s) AS score, 'entity_event_terms'::text AS retrieval_reason
    FROM news_events e JOIN news_items i ON i.item_id = e.leader_item_id
   WHERE e.event_id <> %(event_id)s AND e.updated_at_ms <= %(cutoff)s
     AND e.created_at_ms >= %(since)s AND e.created_at_ms < %(cutoff)s
     AND i.provider_params_available_at_ms < %(cutoff)s AND i.market_kind IS NULL
     AND EXISTS (SELECT 1 FROM news_event_assets a WHERE a.event_id=e.event_id AND a.symbol=ANY(%(symbols)s))
     AND (e.asset_class IN ('unknown', 'none', 'equity_or_commod') OR e.asset_class=ANY(%(markets)s))
     AND EXISTS (SELECT 1 FROM unnest(%(terms)s::text[]) t WHERE position(t in lower(e.comparison_title)) > 0
                 AND upper(t) <> ALL(%(symbols)s))
   ORDER BY COALESCE(NULLIF(i.source_artifact_id,''), NULLIF(i.canonical_url,''), i.item_id),
            e.comparison_fingerprint, score DESC, e.created_at_ms DESC, e.event_id
), entity AS (
  SELECT * FROM entity_unique ORDER BY score DESC, created_at_ms DESC, event_id LIMIT %(entity_max)s
), similarity_band_unique AS (
  SELECT DISTINCT ON (COALESCE(NULLIF(i.source_artifact_id,''), NULLIF(i.canonical_url,''), i.item_id),
                       e.comparison_fingerprint) e.event_id, e.leader_item_id AS item_id,
         e.comparison_title, e.comparison_fingerprint,
         e.focus_fact_text AS leader_title, e.focus_fact_context AS leader_description, e.focus_fact_method,
         e.created_at_ms, i.source_artifact_id, i.canonical_url, 2 AS priority,
         similarity(e.comparison_title, %(title)s) AS score, 'text_similarity'::text AS retrieval_reason
    FROM news_events e JOIN news_items i ON i.item_id = e.leader_item_id
   WHERE e.event_id <> %(event_id)s AND e.updated_at_ms <= %(cutoff)s
     AND e.created_at_ms >= %(since)s AND e.created_at_ms < %(cutoff)s
     AND i.provider_params_available_at_ms < %(cutoff)s AND i.market_kind IS NULL
     AND similarity(e.comparison_title, %(title)s) >= 0.3
   ORDER BY COALESCE(NULLIF(i.source_artifact_id,''), NULLIF(i.canonical_url,''), i.item_id),
            e.comparison_fingerprint, e.comparison_title <-> %(title)s, e.created_at_ms DESC, e.event_id
), similarity_band AS (
  SELECT * FROM similarity_band_unique ORDER BY score DESC, created_at_ms DESC, event_id LIMIT %(similar_max)s
), channels AS (SELECT * FROM explicit UNION ALL SELECT * FROM entity UNION ALL SELECT * FROM similarity_band)
SELECT DISTINCT ON (event_id) event_id, item_id, comparison_title, comparison_fingerprint, created_at_ms,
       source_artifact_id, canonical_url, priority, score, retrieval_reason,
       leader_title, leader_description, focus_fact_method
  FROM channels ORDER BY event_id, priority, score DESC LIMIT %(candidate_max)s
"""


def background_parameters(query: EvidenceQuery) -> dict[str, Any]:
    return dict(
        event_id=query.event_id,
        cutoff=query.cutoff_at_ms,
        since=query.cutoff_at_ms - query.window_ms,
        artifact=query.source_artifact_id,
        url=query.canonical_url,
        title=query.title,
        symbols=[a.symbol for a in query.assets],
        markets=[a.market_type for a in query.assets],
        terms=list(query.terms),
        relation_max=RELATION_MAX,
        entity_max=ENTITY_MAX,
        similar_max=SIMILAR_MAX,
        candidate_max=CANDIDATE_MAX,
    )


class EvidenceStorage:
    conn: Any

    def evidence_item(self, item_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT " + ITEM_MATERIAL_COLUMNS + " FROM news_items WHERE item_id=%s",  # noqa: S608 - constant columns
            (item_id,),
        ).fetchone()
        return dict(row) if row else {}

    def evidence_candidates(self, query: EvidenceQuery) -> list[dict[str, Any]]:
        return [
            dict(row) for row in self.conn.execute(BACKGROUND_CANDIDATES_SQL, background_parameters(query)).fetchall()
        ]

    def evidence_background_material(self, item_ids: Sequence[str]) -> list[dict[str, Any]]:
        # Read exactly the immutable Items selected by the lightweight query, never
        # re-resolve a mutable Event leader between the two reads.
        if not item_ids:
            return []
        return [
            dict(row)
            for row in self.conn.execute(
                "SELECT " + ITEM_MATERIAL_COLUMNS + " FROM news_items WHERE item_id=ANY(%s)",  # noqa: S608
                (list(item_ids),),
            ).fetchall()
        ]

    def evidence_document(self, url: str, *, cutoff: int, since: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT document_id, requested_url, final_url, normalized_url, response_sha256,
                   extracted_text_sha256, extractor_version, extracted_text, reported_published_at_ms,
                   observed_at_ms, available_at_ms, content_type, extraction_status AS status
              FROM news_evidence_documents
             WHERE normalized_url=%s AND available_at_ms <= %s AND available_at_ms >= %s
             ORDER BY available_at_ms DESC, document_id LIMIT 1
            """,
            (url, cutoff, since),
        ).fetchone()
        return dict(row) if row else None

    def save_evidence_document(self, document: DocumentResult) -> None:
        self.conn.execute(
            """
            INSERT INTO news_evidence_documents(document_id, requested_url, final_url, normalized_url,
                response_sha256, extracted_text_sha256, extractor_version, extracted_text,
                reported_published_at_ms, observed_at_ms, available_at_ms, content_type, extraction_status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'success')
            ON CONFLICT (document_id) DO NOTHING
            """,
            (
                document.document_id,
                document.requested_url,
                document.final_url,
                document.normalized_url,
                document.response_sha256,
                document.extracted_text_sha256,
                document.extractor_version,
                document.extracted_text,
                document.reported_published_at_ms,
                document.observed_at_ms,
                document.available_at_ms,
                document.content_type,
            ),
        )
