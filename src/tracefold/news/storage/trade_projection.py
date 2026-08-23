"""News-owned point-in-time projection read by App composition for Trading."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..opennews import source_artifact_identity


class TradeProjectionStorage:
    conn: Any

    def trade_candidate_oi_rows(
        self,
        *,
        metric_version: str,
        after_created_at_ms: int,
        until_created_at_ms: int,
        max_rank_in_window: int,
        min_oi_value_usd: int,
        limit: int = 64,
    ) -> list[dict[str, Any]]:
        """Deterministic telemetry verdicts that pushed, with their rank-ledger row and the frame's venue.

        `venue` comes from the Item's own `provider_metadata.source` rather than the ledger, which does
        not store it. That field is the single strongest discriminator the OI research measured
        (Hyperliquid +1.35% vs Binance -0.26% at 4 h), so a projection that dropped it would leave the
        trading lane unable to test its best-supported hypothesis.
        """

        rows = self.conn.execute(
            """
            SELECT v.event_id,
                   v.created_at_ms          AS verdict_created_at_ms,
                   v.final_decision,
                   epoch.epoch_id           AS learning_epoch,
                   v.program_version,
                   v.program_sha256,
                   v.policy_version,
                   v.editorial ->> 'editorial_origin' AS editorial_origin,
                   v.editorial ->> 'editorial_sha256' AS editorial_sha256,
                   v.scored_judgment_sha256,
                   v.runtime_manifest_sha,
                   s.metric_version,
                   s.symbol,
                   s.direction,
                   s.oi_change_bps,
                   s.oi_value_usd,
                   s.whale_long_profit_bps,
                   s.whale_oi_ratio_bps,
                   s.rank_in_window,
                   s.observed_at_ms,
                   e.ingest_mode,
                   i.provider_metadata ->> 'source' AS venue
              FROM news_verdicts v
              JOIN news_oi_signals s
                ON s.event_id = v.event_id AND s.metric_version = %s
              JOIN news_events e ON e.event_id = v.event_id
              JOIN news_learning_epochs epoch
                ON epoch.epoch_id = 'program_v6'
               AND e.opened_at_ms >= epoch.starts_at_ms
               AND v.created_at_ms >= epoch.starts_at_ms
              LEFT JOIN news_items i ON i.item_id = e.leader_item_id
             WHERE v.stage = 'triage'
               AND v.program_version = 'news_oi_signal_v1'
               AND v.policy_version = 'news_triage_policy_v10'
               AND v.editorial ->> 'editorial_origin' = 'telemetry_deterministic'
               AND jsonb_typeof(v.editorial -> 'relevance') = 'null'
               AND v.program_sha256 ~ '^[0-9a-f]{64}$'
               AND v.editorial ->> 'editorial_sha256' ~ '^[0-9a-f]{64}$'
               AND v.scored_judgment_sha256 IS NOT NULL
               AND v.runtime_manifest_sha IS NOT NULL
               AND v.final_decision IN ('push', 'escalate')
               AND v.degraded = false
               AND e.ingest_mode = 'live'
               AND v.created_at_ms > %s
               AND v.created_at_ms <= %s
               AND s.rank_in_window <= %s
               AND s.oi_value_usd >= %s
             ORDER BY v.created_at_ms, v.event_id
             LIMIT %s
            """,
            (
                metric_version,
                int(after_created_at_ms),
                int(until_created_at_ms),
                int(max_rank_in_window),
                int(min_oi_value_usd),
                int(limit),
            ),
        ).fetchall()
        return [dict(row) for row in rows]

    def trade_candidate_news_rows(
        self,
        *,
        after_created_at_ms: int,
        until_created_at_ms: int,
        limit: int = 64,
    ) -> list[dict[str, Any]]:
        """Model Triage verdicts that pushed on a crypto-class Event, frozen at the verdict cutoff.

        Only the structural conditions live in SQL. Single-primary, grounding, novelty and magnitude are
        the trading lane's own eligibility rules and stay pure functions over the verdict document, so
        they are testable without a database and cannot silently diverge from the funnel report.
        """

        rows = self.conn.execute(
            """
            SELECT v.event_id,
                   v.created_at_ms  AS verdict_created_at_ms,
                   v.final_decision,
                   v.evidence_version,
                   v.evidence_sha256,
                   v.focus_fact_id,
                   v.verdict,
                   epoch.epoch_id AS learning_epoch,
                   v.program_version,
                   v.program_sha256,
                   v.policy_version,
                   v.editorial ->> 'editorial_origin' AS editorial_origin,
                   v.editorial ->> 'editorial_sha256' AS editorial_sha256,
                   v.scored_judgment_sha256,
                   v.runtime_manifest_sha,
                   e.opened_at_ms,
                   e.comparison_fingerprint,
                   e.asset_class,
                   e.grounded_assets,
                   e.ingest_mode,
                   i.source_artifact_id,
                   i.canonical_url
              FROM news_verdicts v
              JOIN news_events e ON e.event_id = v.event_id
              JOIN news_learning_epochs epoch
                ON epoch.epoch_id = 'program_v6'
               AND e.opened_at_ms >= epoch.starts_at_ms
               AND v.created_at_ms >= epoch.starts_at_ms
              LEFT JOIN news_items i ON i.item_id = e.leader_item_id
             WHERE v.stage = 'triage'
               AND v.program_version = 'news_semantic_program_v4'
               AND v.policy_version = 'news_triage_policy_v10'
               AND v.editorial ->> 'editorial_origin' = 'model'
               AND jsonb_typeof(v.editorial -> 'relevance') = 'object'
               AND v.program_sha256 ~ '^[0-9a-f]{64}$'
               AND v.editorial ->> 'editorial_sha256' ~ '^[0-9a-f]{64}$'
               AND v.scored_judgment_sha256 IS NOT NULL
               AND v.runtime_manifest_sha IS NOT NULL
               AND v.final_decision IN ('push', 'escalate')
               AND v.degraded = false
               AND e.ingest_mode = 'live'
               AND e.asset_class = 'crypto'
               AND v.created_at_ms > %s
               AND v.created_at_ms <= %s
             ORDER BY v.created_at_ms, v.event_id
             LIMIT %s
            """,
            (int(after_created_at_ms), int(until_created_at_ms), int(limit)),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            record = dict(row)
            # #154/#157 keep the artifact id as a column and derive its publication time from the URL's
            # own identity (a snowflake, for x.com), so the projection derives it the same way the
            # delivery card's `source_age_s` does rather than inventing a second answer.
            _, published_at_ms = source_artifact_identity(str(record.pop("canonical_url", "") or ""))
            record["source_published_at_ms"] = published_at_ms
            out.append(record)
        return out

    def trade_candidate_instrument(self, *, base_symbol: str, venues: Sequence[str]) -> list[dict[str, Any]]:
        """Exactly-listed native crypto perpetuals for one underlying, in the caller's venue order.

        `instrument_class = 'crypto'` is not decoration: Binance labels its 169 TradFi perps `EQUITY`
        and friends, so a `WMT` Event whose Gate class says crypto still resolves to nothing here.
        HIP-3 builder venues (`hl.xyz`) are excluded by naming the two native perp venues explicitly.
        """

        if not venues:
            return []
        rows = self.conn.execute(
            """
            SELECT venue, venue_symbol, base_symbol, instrument_class, quote_asset, status, last_seen_ms
              FROM news_market_instruments
             WHERE base_symbol = %s
               AND venue = ANY(%s)
               AND status = 'trading'
               AND instrument_class = 'crypto'
             -- Deterministic, because the caller freezes the first row per venue into an immutable
             -- payload. `binance.perp` is snapshotted without a quote filter, so DOGEUSDT, DOGEUSDC and
             -- any dated contract all match; unspecified row order would let two identical manifests
             -- resolve to different books and break "replayable from the case row alone".
             ORDER BY venue,
                      CASE quote_asset WHEN 'USDT' THEN 0 WHEN 'USDC' THEN 1 ELSE 2 END,
                      length(venue_symbol),
                      venue_symbol
            """,
            (str(base_symbol or "").strip().upper(), list(venues)),
        ).fetchall()
        return [dict(row) for row in rows]
