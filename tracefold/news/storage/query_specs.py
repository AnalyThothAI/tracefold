"""Bound News V3 read statements for the PostgreSQL query audit (EXPLAIN coverage)."""

from __future__ import annotations

from tracefold.platform.postgres.audit import ReadQuerySpec

from ..market_review.pricing import REACTION_METRIC_VERSION
from ..review.desk import review_read_statements
from .feed_sql import ASSET_SEARCH_PREDICATE, TEXT_SEARCH_PREDICATE, feed_counts_sql, feed_page_sql


def news_query_specs(*, now_ms: int) -> tuple[ReadQuerySpec, ...]:
    day_ago = int(now_ms) - 24 * 3600_000
    hour_ago = int(now_ms) - 3600_000
    week_ago = int(now_ms) - 168 * 3600_000
    search_base = "e.ingest_mode IN ('live', 'recovery') AND e.opened_at_ms >= %s"
    search_cursor = "(e.opened_at_ms, e.event_id) < (%s, %s)"
    return (
        ReadQuerySpec(
            name="news_feed_events",
            sql="""
                SELECT e.event_id, e.leader_title, e.opened_at_ms, e.admission, e.queue_priority, t.final_decision
                  FROM news_current_events_v1 e
                  JOIN news_items i ON i.item_id = e.leader_item_id
                  LEFT JOIN LATERAL (
                    SELECT final_decision FROM news_verdicts v
                     WHERE v.event_id = e.event_id AND v.stage = 'triage'
                       AND v.judgment_contract_version = 'news_judgment_v2'
                     ORDER BY v.created_at_ms DESC LIMIT 1
                  ) t ON true
                 WHERE e.ingest_mode IN ('live', 'recovery')
                 ORDER BY e.opened_at_ms DESC, e.event_id DESC
                 LIMIT 51
            """,
        ),
        ReadQuerySpec(
            name="news_search_identity",
            sql="""
                SELECT base_symbol, priority
                  FROM (
                    SELECT base_symbol, 0 AS priority FROM news_symbol_aliases WHERE alias = %s
                    UNION ALL
                    SELECT DISTINCT base_symbol, 1 AS priority FROM news_symbol_aliases WHERE base_symbol = %s
                    UNION ALL
                    SELECT DISTINCT base_symbol, 1 AS priority FROM news_market_instruments
                     WHERE status = 'trading' AND base_symbol = %s
                    UNION ALL
                    SELECT DISTINCT base_symbol, 2 AS priority FROM news_market_instruments
                     WHERE status = 'trading' AND venue_symbol = %s
                  ) matches
                 ORDER BY priority, base_symbol
            """,
            params=("BTC", "BTC", "BTC", "BTC"),
        ),
        ReadQuerySpec(
            name="news_search_event_symbols",
            sql="SELECT alias FROM news_symbol_aliases WHERE base_symbol = %s ORDER BY alias",
            params=("BTC",),
        ),
        ReadQuerySpec(
            name="news_feed_asset_search",
            # This is the fresh-search page the console actually requests: a bounded 168 h scope, the exact
            # AssetSearch predicate and the production verdict/delivery joins. The builder is shared with
            # FeedStorage so this audit cannot regress to a simplified look-alike query.
            sql=feed_page_sql(f"{search_base} AND {ASSET_SEARCH_PREDICATE}"),
            params=(week_ago, ["BTC"], 51),
        ),
        ReadQuerySpec(
            name="news_feed_asset_search_counts",
            sql=feed_counts_sql(f"{search_base} AND {ASSET_SEARCH_PREDICATE}"),
            params=(week_ago, ["BTC"]),
            amplification_basis="aggregate_input",
        ),
        ReadQuerySpec(
            name="news_feed_asset_search_cursor",
            sql=feed_page_sql(f"{search_base} AND {ASSET_SEARCH_PREDICATE} AND {search_cursor}"),
            params=(week_ago, ["BTC"], int(now_ms), "\uffff", 51),
        ),
        ReadQuerySpec(
            name="news_feed_text_search",
            sql=feed_page_sql(f"{search_base} AND {TEXT_SEARCH_PREDICATE}"),
            params=(week_ago, "bitcoin", 51),
        ),
        ReadQuerySpec(
            name="news_feed_text_search_counts",
            sql=feed_counts_sql(f"{search_base} AND {TEXT_SEARCH_PREDICATE}"),
            params=(week_ago, "bitcoin"),
            amplification_basis="aggregate_input",
        ),
        ReadQuerySpec(
            name="news_feed_text_search_cursor",
            sql=feed_page_sql(f"{search_base} AND {TEXT_SEARCH_PREDICATE} AND {search_cursor}"),
            params=(week_ago, "bitcoin", int(now_ms), "\uffff", 51),
        ),
        ReadQuerySpec(
            name="news_event_detail",
            sql=(
                "SELECT e.*, i.description FROM news_current_events_v1 e"
                " JOIN news_items i ON i.item_id = e.leader_item_id WHERE e.event_id = %s"
            ),
            params=("event",),
        ),
        ReadQuerySpec(
            name="news_event_asset_projection",
            sql="SELECT asset.event_id, array_agg(asset.symbol ORDER BY asset.symbol) AS symbols"
            " FROM news_event_assets asset"
            " JOIN news_current_events_v1 event ON event.event_id = asset.event_id"
            " WHERE asset.event_id = ANY(%s) GROUP BY asset.event_id",
            params=(["event"],),
        ),
        ReadQuerySpec(
            name="news_event_members",
            sql="""
                SELECT m.item_id, m.match_kind, i.title FROM news_event_members m
                  JOIN news_current_events_v1 event ON event.event_id = m.event_id
                  JOIN news_items i ON i.item_id = m.item_id
                 WHERE m.event_id = %s ORDER BY m.joined_at_ms, m.item_id
            """,
            params=("event",),
        ),
        ReadQuerySpec(
            name="news_event_verdicts",
            sql="SELECT * FROM news_verdicts WHERE event_id = %s "
            "AND judgment_contract_version = 'news_judgment_v2' ORDER BY created_at_ms",
            params=("event",),
        ),
        ReadQuerySpec(
            name="news_storyline_status",
            sql="""
                SELECT count(*) AS pushed
                  FROM news_verdicts v JOIN news_current_events_v1 e ON e.event_id = v.event_id
                 WHERE v.stage = 'triage' AND v.final_decision IN ('push', 'escalate')
                   AND v.judgment_contract_version = 'news_judgment_v2'
                   AND e.storyline_key = %s AND v.created_at_ms >= %s
            """,
            params=("theme:rates", day_ago),
        ),
        ReadQuerySpec(
            name="news_band_lookup",
            sql="""
                SELECT DISTINCT b.event_id FROM news_event_bands b
                  JOIN unnest(%s::smallint[], %s::text[]) AS q(band_index, band_key)
                    ON q.band_index = b.band_index AND q.band_key = b.band_key
                 WHERE b.dedupe_family = %s AND b.expires_at_ms > %s
            """,
            params=([0, 1], ["a", "b"], "general", now_ms),
        ),
        ReadQuerySpec(
            name="news_status_ingest",
            sql="SELECT * FROM news_ingest_state WHERE singleton_key = 'opennews'",
        ),
        ReadQuerySpec(
            name="news_status_incidents_open",
            sql=(
                "SELECT incident_id, cause_class, opened_at_ms FROM news_opennews_incidents"
                " WHERE closed_at_ms IS NULL ORDER BY incident_id"
            ),
        ),
        ReadQuerySpec(
            name="news_status_pipeline_24h",
            sql="SELECT count(*) AS n FROM news_verdicts WHERE stage = 'triage' "
            "AND judgment_contract_version = 'news_judgment_v2' AND created_at_ms >= %s",
            params=(day_ago,),
        ),
        ReadQuerySpec(
            name="news_status_delivery_1h",
            sql="SELECT count(*) AS n FROM news_deliveries delivery"
            " JOIN news_current_events_v1 event ON event.event_id = delivery.event_id"
            " WHERE delivery.state = 'sent' AND delivery.settled_at_ms >= %s",
            params=(hour_ago,),
        ),
        ReadQuerySpec(
            name="news_status_learning_retention",
            sql="SELECT * FROM news_learning_retention_state WHERE singleton",
        ),
        # #88 price plane. The due scan and the review aggregates are the two reads that could grow without
        # anyone noticing, so both are in the EXPLAIN registry with their real predicates.
        # #179: the eligible-rank count runs on every parsed telemetry frame.
        ReadQuerySpec(
            name="news_signal_history",
            sql="SELECT count(*)::int AS n FROM news_oi_signals signal "
            "JOIN news_current_events_v1 event ON event.event_id = signal.event_id "
            "WHERE signal.metric_version = 'oi_signal_v1' AND signal.symbol = 'BTC' "
            "AND signal.observed_at_ms > 0 AND signal.observed_at_ms < 1 AND signal.event_id <> '' "
            "AND signal.whale_oi_ratio_bps > 8000 AND abs(signal.oi_change_bps) >= 0",
        ),
        ReadQuerySpec(
            name="news_quote_snapshot_read",
            sql="SELECT source_key, quotes, received_at_ms FROM news_quote_snapshots",
        ),
        # #207 PR-W1 token page identity. Both are one indexed base lookup, but every asset chip on the
        # console is now a link into them, so they are in the registry with their real predicates.
        ReadQuerySpec(
            name="news_symbol_contracts",
            sql="SELECT venue, venue_symbol, instrument_class, quote_asset FROM news_market_instruments"
            " WHERE base_symbol = %s AND status = 'trading' ORDER BY venue, venue_symbol LIMIT 24",
            params=("BTC",),
        ),
        ReadQuerySpec(
            name="news_symbol_tradeable",
            sql="SELECT 1 FROM news_market_instruments WHERE base_symbol = %s AND status = 'trading'"
            " AND NOT (venue = ANY(%s)) LIMIT 1",
            params=("BTC", ["us.listed"]),
        ),
        ReadQuerySpec(
            name="news_symbol_aliases",
            sql="SELECT alias, base_symbol, source FROM news_symbol_aliases"
            " WHERE base_symbol = ANY(%s) AND source = ANY(%s) ORDER BY base_symbol, alias",
            params=(["BTC"], ["seed"]),
        ),
        ReadQuerySpec(
            name="news_reaction_due_scan",
            sql="""
                SELECT a.event_id, a.symbol, a.opened_at_ms
                  FROM news_event_assets a
                  JOIN news_current_events_v1 e ON e.event_id = a.event_id AND e.ingest_mode = 'live'
                  LEFT JOIN news_event_reactions r
                    ON r.event_id = a.event_id AND r.symbol = a.symbol AND r.metric_version = %s
                 WHERE a.opened_at_ms <= %s
                   AND (r.state IS NULL OR r.state IN ('pending', 'partial'))
                 ORDER BY a.opened_at_ms
                 LIMIT 100
            """,
            params=(REACTION_METRIC_VERSION, hour_ago),
        ),
        ReadQuerySpec(
            name="news_reaction_attach",
            sql=(
                "SELECT reaction.event_id, reaction.symbol, reaction.return_1h_bps, reaction.return_4h_bps,"
                " reaction.state FROM news_event_reactions reaction"
                " JOIN news_current_events_v1 event ON event.event_id = reaction.event_id"
                " WHERE reaction.event_id = ANY(%s) AND reaction.metric_version = %s"
            ),
            params=(["event"], REACTION_METRIC_VERSION),
        ),
        *(
            ReadQuerySpec(name=statement.name, sql=statement.sql, params=statement.params)
            for statement in review_read_statements(now_ms=int(now_ms))
        ),
    )


__all__ = ["news_query_specs"]
