"""News-owned material reads. Callers freeze and select outside transactions."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

from psycopg.types.json import Jsonb

from ..entities import ADDRESS_PATTERN, CRYPTO_QUOTE_SUFFIXES, RELATED_ASSET_ALIASES, asset_retrieval_symbols
from ..evidence import CANDIDATE_MAX, ENTITY_MAX, RELATION_MAX, SIMILAR_MAX, EvidenceQuery

# The trigram channels compare each task text with title-sized `comparison_title` / member `fact_text`
# values, so the bound text is title-sized too. `similarity()` and `%` over a whole body cost body
# length x window rows and never reach the 0.3 threshold against a title anyway (0.11 measured for the
# same story); the full texts still drive terms and assets in `query_for`.
CANDIDATE_TEXT_MAX: Final = 400

ITEM_MATERIAL_COLUMNS = """item_id, source_artifact_id, canonical_url, reporting_origin, published_at_ms,
    provider_params_available_at_ms, provider_params_sha256, evidence_text, evidence_text_sha256"""

# Each channel first returns lightweight identities. Text is loaded only for the
# final source/fact-deduplicated shortlist. These statements also serve EXPLAIN audits.
# Every channel bounds its raw identities before source/fact DISTINCT. The time
# window applies even to source links: URL equality does not authorize a history scan.
_CANDIDATE_COLUMNS = """e.event_id, e.leader_item_id AS item_id, e.comparison_title,
    e.comparison_fingerprint, e.focus_fact_text AS leader_title,
    e.focus_fact_context AS leader_description, e.focus_fact_method, e.created_at_ms,
    e.asset_class, e.grounded_assets, i.source_artifact_id, i.canonical_url,
    ARRAY(SELECT m.fact_text FROM news_event_members m
           WHERE m.event_id=e.event_id AND m.joined_at_ms <= %(cutoff)s
           ORDER BY m.joined_at_ms, m.item_id, m.fact_id) AS task_texts"""
_CANDIDATE_WHERE = """e.event_id <> %(event_id)s AND e.updated_at_ms <= %(cutoff)s
    AND e.created_at_ms >= %(since)s AND e.created_at_ms < %(cutoff)s
    AND i.provider_params_available_at_ms <= %(cutoff)s AND i.market_kind IS NULL"""


def _channel_sql(
    name: str, *, predicate: str, priority: int, reason: str, cap: str, order: str = "e.created_at_ms DESC, e.event_id"
) -> str:
    return f"""{name}_raw AS (  -- code-owned fragments only

      SELECT {_CANDIDATE_COLUMNS}, {priority} AS priority,
             greatest(
                 COALESCE((SELECT max(similarity(e.comparison_title, text))
                             FROM unnest(%(texts)s::text[]) text), 0),
                 COALESCE((SELECT max(similarity(m.fact_text, text))
                             FROM news_event_members m CROSS JOIN unnest(%(texts)s::text[]) text
                            WHERE m.event_id=e.event_id AND m.joined_at_ms <= %(cutoff)s), 0)
             ) AS score, '{reason}'::text AS retrieval_reason
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
                predicate="""i.source_artifact_id=ANY(%(artifacts)s::text[])
                   OR i.canonical_url=ANY(%(urls)s::text[])""",
                priority=0,
                reason="explicit_origin",
                cap="relation_max",
            ),
            _channel_sql(
                "entity",
                predicate="""cardinality(%(symbols)s::text[]) > 0 AND EXISTS (
                     SELECT 1 FROM news_event_assets a
                      CROSS JOIN LATERAL (
                        SELECT regexp_replace(a.symbol, '^[[:space:]$]+|[[:space:]]+$', '', 'g') AS text
                      ) tagged
                      CROSS JOIN LATERAL (
                        SELECT CASE WHEN tagged.text ~ %(address_pattern)s THEN tagged.text
                          ELSE regexp_replace(regexp_replace(upper(tagged.text), '^XYZ-', ''), '^[^:]*:', '')
                          END AS symbol
                      ) normalized
                      WHERE a.event_id=e.event_id AND (
                        normalized.symbol=ANY(%(symbols)s)
                        OR COALESCE(%(aliases)s::jsonb ->> normalized.symbol, normalized.symbol)=ANY(%(symbols)s)
                        OR (COALESCE(a.market_type,'unknown') IN ('crypto','unknown')
                            AND tagged.text !~ %(address_pattern)s AND (
                          SELECT left(normalized.symbol, length(normalized.symbol)-length(quote))
                            FROM unnest(%(quotes)s::text[]) WITH ORDINALITY quotes(quote, rank)
                           WHERE right(normalized.symbol, length(quote))=quote
                             AND length(normalized.symbol) > length(quote)+1
                           ORDER BY rank LIMIT 1
                        )=ANY(%(symbols)s))
                      )
                   )
                   AND EXISTS (
                     SELECT 1 FROM unnest(%(terms)s::text[]) t
                      WHERE upper(t) <> ALL(%(symbols)s) AND (
                        position(t in lower(e.comparison_title)) > 0
                        OR EXISTS (SELECT 1 FROM news_event_members m
                                    WHERE m.event_id=e.event_id AND m.joined_at_ms <= %(cutoff)s
                                      AND position(t in lower(m.fact_text)) > 0)
                      )
                   )""",
                priority=1,
                reason="entity_event_terms",
                cap="entity_max",
            ),
            _channel_sql(
                "similarity_band",
                predicate="""EXISTS (SELECT 1 FROM unnest(%(texts)s::text[]) text
                       WHERE e.comparison_title %% text
                          OR EXISTS (SELECT 1 FROM news_event_members m
                                      WHERE m.event_id=e.event_id AND m.joined_at_ms <= %(cutoff)s
                                        AND m.fact_text %% text))""",
                priority=2,
                reason="text_similarity",
                cap="similar_max",
                order="score DESC, e.created_at_ms DESC, e.event_id",
            ),
        )
    )
    + """, channels AS (SELECT * FROM explicit UNION ALL SELECT * FROM entity UNION ALL SELECT * FROM similarity_band),
merged AS (SELECT DISTINCT ON (event_id) * FROM channels ORDER BY event_id, priority, score DESC
           LIMIT %(candidate_max)s)
SELECT m.*, COALESCE((
         SELECT jsonb_agg(asset)
           FROM (SELECT document FROM news_event_updates u
                  WHERE u.event_id=m.event_id AND u.adopted_at_ms <= %(cutoff)s
                  ORDER BY u.adopted_at_ms DESC, u.content_revision DESC LIMIT 1) latest
           CROSS JOIN LATERAL jsonb_array_elements(latest.document -> 'claims') claim
           CROSS JOIN LATERAL jsonb_array_elements(claim -> 'fields' -> 'assets') asset
          WHERE NOT (COALESCE(latest.document -> 'retired_claim_refs', '[]'::jsonb) ? (claim ->> 'ref'))
            AND NOT (COALESCE(latest.document -> 'superseded_claim_refs', '[]'::jsonb) ? (claim ->> 'ref'))
       ), '[]'::jsonb) ||
       COALESCE((SELECT jsonb_agg(jsonb_build_object('symbol', a.symbol, 'market_type', a.market_type))
                 FROM news_event_assets a WHERE a.event_id=m.event_id), '[]'::jsonb) AS assets
  FROM merged m
"""
)


def background_parameters(query: EvidenceQuery) -> dict[str, Any]:
    return dict(
        event_id=query.event_id,
        cutoff=query.cutoff_at_ms,
        since=query.cutoff_at_ms - query.window_ms,
        artifacts=list(query.source_artifact_ids),
        urls=list(query.canonical_urls),
        texts=[text[:CANDIDATE_TEXT_MAX] for text in query.texts],
        symbols=sorted(
            {symbol for asset in query.assets for symbol in asset_retrieval_symbols(asset.symbol, asset.market_type)}
        ),
        aliases=Jsonb(RELATED_ASSET_ALIASES),
        quotes=list(CRYPTO_QUOTE_SUFFIXES),
        address_pattern=ADDRESS_PATTERN,
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
