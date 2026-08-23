"""Bound News V3 read statements for the PostgreSQL query audit (EXPLAIN coverage)."""

from __future__ import annotations

from tracefold.platform.postgres.postgres_audit import ReadQuerySpec

from .market_review.pricing import REACTION_METRIC_VERSION
from .review import review_read_statements


def news_query_specs(*, now_ms: int) -> tuple[ReadQuerySpec, ...]:
    day_ago = int(now_ms) - 24 * 3600_000
    hour_ago = int(now_ms) - 3600_000
    return (
        ReadQuerySpec(
            name="news_feed_events",
            sql="""
                SELECT e.event_id, e.leader_title, e.opened_at_ms, e.admission, e.queue_priority, t.final_decision
                  FROM news_events e
                  JOIN news_items i ON i.item_id = e.leader_item_id
                  LEFT JOIN LATERAL (
                    SELECT final_decision FROM news_verdicts v
                     WHERE v.event_id = e.event_id AND v.stage = 'triage'
                     ORDER BY v.created_at_ms DESC LIMIT 1
                  ) t ON true
                 WHERE e.ingest_mode IN ('live', 'recovery')
                 ORDER BY e.opened_at_ms DESC, e.event_id DESC
                 LIMIT 51
            """,
        ),
        ReadQuerySpec(
            name="news_feed_symbol_filter",
            sql="""
                SELECT e.event_id FROM news_events e
                 WHERE EXISTS (SELECT 1 FROM news_event_assets a WHERE a.event_id = e.event_id AND a.symbol = %s)
                 ORDER BY e.opened_at_ms DESC LIMIT 51
            """,
            params=("BTC",),
        ),
        ReadQuerySpec(
            name="news_feed_search",
            sql="""
                SELECT e.event_id FROM news_events e
                 WHERE e.search_doc @@ plainto_tsquery('simple', %s)
                 ORDER BY e.opened_at_ms DESC LIMIT 51
            """,
            params=("bitcoin",),
        ),
        ReadQuerySpec(
            name="news_event_detail",
            sql=(
                "SELECT e.*, i.description FROM news_events e"
                " JOIN news_items i ON i.item_id = e.leader_item_id WHERE e.event_id = %s"
            ),
            params=("event",),
        ),
        ReadQuerySpec(
            name="news_event_members",
            sql="""
                SELECT m.item_id, m.match_kind, i.title FROM news_event_members m
                  JOIN news_items i ON i.item_id = m.item_id WHERE m.event_id = %s ORDER BY m.joined_at_ms, m.item_id
            """,
            params=("event",),
        ),
        ReadQuerySpec(
            name="news_event_verdicts",
            sql="SELECT * FROM news_verdicts WHERE event_id = %s ORDER BY created_at_ms",
            params=("event",),
        ),
        ReadQuerySpec(
            name="news_storyline_status",
            sql="""
                SELECT count(*) AS pushed FROM news_verdicts v JOIN news_events e ON e.event_id = v.event_id
                 WHERE v.stage = 'triage' AND v.final_decision IN ('push', 'escalate')
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
                 WHERE b.family = %s AND b.expires_at_ms > %s
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
            sql="SELECT count(*) AS n FROM news_verdicts WHERE stage = 'triage' AND created_at_ms >= %s",
            params=(day_ago,),
        ),
        ReadQuerySpec(
            name="news_status_delivery_1h",
            sql="SELECT count(*) AS n FROM news_deliveries WHERE state = 'sent' AND settled_at_ms >= %s",
            params=(hour_ago,),
        ),
        ReadQuerySpec(
            name="news_status_learning_retention",
            sql="SELECT * FROM news_learning_retention_state WHERE singleton",
        ),
        # #88 price plane. The due scan and the review aggregates are the two reads that could grow without
        # anyone noticing, so both are in the EXPLAIN registry with their real predicates.
        # #137: the rank read runs on every telemetry frame.
        ReadQuerySpec(
            name="news_signal_history",
            sql="SELECT observed_at_ms FROM news_oi_signals "
            "WHERE metric_version = 'oi_signal_v1' AND symbol = 'BTC' "
            "AND observed_at_ms > 0 AND observed_at_ms < 1 AND event_id <> '' "
            "ORDER BY observed_at_ms DESC LIMIT 64",
        ),
        ReadQuerySpec(
            name="news_quote_snapshot_read",
            sql="SELECT source_key, quotes, received_at_ms FROM news_quote_snapshots",
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
        ),
        ReadQuerySpec(
            name="news_reaction_attach",
            sql=(
                "SELECT event_id, symbol, return_1h_bps, return_4h_bps, state FROM news_event_reactions"
                " WHERE event_id = ANY(%s) AND metric_version = %s"
            ),
            params=(["event"], REACTION_METRIC_VERSION),
        ),
        *(
            ReadQuerySpec(name=statement.name, sql=statement.sql, params=statement.params)
            for statement in review_read_statements(now_ms=int(now_ms))
        ),
    )


__all__ = ["news_query_specs"]
