"""Bound News V3 read statements for the PostgreSQL query audit (EXPLAIN coverage)."""

from __future__ import annotations

from tracefold.platform.postgres.audit import ReadQuerySpec

from ..market_review.pricing import REACTION_METRIC_VERSION
from ..review.desk import review_read_statements
from ..source_contracts import MARKET_KINDS
from .decisions import UNPUBLISHED_VERDICT_CANDIDATES_SQL
from .events import UNPUBLISHED_EVENT_CANDIDATES_SQL
from .feed_sql import (
    ASSET_SEARCH_PREDICATE,
    CURRENT_EVENT_CARD_SQL,
    EVENT_VERDICTS_SQL,
    STATUS_INGEST_SQL,
    STATUS_LEARNING_RETENTION_SQL,
    TEXT_SEARCH_PREDICATE,
    feed_counts_sql,
    feed_page_sql,
)
from .market import (
    MARKET_GROUPS_SQL,
    MARKET_ITEM_SQL,
    MARKET_SOURCES_SQL,
    MARKET_TIMELINE_SQL,
)
from .operations import (
    RAW_RETENTION_CANDIDATE_SQL,
    RECOVERY_BACKLOG_LIMIT,
    pending_recovery_incidents_statement,
)


def news_query_specs(*, now_ms: int) -> tuple[ReadQuerySpec, ...]:
    day_ago = int(now_ms) - 24 * 3600_000
    hour_ago = int(now_ms) - 3600_000
    week_ago = int(now_ms) - 168 * 3600_000
    raw_cutoff = int(now_ms) - 30 * 24 * 3600_000
    judged_cutoff = int(now_ms) - 365 * 24 * 3600_000
    search_base = "e.ingest_mode IN ('live', 'recovery') AND e.opened_at_ms >= %s"
    search_cursor = "(e.opened_at_ms, e.event_id) < (%s, %s)"
    recovery_backlog_sql, recovery_backlog_params = pending_recovery_incidents_statement(limit=RECOVERY_BACKLOG_LIMIT)
    return (
        ReadQuerySpec(
            name="news_feed_events",
            sql=feed_page_sql("e.ingest_mode IN ('live', 'recovery')"),
            params=(now_ms, 51),
            max_read_return_amplification=32.0,
        ),
        ReadQuerySpec(
            name="news_event_handoff_candidates",
            sql=UNPUBLISHED_EVENT_CANDIDATES_SQL,
            params=(int(now_ms) - 15_000, day_ago, 50),
            max_read_return_amplification=20.0,
        ),
        ReadQuerySpec(
            name="news_verdict_handoff_candidates",
            sql=UNPUBLISHED_VERDICT_CANDIDATES_SQL,
            params=(int(now_ms) - 15_000, day_ago, 50),
            max_read_return_amplification=20.0,
        ),
        ReadQuerySpec(
            name="news_raw_retention_candidates",
            sql=RAW_RETENTION_CANDIDATE_SQL,
            params=(raw_cutoff, judged_cutoff, judged_cutoff, judged_cutoff, judged_cutoff, judged_cutoff, 500),
            max_read_return_amplification=20.0,
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
            max_read_return_amplification=8.0,
        ),
        ReadQuerySpec(
            name="news_search_event_symbols",
            sql="SELECT alias FROM news_symbol_aliases WHERE base_symbol = %s ORDER BY alias",
            params=("BTC",),
            max_read_return_amplification=8.0,
        ),
        ReadQuerySpec(
            name="news_feed_asset_search",
            # This is the fresh-search page the console actually requests: a bounded 168 h scope, the exact
            # AssetSearch predicate and the production verdict/delivery joins. The builder is shared with
            # FeedStorage so this audit cannot regress to a simplified look-alike query.
            sql=feed_page_sql(f"{search_base} AND {ASSET_SEARCH_PREDICATE}"),
            params=(now_ms, week_ago, ["BTC"], 51),
            max_read_return_amplification=32.0,
        ),
        ReadQuerySpec(
            name="news_feed_asset_search_counts",
            sql=feed_counts_sql(f"{search_base} AND {ASSET_SEARCH_PREDICATE}"),
            params=(now_ms, week_ago, ["BTC"]),
            amplification_basis="aggregate_input",
            max_read_return_amplification=2.0,
        ),
        ReadQuerySpec(
            name="news_feed_asset_search_cursor",
            sql=feed_page_sql(f"{search_base} AND {ASSET_SEARCH_PREDICATE} AND {search_cursor}"),
            params=(now_ms, week_ago, ["BTC"], int(now_ms), "\uffff", 51),
            max_read_return_amplification=32.0,
        ),
        ReadQuerySpec(
            name="news_feed_text_search",
            sql=feed_page_sql(f"{search_base} AND {TEXT_SEARCH_PREDICATE}"),
            params=(now_ms, week_ago, "bitcoin", 51),
            max_read_return_amplification=32.0,
        ),
        ReadQuerySpec(
            name="news_feed_text_search_counts",
            sql=feed_counts_sql(f"{search_base} AND {TEXT_SEARCH_PREDICATE}"),
            params=(now_ms, week_ago, "bitcoin"),
            amplification_basis="aggregate_input",
            max_read_return_amplification=2.0,
        ),
        ReadQuerySpec(
            name="news_feed_text_search_cursor",
            sql=feed_page_sql(f"{search_base} AND {TEXT_SEARCH_PREDICATE} AND {search_cursor}"),
            params=(now_ms, week_ago, "bitcoin", int(now_ms), "\uffff", 51),
            max_read_return_amplification=32.0,
        ),
        ReadQuerySpec(
            name="news_event_detail",
            sql=CURRENT_EVENT_CARD_SQL,
            params=("event",),
            max_read_return_amplification=8.0,
        ),
        ReadQuerySpec(
            name="news_event_asset_projection",
            sql="SELECT asset.event_id, array_agg(asset.symbol ORDER BY asset.symbol) AS symbols"
            " FROM news_event_assets asset"
            " JOIN news_events event ON event.event_id = asset.event_id"
            " WHERE asset.event_id = ANY(%s) GROUP BY asset.event_id",
            params=(["event"],),
            max_read_return_amplification=8.0,
        ),
        ReadQuerySpec(
            name="news_event_members",
            sql="""
                SELECT m.item_id, m.match_kind, i.title FROM news_event_members m
                  JOIN news_events event ON event.event_id = m.event_id
                  JOIN news_items i ON i.item_id = m.item_id
                 WHERE m.event_id = %s ORDER BY m.joined_at_ms, m.item_id
            """,
            params=("event",),
            max_read_return_amplification=8.0,
        ),
        ReadQuerySpec(
            name="news_event_verdicts",
            sql=EVENT_VERDICTS_SQL,
            params=("event",),
            max_read_return_amplification=8.0,
        ),
        ReadQuerySpec(
            name="news_storyline_status",
            sql="""
                SELECT count(*) AS pushed
                  FROM news_verdicts v JOIN news_events e ON e.event_id = v.event_id
                 WHERE v.stage = 'triage' AND v.final_decision IN ('push', 'escalate')
                   AND v.judgment_contract_version = 'news_judgment_v2'
                   AND e.storyline_key = %s AND v.created_at_ms >= %s
            """,
            params=("topic:rates", day_ago),
            max_read_return_amplification=20.0,
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
            max_read_return_amplification=20.0,
        ),
        ReadQuerySpec(
            name="news_status_ingest",
            sql=STATUS_INGEST_SQL,
            max_read_return_amplification=4.0,
        ),
        ReadQuerySpec(
            name="news_status_incidents_open",
            sql=(
                "SELECT incident_id, cause_class, opened_at_ms FROM news_opennews_incidents"
                " WHERE closed_at_ms IS NULL ORDER BY incident_id"
            ),
            max_read_return_amplification=20.0,
        ),
        ReadQuerySpec(
            name="news_status_recovery_backlog",
            sql=recovery_backlog_sql,
            params=recovery_backlog_params,
            max_read_return_amplification=20.0,
        ),
        ReadQuerySpec(
            name="news_status_pipeline_24h",
            sql="SELECT count(*) AS n FROM news_verdicts WHERE stage = 'triage' "
            "AND judgment_contract_version = 'news_judgment_v2' AND created_at_ms >= %s",
            params=(day_ago,),
            max_read_return_amplification=20.0,
        ),
        ReadQuerySpec(
            name="news_status_delivery_1h",
            sql="SELECT count(*) AS n FROM news_deliveries delivery"
            " JOIN news_events event ON event.event_id = delivery.event_id"
            " WHERE delivery.state = 'sent' AND delivery.settled_at_ms >= %s",
            params=(hour_ago,),
            max_read_return_amplification=20.0,
        ),
        ReadQuerySpec(
            name="news_status_learning_retention",
            sql=STATUS_LEARNING_RETENTION_SQL,
            max_read_return_amplification=4.0,
        ),
        # #88 price plane. The due scan and the review aggregates are the two reads that could grow without
        # anyone noticing, so both are in the EXPLAIN registry with their real predicates.
        # #553. The market list is the one public read whose scan is deliberately wider than its page:
        # collapsing consecutive observations is a property of the whole window, so the window is read
        # and the collapsed groups are paged out of it. Both bounds are in the statement itself.
        ReadQuerySpec(
            name="news_market_groups",
            sql=MARKET_GROUPS_SQL,
            params=(list(MARKET_KINDS), week_ago, now_ms, now_ms, "", 51),
            max_read_return_amplification=100.0,
        ),
        ReadQuerySpec(
            name="news_market_sources",
            sql=MARKET_SOURCES_SQL,
            params=(week_ago, now_ms),
            max_read_return_amplification=100.0,
        ),
        ReadQuerySpec(
            name="news_market_item",
            sql=MARKET_ITEM_SQL,
            params=("0" * 64,),
            max_read_return_amplification=4.0,
        ),
        ReadQuerySpec(
            name="news_market_group_timeline",
            sql=MARKET_TIMELINE_SQL,
            params=("oi|opennews||BTC|oi_signal_v1|opennews_oi_source_v1|300000",),
            max_read_return_amplification=20.0,
        ),
        ReadQuerySpec(
            name="news_quote_snapshot_read",
            sql="SELECT source_key, quotes, received_at_ms FROM news_quote_snapshots",
            max_read_return_amplification=4.0,
        ),
        # #207 PR-W1 token page identity. Both are one indexed base lookup, but every asset chip on the
        # console is now a link into them, so they are in the registry with their real predicates.
        ReadQuerySpec(
            name="news_symbol_contracts",
            sql="SELECT venue, venue_symbol, instrument_class, quote_asset FROM news_market_instruments"
            " WHERE base_symbol = %s AND status = 'trading' ORDER BY venue, venue_symbol LIMIT 24",
            params=("BTC",),
            max_read_return_amplification=8.0,
        ),
        ReadQuerySpec(
            name="news_symbol_tradeable",
            sql="SELECT 1 FROM news_market_instruments WHERE base_symbol = %s AND status = 'trading'"
            " AND NOT (venue = ANY(%s)) LIMIT 1",
            params=("BTC", ["us.listed"]),
            max_read_return_amplification=8.0,
        ),
        ReadQuerySpec(
            name="news_symbol_aliases",
            sql="SELECT alias, base_symbol, source FROM news_symbol_aliases"
            " WHERE base_symbol = ANY(%s) AND source = ANY(%s) ORDER BY base_symbol, alias",
            params=(["BTC"], ["seed"]),
            max_read_return_amplification=8.0,
        ),
        ReadQuerySpec(
            name="news_reaction_due_scan",
            sql="""
                SELECT a.event_id, a.symbol, a.opened_at_ms
                  FROM news_event_assets a
                  JOIN news_events e ON e.event_id = a.event_id AND e.ingest_mode = 'live'
                  LEFT JOIN news_event_reactions r
                    ON r.event_id = a.event_id AND r.symbol = a.symbol AND r.metric_version = %s
                 WHERE a.opened_at_ms <= %s
                   AND (r.state IS NULL OR r.state IN ('pending', 'partial'))
                 ORDER BY a.opened_at_ms
                 LIMIT 100
            """,
            params=(REACTION_METRIC_VERSION, hour_ago),
            max_read_return_amplification=20.0,
        ),
        ReadQuerySpec(
            name="news_reaction_attach",
            sql=(
                "SELECT reaction.event_id, reaction.symbol, reaction.return_1h_bps, reaction.return_4h_bps,"
                " reaction.state FROM news_event_reactions reaction"
                " JOIN news_events event ON event.event_id = reaction.event_id"
                " WHERE reaction.event_id = ANY(%s) AND reaction.metric_version = %s"
            ),
            params=(["event"], REACTION_METRIC_VERSION),
            max_read_return_amplification=8.0,
        ),
        *(
            ReadQuerySpec(
                name=statement.name,
                sql=statement.sql,
                params=statement.params,
                max_read_return_amplification=20.0,
            )
            for statement in review_read_statements(now_ms=int(now_ms))
        ),
    )


__all__ = ["news_query_specs"]
