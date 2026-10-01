"""News-owned material reads. Callers freeze and select outside transactions."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..entities import asset_retrieval_symbols, stored_asset_codes
from ..evidence import (
    CANDIDATE_MAX,
    ENTITY_MAX,
    RELATION_MAX,
    SIMILAR_MAX,
    SIMILARITY_PROBE_CHARS_MAX,
    SIMILARITY_PROBE_TEXTS_MAX,
    EvidenceQuery,
)

ITEM_MATERIAL_COLUMNS = """item_id, source_artifact_id, canonical_url, reporting_origin, published_at_ms,
    provider_params_available_at_ms, provider_params_sha256, evidence_text, evidence_text_sha256"""

# Related-Event recall (#771). Each channel draws a bounded set of Event ids from an index, and only that pool is
# scored, deduplicated by source and fact, and capped:
#
# - explicit: the task sources' artifact ids and URLs, through the item indexes and the Event leader index;
# - entity: the tagged assets' generated retrieval codes (`news_event_assets.retrieval_symbol` and
#   `retrieval_pair_base`), then the Event-term match on the matching Events only;
# - similarity: the member-fact trigram index, probed with the first short task texts.
#
# The time window applies to every channel: URL equality does not authorise a history scan. Nothing evaluates
# every Event of the window; `test_news_recall_bounds` holds the plan to that at production scale.
_WINDOW = """e.event_id <> %(event_id)s AND e.updated_at_ms <= %(cutoff)s
    AND e.created_at_ms >= %(since)s AND e.created_at_ms < %(cutoff)s
    AND i.provider_params_available_at_ms <= %(cutoff)s AND i.market_kind IS NULL"""
_ORIGIN = "COALESCE(NULLIF(source_artifact_id,''), NULLIF(canonical_url,''), item_id)"


def _channel_sql(
    name: str, *, ids: str, priority: int, reason: str, cap: str, order: str, predicate: str = "TRUE"
) -> str:
    return f"""{name}_raw AS (
      SELECT s.*, {priority} AS priority, '{reason}'::text AS retrieval_reason
        FROM scored s WHERE s.event_id IN (SELECT event_id FROM {ids}) AND {predicate}
       ORDER BY {order} LIMIT %(candidate_max)s
    ), {name}_unique AS (
      SELECT DISTINCT ON ({_ORIGIN}, comparison_fingerprint) *
        FROM {name}_raw
       ORDER BY {_ORIGIN}, comparison_fingerprint, score DESC, created_at_ms DESC, event_id
    ), {name} AS (
      SELECT * FROM {name}_unique ORDER BY score DESC, created_at_ms DESC, event_id LIMIT %({cap})s
    )"""  # noqa: S608 - only constant SQL fragments; values remain bound parameters


BACKGROUND_CANDIDATES_SQL = (
    f"""WITH explicit_ids AS MATERIALIZED (
      SELECT e.event_id
        FROM (SELECT item_id FROM news_items
               WHERE source_artifact_id = ANY(%(artifacts)s::text[]) AND source_artifact_id <> ''
              UNION
              SELECT item_id FROM news_items WHERE canonical_url = ANY(%(urls)s::text[])) origin
        JOIN news_events e ON e.leader_item_id = origin.item_id
        JOIN news_items i ON i.item_id = e.leader_item_id
       WHERE {_WINDOW}
       ORDER BY e.created_at_ms DESC, e.event_id LIMIT %(candidate_max)s
    ), entity_ids AS MATERIALIZED (
      -- Tagged Events newest first (one keyed lookup each), then the leader and Event-term checks lazily: the
      -- LIMIT stops the walk once enough Events pass, however common the asset.
      SELECT tagged.event_id
        FROM (SELECT e.event_id, e.leader_item_id, e.comparison_title, e.created_at_ms
                FROM (SELECT DISTINCT a.event_id FROM news_event_assets a
                       WHERE a.retrieval_symbol = ANY(%(stored_codes)s::text[])
                          OR a.retrieval_pair_base = ANY(%(symbols)s::text[])) asset
                -- LIMIT keeps the keyed lookup a lookup: a flattened join may scan every Event.
                CROSS JOIN LATERAL (SELECT * FROM news_events e WHERE e.event_id = asset.event_id LIMIT 1) e
               WHERE cardinality(%(symbols)s::text[]) > 0 AND e.event_id <> %(event_id)s
                 AND e.updated_at_ms <= %(cutoff)s AND e.created_at_ms >= %(since)s AND e.created_at_ms < %(cutoff)s
               ORDER BY e.created_at_ms DESC, e.event_id) tagged
        JOIN news_items i ON i.item_id = tagged.leader_item_id
       WHERE i.provider_params_available_at_ms <= %(cutoff)s AND i.market_kind IS NULL AND EXISTS (
         SELECT 1
           FROM (SELECT lower(tagged.comparison_title) AS text
                 UNION ALL SELECT lower(m.fact_text) FROM news_event_members m
                            WHERE m.event_id = tagged.event_id AND m.joined_at_ms <= %(cutoff)s) own
           JOIN unnest(%(terms)s::text[]) t ON position(t in own.text) > 0
          WHERE upper(t) <> ALL(%(symbols)s::text[]))
       ORDER BY tagged.created_at_ms DESC, tagged.event_id LIMIT %(candidate_max)s
    ), similarity_ids AS MATERIALIZED (
      -- Events with a member fact at the trigram threshold of a probe text. The leader fact stands in for the
      -- title it was normalised from, so it is probed even when its provider clock is ahead of the cutoff; the
      -- channel's predicate is applied to the scored texts below.
      SELECT DISTINCT matched.event_id
        FROM unnest(%(probe_texts)s::text[]) probe(text)
        CROSS JOIN LATERAL (SELECT m.event_id FROM news_event_members m WHERE m.fact_text %% probe.text) matched
    ), pool AS MATERIALIZED (
      SELECT event_id FROM explicit_ids UNION SELECT event_id FROM entity_ids UNION SELECT event_id FROM similarity_ids
    ), task AS MATERIALIZED (
      SELECT text, cardinality(show_trgm(text)) AS trigrams, length(text) > %(near_text_chars)s AS far
        FROM unnest(%(texts)s::text[]) text
    ), scored AS MATERIALIZED (
      -- The best trigram similarity of any task text to the title or a member fact: the number the window-wide
      -- scan computed, for the pool only. A far (long) text is compared only when it could beat the near texts'
      -- best: sim(a, b) <= |trgm(a)| / |trgm(b)| and |trgm(a)| <= 1.5 * length(a) + 0.5, so a skipped pair never
      -- changes the maximum.
      SELECT e.event_id, e.leader_item_id AS item_id, e.comparison_title, e.comparison_fingerprint,
             e.focus_fact_text AS leader_title, e.focus_fact_context AS leader_description, e.focus_fact_method,
             e.created_at_ms, e.asset_class, e.grounded_assets, i.source_artifact_id, i.canonical_url,
             greatest(near.score, far.score) AS score
        FROM pool
        CROSS JOIN LATERAL (SELECT * FROM news_events e WHERE e.event_id = pool.event_id LIMIT 1) e
        JOIN news_items i ON i.item_id = e.leader_item_id
        CROSS JOIN LATERAL (
          SELECT array_agg(DISTINCT own.text) AS texts
            FROM (SELECT e.comparison_title AS text
                  UNION ALL SELECT m.fact_text FROM news_event_members m
                             WHERE m.event_id = e.event_id AND m.joined_at_ms <= %(cutoff)s) own) own
        CROSS JOIN LATERAL (
          SELECT COALESCE(max(similarity(o.text, task.text)), 0) AS score
            FROM unnest(own.texts) o(text) CROSS JOIN task WHERE NOT task.far) near
        CROSS JOIN LATERAL (
          SELECT COALESCE(max(similarity(o.text, task.text)), 0) AS score
            FROM unnest(own.texts) o(text) CROSS JOIN task
           WHERE task.far AND 1.5 * length(o.text) + 1 >= near.score * task.trigrams) far
       WHERE {_WINDOW}
    ), """  # noqa: S608 - assembled from code-owned SQL fragments; all values are bound
    + ", ".join(
        (
            _channel_sql(
                "explicit",
                ids="explicit_ids",
                priority=0,
                reason="explicit_origin",
                cap="relation_max",
                order="s.created_at_ms DESC, s.event_id",
            ),
            _channel_sql(
                "entity",
                ids="entity_ids",
                priority=1,
                reason="entity_event_terms",
                cap="entity_max",
                order="s.created_at_ms DESC, s.event_id",
            ),
            _channel_sql(
                "similarity_band",
                ids="similarity_ids",
                priority=2,
                reason="text_similarity",
                cap="similar_max",
                order="s.score DESC, s.created_at_ms DESC, s.event_id",
                # `%` itself: some title or visible member fact at the trigram threshold of some task text. The
                # library default answers while pg_trgm is not yet loaded (only planning can ask that early).
                predicate="s.score::float8 >= "
                "COALESCE(current_setting('pg_trgm.similarity_threshold', true), '0.3')::float8",
            ),
        )
    )
    + """, channels AS (SELECT * FROM explicit UNION ALL SELECT * FROM entity UNION ALL SELECT * FROM similarity_band),
merged AS (SELECT DISTINCT ON (event_id) * FROM channels ORDER BY event_id, priority, score DESC
           LIMIT %(candidate_max)s)
SELECT m.event_id, m.item_id, m.comparison_title, m.comparison_fingerprint, m.leader_title, m.leader_description,
       m.focus_fact_method, m.created_at_ms, m.asset_class, m.grounded_assets, m.source_artifact_id, m.canonical_url,
       ARRAY(SELECT f.fact_text FROM news_event_members f
              WHERE f.event_id = m.event_id AND f.joined_at_ms <= %(cutoff)s
              ORDER BY f.joined_at_ms, f.item_id, f.fact_id) AS task_texts,
       m.priority, m.score, m.retrieval_reason,
       COALESCE((
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
 ORDER BY m.event_id
"""
)


def similarity_probe_texts(texts: Sequence[str]) -> list[str]:
    """The task texts the similarity channel probes the member-fact index with, in task order."""

    return [text for text in texts if len(text) <= SIMILARITY_PROBE_CHARS_MAX][:SIMILARITY_PROBE_TEXTS_MAX]


def background_parameters(query: EvidenceQuery) -> dict[str, Any]:
    symbols = sorted(
        {symbol for asset in query.assets for symbol in asset_retrieval_symbols(asset.symbol, asset.market_type)}
    )
    return dict(
        event_id=query.event_id,
        cutoff=query.cutoff_at_ms,
        since=query.cutoff_at_ms - query.window_ms,
        # An empty origin identifies nothing (the artifact index covers non-empty ids only).
        artifacts=[value for value in query.source_artifact_ids if value],
        urls=[value for value in query.canonical_urls if value],
        texts=list(query.texts),
        probe_texts=similarity_probe_texts(query.texts),
        near_text_chars=SIMILARITY_PROBE_CHARS_MAX,
        symbols=symbols,
        stored_codes=list(stored_asset_codes(symbols)),
        terms=list(query.terms),
        relation_max=RELATION_MAX,
        entity_max=ENTITY_MAX,
        similar_max=SIMILAR_MAX,
        candidate_max=CANDIDATE_MAX,
    )


class EvidenceStorage:
    conn: Any

    def evidence_candidates(self, query: EvidenceQuery) -> list[dict[str, Any]]:
        # Never a server-side prepared statement: the right plan depends on the bound arrays (how common an asset
        # is, which texts probe), and a cached generic plan of this statement measured 10-20x slower (#771).
        return [
            dict(row)
            for row in self.conn.execute(
                BACKGROUND_CANDIDATES_SQL, background_parameters(query), prepare=False
            ).fetchall()
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
