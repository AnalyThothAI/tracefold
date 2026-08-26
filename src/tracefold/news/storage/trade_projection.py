"""News-owned point-in-time projection read by App composition for Trading.

The three row contracts below are the published shape of that projection. They are `TypedDict`s
because the rows *are* the SELECT lists — naming the columns is the whole contract, and a runtime
model here would coerce values PostgreSQL already typed and turn a nullable LEFT JOIN column into a
different value. Nothing outside this module may add, rename or retype a key without editing them,
which is what makes the App-side mapper break at type-check time instead of at 03:00 in a runner.

They say nothing about *trade* eligibility: single-primary, grounding, novelty, magnitude, freshness
and rank live in the trading lane's own pure rules. This side owns generation identity, the
point-in-time boundaries and the deterministic order.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any, TypedDict

from ..opennews import source_artifact_identity

# Bump when a key is added, removed or retyped below — and when what a key *means* changes without the
# key moving. The consumer's mapper is versioned against it, so neither a silently widened projection
# nor a silently re-scoped one can reach Trading unnoticed.
#
# v2 (#211): the two candidate reads return the *newest* rows in the window rather than the oldest.
# Trading's scan horizon grew from `max_age x 3` to `max_age + max(lookback)` so a legal counterpart is
# actually visible, and with an ascending `LIMIT` a busy hour of that wider window would have been
# answered entirely with its oldest rows — spending the whole budget on context and returning none of
# the fresh triggers the scan exists to find.
#
# v6 (#264): the OI read stops being "the OI verdicts the reader pushed" and becomes "the OI facts this
# generation parsed and persisted". `final_decision` and the deterministic rule are still selected — a
# capital decision must be traceable to the judgment beside it — but they no longer decide *visibility*,
# and the two Trading thresholds the SELECT used to execute (`max_rank_in_window`, `min_oi_value_usd`)
# are gone from the signature. Measured over the seven days this table has existed, the reader's own
# `whale_oi_ratio > 80%` push rule admitted 2 of the 7 frames that meet the target strategy's
# conditions; the other 5 — TUT 15.48%/54.24% among them — were `drop` and never reached Trading at all.
#
# v7 (#265): the OI read publishes what the provider proves about *how* the frame was measured —
# `source_strategy_id`, `source_contract_version`, `measurement_window_ms` — beside the four numbers it
# measured. All three are nullable together, and `NULL` is the contract: it means the interval could not
# be proven for this frame and a consumer must refuse it rather than assume five minutes.
NEWS_TRADE_PROJECTION_VERSION = "news_trade_projection_v7"

# One read's ceiling per lane. The consumer's widest configured horizon is `max_age + max(lookback)` —
# 65 minutes at the shipped configuration — and the measured live rate through these exact predicates
# is about eleven rows an hour on the News lane and nothing like a full lane on the OI one, so this is
# roughly twenty times the volume it has to carry. It is still a ceiling, not a promise: a lane that
# comes back with exactly this many rows was truncated, and the funnel's `oi_rows` / `news_rows`
# counters are where that shows.
TRADE_PROJECTION_ROW_LIMIT = 256


class OiTradeProjectionRow(TypedDict):
    """One parsed deterministic OI telemetry fact, with its rank-ledger row and the frame's venue.

    `final_decision` and `source_rule` are the reader's own judgment of the same frame. They are here so
    a capital decision can be traced to the verdict beside it, and for no other purpose: since #264 the
    reader's push/drop no longer decides whether Trading can see the fact.
    """

    event_id: str
    verdict_created_at_ms: int
    final_decision: str
    # `evaluate_oi`'s named rule, from the deterministic judge's own trace. Nullable: a trace written by
    # an older judge, or one that failed to record the member, must read as absent rather than as a rule.
    source_rule: str | None
    learning_epoch: str
    program_version: str
    program_sha256: str
    policy_version: str
    editorial_origin: str
    editorial_sha256: str
    scored_judgment_sha256: str
    runtime_manifest_sha: str
    metric_version: str
    # What the provider proves about the measurement, not about the market (#265). Nullable together:
    # a `NULL` window means unproven, and it is the answer a consumer must act on rather than default.
    # `whale_long_profit_bps` is the provider's own `Whale Long Profit N%` and nothing more — not an
    # account count, not a total unrealised PnL, and not "every smart-money account is in profit".
    source_strategy_id: str | None
    source_contract_version: str | None
    measurement_window_ms: int | None
    symbol: str
    direction: str
    oi_change_bps: int
    oi_value_usd: int
    whale_long_profit_bps: int
    whale_oi_ratio_bps: int
    rank_in_window: int
    observed_at_ms: int
    ingest_mode: str
    # LEFT JOIN on the leader Item, then a JSON member: absent frames and untagged frames both read None.
    venue: str | None


class NewsTradeProjectionRow(TypedDict):
    """One model Triage verdict that pushed on a crypto-class Event, frozen at the verdict cutoff."""

    event_id: str
    verdict_created_at_ms: int
    final_decision: str
    # Nullable in `news_verdicts` and deliberately not filtered here: a verdict with no evidence pointer
    # is still a real judgment, and adding the predicate would silently narrow the projection's rows.
    # `program_sha256`, `scored_judgment_sha256` and `runtime_manifest_sha` are nullable too, but the
    # WHERE clause already requires each of them, so those cross as `str`.
    evidence_version: int | None
    evidence_sha256: str | None
    focus_fact_id: str | None
    verdict: Any
    learning_epoch: str
    program_version: str
    program_sha256: str
    policy_version: str
    editorial_origin: str
    editorial_sha256: str
    scored_judgment_sha256: str
    runtime_manifest_sha: str
    opened_at_ms: int
    comparison_fingerprint: str
    asset_class: str
    grounded_assets: Any
    ingest_mode: str
    source_artifact_id: str | None
    # Derived here from the canonical URL's own identity, never selected: see the note in the reader.
    source_published_at_ms: int | None


class LiquidationTradeProjectionRow(TypedDict):
    """One admission-time normalized forced-flow fact; direction remains descriptive only."""

    source_key: str
    item_id: str
    fact_id: str
    symbol: str
    venue: str
    liquidated_position_side: str
    forced_order_side: str
    notional_usd: Decimal
    quantity: Decimal | None
    price: Decimal
    event_at_ms: int
    received_at_ms: int
    parser_version: str
    provider_record_identity: str
    symbol_contract_identity: str
    position_side_semantics: str
    quantity_semantics: str
    notional_semantics: str
    price_semantics: str
    completeness_assumption: str
    throttle_assumption: str
    source_contract_version: str
    source_contract_complete: bool
    ingest_mode: str


class TradeInstrumentProjectionRow(TypedDict):
    """One exactly-listed native crypto perpetual for one underlying."""

    venue: str
    venue_symbol: str
    base_symbol: str
    instrument_class: str
    quote_asset: str | None
    status: str
    last_seen_ms: int


class TradeProjectionStorage:
    conn: Any

    def trade_candidate_oi_rows(
        self,
        *,
        metric_version: str,
        after_created_at_ms: int,
        until_created_at_ms: int,
        limit: int = TRADE_PROJECTION_ROW_LIMIT,
    ) -> list[OiTradeProjectionRow]:
        """Every current-generation, live, successfully parsed OI fact in the window (#264).

        What this read proves is that the *fact* is trustworthy: it was parsed by this generation's
        deterministic judge, under the executable Program/policy identity, from a live ingest. What it
        deliberately no longer decides is whether the fact may reach capital. Two rules used to live in
        this SELECT and do not any more:

        * `final_decision IN ('push','escalate')` made the reader's product policy the capital lane's
          entry. The reader's rule is `whale_oi_ratio > 80%`; the strategy #265 targets needs `> 50%`,
          so five of the seven qualifying frames in the last seven days were dropped here and Trading
          never saw them. The column is still selected, as audit, beside `source_rule`.
        * `rank_in_window <= N` and `oi_value_usd >= N` were Trading's own thresholds executed in News's
          SQL. A row filtered out here is indistinguishable from a row that never existed, which is what
          made `oi_rows = 0` unable to say *why*. They now belong to the Trading Candidate Gate, which
          records a named reason per source instead.

        `venue` comes from the Item's own `provider_metadata.source` rather than the ledger, which does
        not store it. That field is the single strongest discriminator the OI research measured
        (Hyperliquid +1.35% vs Binance -0.26% at 4 h), so a projection that dropped it would leave the
        trading lane unable to test its best-supported hypothesis.

        Newest first at the limit (#211). The consumer reads a window wide enough to hold an hour of
        attachable context, and every row it can act on *now* is at the recent end of it. Truncating
        the far end drops rows that could only ever have been superseded context; truncating the near
        end would drop the triggers.
        """

        rows = self.conn.execute(
            """
            SELECT v.event_id,
                   v.created_at_ms          AS verdict_created_at_ms,
                   v.final_decision,
                   v.trace -> 'oi_signal' ->> 'rule' AS source_rule,
                   epoch.epoch_id           AS learning_epoch,
                   v.program_version,
                   v.program_sha256,
                   v.policy_version,
                   v.editorial ->> 'editorial_origin' AS editorial_origin,
                   v.editorial ->> 'editorial_sha256' AS editorial_sha256,
                   v.scored_judgment_sha256,
                   v.runtime_manifest_sha,
                   s.metric_version,
                   s.source_strategy_id,
                   s.source_contract_version,
                   s.measurement_window_ms,
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
                ON epoch.epoch_id = 'program_v7'
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
               AND v.degraded = false
               AND e.ingest_mode = 'live'
               AND v.created_at_ms > %s
               AND v.created_at_ms <= %s
             ORDER BY v.created_at_ms DESC, v.event_id DESC
             LIMIT %s
            """,
            (
                metric_version,
                int(after_created_at_ms),
                int(until_created_at_ms),
                int(limit),
            ),
        ).fetchall()
        return [_oi_projection_row(row) for row in rows]

    def trade_candidate_news_rows(
        self,
        *,
        after_created_at_ms: int,
        until_created_at_ms: int,
        limit: int = TRADE_PROJECTION_ROW_LIMIT,
    ) -> list[NewsTradeProjectionRow]:
        """Model Triage verdicts that pushed on a crypto-class Event, frozen at the verdict cutoff.

        Only the structural conditions live in SQL. Single-primary, grounding, novelty and magnitude are
        the trading lane's own eligibility rules and stay pure functions over the verdict document, so
        they are testable without a database and cannot silently diverge from the funnel report.

        Newest first at the limit, for the reason given on the OI read.
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
                ON epoch.epoch_id = 'program_v7'
               AND e.opened_at_ms >= epoch.starts_at_ms
               AND v.created_at_ms >= epoch.starts_at_ms
              LEFT JOIN news_items i ON i.item_id = e.leader_item_id
             WHERE v.stage = 'triage'
               AND v.program_version = 'news_semantic_program_v5'
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
             ORDER BY v.created_at_ms DESC, v.event_id DESC
             LIMIT %s
            """,
            (int(after_created_at_ms), int(until_created_at_ms), int(limit)),
        ).fetchall()
        return [_news_projection_row(row) for row in rows]

    def trade_candidate_liquidation_rows(
        self,
        *,
        after_received_at_ms: int,
        until_received_at_ms: int,
        limit: int = TRADE_PROJECTION_ROW_LIMIT,
    ) -> list[LiquidationTradeProjectionRow]:
        """Typed live forced-flow facts; no strategy or directional inference is made here."""

        rows = self.conn.execute(
            """
            SELECT l.source_key, l.item_id, l.fact_id, l.symbol, l.venue,
                   l.liquidated_position_side, l.forced_order_side, l.notional_usd,
                   l.quantity, l.price, l.event_at_ms, l.received_at_ms, l.parser_version,
                   l.provider_record_identity, l.symbol_contract_identity,
                   l.position_side_semantics, l.quantity_semantics, l.notional_semantics,
                   l.price_semantics, l.completeness_assumption, l.throttle_assumption,
                   l.source_contract_version, l.source_contract_complete, l.ingest_mode
              FROM news_market_liquidations l
             WHERE l.ingest_mode = 'live'
               AND l.received_at_ms > %s
               AND l.received_at_ms <= %s
             ORDER BY l.received_at_ms DESC, l.source_key DESC
             LIMIT %s
            """,
            (int(after_received_at_ms), int(until_received_at_ms), int(limit)),
        ).fetchall()
        return [_liquidation_projection_row(row) for row in rows]

    def trade_candidate_instrument(
        self, *, base_symbol: str, venues: Sequence[str]
    ) -> list[TradeInstrumentProjectionRow]:
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
        return [
            TradeInstrumentProjectionRow(
                venue=row["venue"],
                venue_symbol=row["venue_symbol"],
                base_symbol=row["base_symbol"],
                instrument_class=row["instrument_class"],
                quote_asset=row["quote_asset"],
                status=row["status"],
                last_seen_ms=row["last_seen_ms"],
            )
            for row in rows
        ]


def _oi_projection_row(row: Any) -> OiTradeProjectionRow:
    """Name every selected column. No coercion: psycopg already returns the column's own type."""

    return OiTradeProjectionRow(
        event_id=row["event_id"],
        verdict_created_at_ms=row["verdict_created_at_ms"],
        final_decision=row["final_decision"],
        source_rule=row["source_rule"],
        learning_epoch=row["learning_epoch"],
        program_version=row["program_version"],
        program_sha256=row["program_sha256"],
        policy_version=row["policy_version"],
        editorial_origin=row["editorial_origin"],
        editorial_sha256=row["editorial_sha256"],
        scored_judgment_sha256=row["scored_judgment_sha256"],
        runtime_manifest_sha=row["runtime_manifest_sha"],
        metric_version=row["metric_version"],
        source_strategy_id=row["source_strategy_id"],
        source_contract_version=row["source_contract_version"],
        measurement_window_ms=row["measurement_window_ms"],
        symbol=row["symbol"],
        direction=row["direction"],
        oi_change_bps=row["oi_change_bps"],
        oi_value_usd=row["oi_value_usd"],
        whale_long_profit_bps=row["whale_long_profit_bps"],
        whale_oi_ratio_bps=row["whale_oi_ratio_bps"],
        rank_in_window=row["rank_in_window"],
        observed_at_ms=row["observed_at_ms"],
        ingest_mode=row["ingest_mode"],
        venue=row["venue"],
    )


def _news_projection_row(row: Any) -> NewsTradeProjectionRow:
    """Name every selected column, and derive the one field that is not selected.

    #154/#157 keep the artifact id as a column and derive its publication time from the URL's own
    identity (a snowflake, for x.com), so the projection derives it the same way the delivery card's
    `source_age_s` does rather than inventing a second answer. `canonical_url` is read here and does
    not cross the boundary.
    """

    _, published_at_ms = source_artifact_identity(str(row["canonical_url"] or ""))
    return NewsTradeProjectionRow(
        event_id=row["event_id"],
        verdict_created_at_ms=row["verdict_created_at_ms"],
        final_decision=row["final_decision"],
        evidence_version=row["evidence_version"],
        evidence_sha256=row["evidence_sha256"],
        focus_fact_id=row["focus_fact_id"],
        verdict=row["verdict"],
        learning_epoch=row["learning_epoch"],
        program_version=row["program_version"],
        program_sha256=row["program_sha256"],
        policy_version=row["policy_version"],
        editorial_origin=row["editorial_origin"],
        editorial_sha256=row["editorial_sha256"],
        scored_judgment_sha256=row["scored_judgment_sha256"],
        runtime_manifest_sha=row["runtime_manifest_sha"],
        opened_at_ms=row["opened_at_ms"],
        comparison_fingerprint=row["comparison_fingerprint"],
        asset_class=row["asset_class"],
        grounded_assets=row["grounded_assets"],
        ingest_mode=row["ingest_mode"],
        source_artifact_id=row["source_artifact_id"],
        source_published_at_ms=published_at_ms,
    )


def _liquidation_projection_row(row: Any) -> LiquidationTradeProjectionRow:
    return LiquidationTradeProjectionRow(
        source_key=row["source_key"],
        item_id=row["item_id"],
        fact_id=row["fact_id"],
        symbol=row["symbol"],
        venue=row["venue"],
        liquidated_position_side=row["liquidated_position_side"],
        forced_order_side=row["forced_order_side"],
        notional_usd=row["notional_usd"],
        quantity=row["quantity"],
        price=row["price"],
        event_at_ms=row["event_at_ms"],
        received_at_ms=row["received_at_ms"],
        parser_version=row["parser_version"],
        provider_record_identity=row["provider_record_identity"],
        symbol_contract_identity=row["symbol_contract_identity"],
        position_side_semantics=row["position_side_semantics"],
        quantity_semantics=row["quantity_semantics"],
        notional_semantics=row["notional_semantics"],
        price_semantics=row["price_semantics"],
        completeness_assumption=row["completeness_assumption"],
        throttle_assumption=row["throttle_assumption"],
        source_contract_version=row["source_contract_version"],
        source_contract_complete=row["source_contract_complete"],
        ingest_mode=row["ingest_mode"],
    )
