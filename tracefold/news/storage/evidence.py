"""News-owned material reads. Callers freeze and select outside transactions."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..evidence import CANDIDATE_MAX, ENTITY_MAX, RELATION_MAX, SIMILAR_MAX, EvidenceQuery

ITEM_MATERIAL_COLUMNS = """item_id, source_artifact_id, canonical_url, reporting_origin, published_at_ms,
    provider_params_available_at_ms, provider_params_sha256, evidence_text, evidence_text_sha256,
    provider_params_conflict_sha256, provider_params_conflict_at_ms"""

# Each channel first returns lightweight identities. Text is loaded only for the
# final source/fact-deduplicated shortlist. These statements also serve EXPLAIN audits.
# Every channel bounds its raw identities before source/fact DISTINCT. The time
# window applies even to source links: URL equality does not authorize a history scan.
_CANDIDATE_COLUMNS = """e.event_id, e.leader_item_id AS item_id, e.comparison_title,
    e.comparison_fingerprint, e.focus_fact_text AS leader_title,
    e.focus_fact_context AS leader_description, e.focus_fact_method, e.created_at_ms,
    e.asset_class, e.grounded_assets, i.source_artifact_id, i.canonical_url"""
_CANDIDATE_WHERE = """e.event_id <> %(event_id)s AND e.updated_at_ms <= %(cutoff)s
    AND e.created_at_ms >= %(since)s AND e.created_at_ms < %(cutoff)s
    AND i.provider_params_available_at_ms <= %(cutoff)s AND i.market_kind IS NULL"""


def _channel_sql(
    name: str, *, predicate: str, priority: int, reason: str, cap: str, order: str = "e.created_at_ms DESC, e.event_id"
) -> str:
    return f"""{name}_raw AS (  -- code-owned fragments only

      SELECT {_CANDIDATE_COLUMNS}, {priority} AS priority,
             similarity(e.comparison_title, %(title)s) AS score, '{reason}'::text AS retrieval_reason
        FROM news_events e JOIN news_items i ON i.item_id=e.leader_item_id
       WHERE {_CANDIDATE_WHERE} AND ({predicate})
       ORDER BY {order} LIMIT %(candidate_max)s
    ), {name}_unique AS (
      SELECT DISTINCT ON (COALESCE(NULLIF(source_artifact_id,''), NULLIF(canonical_url,''), item_id),
                          comparison_fingerprint) *
        FROM {name}_raw
       ORDER BY COALESCE(NULLIF(source_artifact_id,''), NULLIF(canonical_url,''), item_id),
                comparison_fingerprint, score DESC, created_at_ms DESC, event_id
    ), {name} AS (
      SELECT * FROM {name}_unique ORDER BY score DESC, created_at_ms DESC, event_id LIMIT %({cap})s
    )"""  # noqa: S608 - only constant SQL fragments; values remain bound parameters


BACKGROUND_CANDIDATES_SQL = (
    "WITH "  # noqa: S608 - assembled from code-owned SQL fragments; all values are bound
    + ", ".join(
        (
            _channel_sql(
                "explicit",
                predicate="""(%(artifact)s <> '' AND i.source_artifact_id=%(artifact)s)
                   OR (%(url)s <> '' AND i.canonical_url=%(url)s)""",
                priority=0,
                reason="explicit_origin",
                cap="relation_max",
            ),
            _channel_sql(
                "entity",
                predicate="""EXISTS (SELECT 1 FROM news_event_assets a
                   WHERE a.event_id=e.event_id AND a.symbol=ANY(%(symbols)s))
                   AND EXISTS (SELECT 1 FROM unnest(%(terms)s::text[]) t
                       WHERE position(t in lower(e.comparison_title)) > 0 AND upper(t) <> ALL(%(symbols)s))""",
                priority=1,
                reason="entity_event_terms",
                cap="entity_max",
            ),
            _channel_sql(
                "similarity_band",
                predicate="e.comparison_title %% %(title)s",
                priority=2,
                reason="text_similarity",
                cap="similar_max",
                order="e.comparison_title <-> %(title)s, e.created_at_ms DESC, e.event_id",
            ),
        )
    )
    + """, channels AS (SELECT * FROM explicit UNION ALL SELECT * FROM entity UNION ALL SELECT * FROM similarity_band),
merged AS (SELECT DISTINCT ON (event_id) * FROM channels ORDER BY event_id, priority, score DESC
           LIMIT %(candidate_max)s)
SELECT m.*, COALESCE(v.verdict->'assets', '[]'::jsonb) ||
       COALESCE((SELECT jsonb_agg(jsonb_build_object('symbol', a.symbol, 'market_type', a.market_type))
                 FROM news_event_assets a WHERE a.event_id=m.event_id), '[]'::jsonb) AS assets
  FROM merged m LEFT JOIN LATERAL (
    SELECT verdict FROM news_verdicts WHERE event_id=m.event_id AND stage='triage'
      AND created_at_ms <= %(cutoff)s ORDER BY created_at_ms DESC, policy_version DESC LIMIT 1
  ) v ON true
"""
)


def background_parameters(query: EvidenceQuery) -> dict[str, Any]:
    return dict(
        event_id=query.event_id,
        cutoff=query.cutoff_at_ms,
        since=query.cutoff_at_ms - query.window_ms,
        artifact=query.source_artifact_id,
        url=query.canonical_url,
        title=query.title,
        symbols=[a.symbol for a in query.assets],
        terms=list(query.terms),
        relation_max=RELATION_MAX,
        entity_max=ENTITY_MAX,
        similar_max=SIMILAR_MAX,
        candidate_max=CANDIDATE_MAX,
    )


class EvidenceStorage:
    conn: Any

    def evidence_candidates(self, query: EvidenceQuery) -> list[dict[str, Any]]:
        return [
            dict(row) for row in self.conn.execute(BACKGROUND_CANDIDATES_SQL, background_parameters(query)).fetchall()
        ]

    def evidence_material(self, item_ids: Sequence[str]) -> list[dict[str, Any]]:
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

    def evidence_member_metadata(self, item_ids: Sequence[str]) -> list[dict[str, Any]]:
        if not item_ids:
            return []
        return [
            dict(row)
            for row in self.conn.execute(
                """SELECT item_id, evidence_text_sha256, provider_params_available_at_ms
                 FROM news_items WHERE item_id=ANY(%s)""",
                (list(item_ids),),
            ).fetchall()
        ]
