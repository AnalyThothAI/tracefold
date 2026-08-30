"""News-owned point-in-time projection read by App composition for Trading.

The row contracts below are the published shape of that projection. They are `TypedDict`s
because the rows *are* the SELECT lists — naming the columns is the whole contract, and a runtime
model here would coerce values PostgreSQL already typed and turn a nullable LEFT JOIN column into a
different value. Nothing outside this module may add, rename or retype a key without editing them,
which is what makes the App-side mapper break at type-check time instead of at 03:00 in a runner.

They say nothing about *trade* eligibility: freshness, rank and the liquidity floor live in the
capital lane's own pure rules. This side owns generation identity, the point-in-time boundaries and
the deterministic order.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, TypedDict

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
#
# v9 (#331): the editorial-verdict and liquidation reads are gone. Editorial News no longer triggers
# automatic capital and there is no online liquidation consumer, so publishing either was a projection
# nothing read — and an invitation for the capital lane to grow a second trigger by accident. The OI
# read and the instrument reads are what remains.
# v10 (#369): OI carries its typed judgment identity directly.  The retired
# synthetic Editorial envelope is neither read nor reconstructed.
# v11 (#377): the OI ledger freezes its source Item, venue, availability clock and learning epoch at
# insertion. Evidence capture no longer reconstructs an old frame from a mutable Event leader or the
# currently active learning bundle.
# v12 (#377): evidence capture and fixed-window verification publish bounded bulk catalogue/source
# projections. App no longer runs one catalogue query per source or treats Trading's Gate ledger as
# the universe of News facts that should have reached that Gate.
NEWS_TRADE_PROJECTION_VERSION = "news_trade_projection_v12"

# One read's ceiling per lane. The consumer's widest configured horizon is `max_age + max(lookback)` —
# 65 minutes at the shipped configuration — and the measured live rate through these exact predicates
# is about eleven rows an hour on the News lane and nothing like a full lane on the OI one, so this is
# roughly twenty times the volume it has to carry. It is still a ceiling, not a promise: a lane that
# comes back with exactly this many rows was truncated, and the funnel's `oi_rows` / `news_rows`
# counters are where that shows.
# The epoch a projection row belongs to is the epoch of the deployment that produced it (#314). Joining
# through the active agent keeps that true after a rollback, where "the newest epoch row" would name a
# bundle this process is not running.
_CURRENT_EPOCH_JOIN = """
              JOIN news_learning_epochs epoch
                ON epoch.bundle_sha = (
                  SELECT stable_sha FROM news_review_active_agent_v1
                   ORDER BY created_at_ms DESC LIMIT 1
                )
               AND e.opened_at_ms >= epoch.starts_at_ms
               AND v.created_at_ms >= epoch.starts_at_ms"""

TRADE_PROJECTION_ROW_LIMIT = 256


class OiTradeProjectionRow(TypedDict):
    """One parsed deterministic OI telemetry fact, with its rank-ledger row and the frame's venue.

    `final_decision` and `source_rule` are the reader's own judgment of the same frame. They are here so
    a capital decision can be traced to the verdict beside it, and for no other purpose: since #264 the
    reader's push/drop no longer decides whether Trading can see the fact.
    """

    event_id: str
    source_item_id: str
    verdict_created_at_ms: int
    final_decision: str
    # `evaluate_oi`'s non-empty named rule from the current canonical judgment atom.
    source_rule: str
    learning_epoch: str
    program_version: str
    program_sha256: str
    policy_version: str
    judgment_contract_version: str
    judgment_origin: str
    judgment_sha256: str
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
    source_available_at_ms: int
    ingest_mode: str
    # LEFT JOIN on the leader Item, then a JSON member: absent frames and untagged frames both read None.
    venue: str | None


class TradeInstrumentProjectionRow(TypedDict):
    """One exactly-listed native crypto perpetual for one underlying."""

    venue: str
    venue_symbol: str
    base_symbol: str
    instrument_class: str
    quote_asset: str | None
    status: str
    last_seen_ms: int


class TradeEvidenceCollectionHealthRow(TypedDict):
    """Blind-safe collector state and source denominator for one future batch."""

    connected: bool
    last_frame_at_ms: int | None
    last_error_code: str | None
    expected_source_count: int


class TradeEvidenceCatalogProjectionRow(TypedDict):
    """One point-in-time listing row attached to its bounded evidence source."""

    event_id: str
    metric_version: str
    source_observed_at_ms: int
    venue: str
    venue_symbol: str
    base_symbol: str
    instrument_class: str
    quote_asset: str | None
    status: str
    last_seen_ms: int


class TradeFixedWindowOiSourceRow(TypedDict):
    """Complete News-owned OI source identity for one fixed acceptance window."""

    event_id: str
    metric_version: str
    source_venue: str | None
    observed_at_ms: int
    available_at_ms: int
    verdict_created_at_ms: int


class TradeProjectionStorage:
    conn: Any

    def trade_evidence_collection_health(
        self,
        *,
        start_observed_at_ms: int,
        end_observed_at_ms: int,
        available_at_or_before_ms: int,
        source_venues: tuple[str, ...],
    ) -> TradeEvidenceCollectionHealthRow:
        """News-owned raw collector and denominator facts for blind-safe Trading health."""

        row = self.conn.execute(
            """
            SELECT ingest.connected, ingest.last_frame_at_ms, ingest.last_error_code,
                   (SELECT count(*)
                      FROM news_oi_signals signal
                     WHERE signal.observed_at_ms >= %(start)s
                       AND signal.observed_at_ms < %(end)s
                       AND signal.available_at_ms <= %(available)s
                       AND signal.source_venue = ANY(%(venues)s)) AS expected_source_count
              FROM news_ingest_state ingest
             WHERE ingest.singleton_key = 'opennews'
            """,
            {
                "start": int(start_observed_at_ms),
                "end": int(end_observed_at_ms),
                "available": int(available_at_or_before_ms),
                "venues": list(source_venues),
            },
        ).fetchone()
        return TradeEvidenceCollectionHealthRow(
            connected=False if row is None else bool(row["connected"]),
            last_frame_at_ms=None if row is None else row["last_frame_at_ms"],
            last_error_code="collector_state_missing" if row is None else row["last_error_code"],
            expected_source_count=0 if row is None else int(row["expected_source_count"]),
        )

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
            f"""
            SELECT v.event_id,
                   s.source_item_id,
                   v.created_at_ms          AS verdict_created_at_ms,
                   v.final_decision,
                   v.trace #>> '{{judgment,rule}}' AS source_rule,
                   epoch.epoch_id           AS learning_epoch,
                   v.program_version,
                   v.program_sha256,
                   v.policy_version,
                   v.judgment_contract_version,
                   v.judgment_origin,
                   v.scored_judgment_sha256 AS judgment_sha256,
                   v.runtime_manifest_sha,
                   s.metric_version,
                   s.source_strategy_id,
                   s.source_contract_version,
                   s.measurement_window_ms,
                   v.trace #>> '{{judgment,signal,symbol}}' AS symbol,
                   v.trace #>> '{{judgment,signal,direction}}' AS direction,
                   (v.trace #>> '{{judgment,signal,oi_change_bps}}')::bigint AS oi_change_bps,
                   (v.trace #>> '{{judgment,signal,oi_value_usd}}')::bigint AS oi_value_usd,
                   (v.trace #>> '{{judgment,signal,whale_long_profit_bps}}')::bigint AS whale_long_profit_bps,
                   (v.trace #>> '{{judgment,signal,whale_oi_ratio_bps}}')::bigint AS whale_oi_ratio_bps,
                   (v.trace #>> '{{judgment,rank_in_window}}')::integer AS rank_in_window,
                   s.observed_at_ms,
                   s.available_at_ms AS source_available_at_ms,
                   i.first_ingest_mode AS ingest_mode,
                   s.source_venue AS venue
              FROM news_verdicts v
              JOIN news_oi_signals s
                ON s.event_id = v.event_id AND s.metric_version = %s
               AND v.trace #> '{{judgment,signal}}' = jsonb_build_object(
                     'symbol', s.symbol,
                     'direction', s.direction,
                     'oi_change_bps', s.oi_change_bps,
                     'oi_value_usd', s.oi_value_usd,
                     'whale_long_profit_bps', s.whale_long_profit_bps,
                     'whale_oi_ratio_bps', s.whale_oi_ratio_bps
                   )
               AND (v.trace #>> '{{judgment,rank_in_window}}')::integer = s.rank_in_window
               AND v.trace #>> '{{oi_signal,source_strategy_id}}' IS NOT DISTINCT FROM s.source_strategy_id
               AND v.trace #>> '{{oi_signal,source_contract_version}}' IS NOT DISTINCT FROM s.source_contract_version
               AND (v.trace #>> '{{oi_signal,measurement_window_ms}}')::bigint
                     IS NOT DISTINCT FROM s.measurement_window_ms
              JOIN news_items i ON i.item_id = s.source_item_id
              JOIN news_current_events_v1 e ON e.event_id = v.event_id{_CURRENT_EPOCH_JOIN}
             WHERE v.stage = 'triage'
               AND v.judgment_contract_version = 'news_judgment_v2'
               AND v.judgment_origin = 'oi'
               AND v.program_version = 'news_oi_signal_v2'
               AND v.policy_version = 'news_triage_policy_v11'
               AND v.editorial IS NULL
               AND v.program_sha256 ~ '^[0-9a-f]{{64}}$'
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

    def trade_evidence_oi_rows(
        self,
        *,
        metric_version: str,
        start_observed_at_ms: int,
        end_observed_at_ms: int,
        known_at_or_before_ms: int,
        available_at_or_before_ms: int,
        limit: int = TRADE_PROJECTION_ROW_LIMIT,
    ) -> list[OiTradeProjectionRow]:
        """Freeze sources known by the batch end and durably available by its capture clock (#377)."""

        rows = self.conn.execute(
            """
            SELECT v.event_id,
                   s.source_item_id,
                   v.created_at_ms          AS verdict_created_at_ms,
                   v.final_decision,
                   v.trace #>> '{judgment,rule}' AS source_rule,
                   s.learning_epoch,
                   v.program_version,
                   v.program_sha256,
                   v.policy_version,
                   v.judgment_contract_version,
                   v.judgment_origin,
                   v.scored_judgment_sha256 AS judgment_sha256,
                   v.runtime_manifest_sha,
                   s.metric_version,
                   s.source_strategy_id,
                   s.source_contract_version,
                   s.measurement_window_ms,
                   v.trace #>> '{judgment,signal,symbol}' AS symbol,
                   v.trace #>> '{judgment,signal,direction}' AS direction,
                   (v.trace #>> '{judgment,signal,oi_change_bps}')::bigint AS oi_change_bps,
                   (v.trace #>> '{judgment,signal,oi_value_usd}')::bigint AS oi_value_usd,
                   (v.trace #>> '{judgment,signal,whale_long_profit_bps}')::bigint AS whale_long_profit_bps,
                   (v.trace #>> '{judgment,signal,whale_oi_ratio_bps}')::bigint AS whale_oi_ratio_bps,
                   (v.trace #>> '{judgment,rank_in_window}')::integer AS rank_in_window,
                   s.observed_at_ms,
                   s.available_at_ms AS source_available_at_ms,
                   i.first_ingest_mode AS ingest_mode,
                   s.source_venue AS venue
              FROM news_verdicts v
              JOIN news_oi_signals s
                ON s.event_id = v.event_id AND s.metric_version = %s
               AND v.trace #> '{judgment,signal}' = jsonb_build_object(
                     'symbol', s.symbol,
                     'direction', s.direction,
                     'oi_change_bps', s.oi_change_bps,
                     'oi_value_usd', s.oi_value_usd,
                     'whale_long_profit_bps', s.whale_long_profit_bps,
                     'whale_oi_ratio_bps', s.whale_oi_ratio_bps
                   )
               AND (v.trace #>> '{judgment,rank_in_window}')::integer = s.rank_in_window
               AND v.trace #>> '{oi_signal,source_strategy_id}' IS NOT DISTINCT FROM s.source_strategy_id
               AND v.trace #>> '{oi_signal,source_contract_version}' IS NOT DISTINCT FROM s.source_contract_version
               AND (v.trace #>> '{oi_signal,measurement_window_ms}')::bigint
                     IS NOT DISTINCT FROM s.measurement_window_ms
              JOIN news_items i ON i.item_id = s.source_item_id
             WHERE v.stage = 'triage'
               AND v.judgment_contract_version = 'news_judgment_v2'
               AND v.judgment_origin = 'oi'
               AND v.program_version = 'news_oi_signal_v2'
               AND v.policy_version = 'news_triage_policy_v11'
               AND v.editorial IS NULL
               AND v.program_sha256 ~ '^[0-9a-f]{64}$'
               AND v.scored_judgment_sha256 IS NOT NULL
               AND v.runtime_manifest_sha IS NOT NULL
               AND v.degraded = false
               AND s.available_at_ms <= %s
               AND s.observed_at_ms >= %s
               AND s.observed_at_ms < %s
               AND v.created_at_ms <= %s
             ORDER BY s.observed_at_ms, v.event_id
             LIMIT %s
            """,
            (
                metric_version,
                int(available_at_or_before_ms),
                int(start_observed_at_ms),
                int(end_observed_at_ms),
                int(known_at_or_before_ms),
                int(limit),
            ),
        ).fetchall()
        return [_oi_projection_row(row) for row in rows]

    def trade_evidence_catalog_rows(
        self,
        *,
        metric_version: str,
        start_observed_at_ms: int,
        end_observed_at_ms: int,
        known_at_or_before_ms: int,
        available_at_or_before_ms: int,
        source_limit: int,
        catalog_limit: int,
    ) -> list[TradeEvidenceCatalogProjectionRow]:
        """Bulk point-in-time catalogues for the exact bounded evidence-source query."""

        rows = self.conn.execute(
            """
            WITH sources AS (
              SELECT v.event_id, s.metric_version, s.observed_at_ms,
                     v.trace #>> '{judgment,signal,symbol}' AS symbol,
                     s.source_venue
                FROM news_verdicts v
                JOIN news_oi_signals s
                  ON s.event_id = v.event_id AND s.metric_version = %(metric)s
                 AND v.trace #> '{judgment,signal}' = jsonb_build_object(
                       'symbol', s.symbol,
                       'direction', s.direction,
                       'oi_change_bps', s.oi_change_bps,
                       'oi_value_usd', s.oi_value_usd,
                       'whale_long_profit_bps', s.whale_long_profit_bps,
                       'whale_oi_ratio_bps', s.whale_oi_ratio_bps
                     )
                 AND (v.trace #>> '{judgment,rank_in_window}')::integer = s.rank_in_window
                 AND v.trace #>> '{oi_signal,source_strategy_id}' IS NOT DISTINCT FROM s.source_strategy_id
                 AND v.trace #>> '{oi_signal,source_contract_version}' IS NOT DISTINCT FROM s.source_contract_version
                 AND (v.trace #>> '{oi_signal,measurement_window_ms}')::bigint
                       IS NOT DISTINCT FROM s.measurement_window_ms
               WHERE v.stage = 'triage'
                 AND v.judgment_contract_version = 'news_judgment_v2'
                 AND v.judgment_origin = 'oi'
                 AND v.program_version = 'news_oi_signal_v2'
                 AND v.policy_version = 'news_triage_policy_v11'
                 AND v.editorial IS NULL
                 AND v.program_sha256 ~ '^[0-9a-f]{64}$'
                 AND v.scored_judgment_sha256 IS NOT NULL
                 AND v.runtime_manifest_sha IS NOT NULL
                 AND v.degraded = false
                 AND s.available_at_ms <= %(available)s
                 AND s.observed_at_ms >= %(start)s
                 AND s.observed_at_ms < %(end)s
                 AND v.created_at_ms <= %(known)s
               ORDER BY s.observed_at_ms, v.event_id
               LIMIT %(source_limit)s
            )
            SELECT source.event_id, source.metric_version,
                   source.observed_at_ms AS source_observed_at_ms,
                   historical.venue, historical.venue_symbol, historical.base_symbol,
                   historical.instrument_class, historical.quote_asset, historical.status,
                   historical.observed_at_ms AS last_seen_ms
              FROM sources source
              JOIN LATERAL (
                SELECT DISTINCT ON (event.venue, event.venue_symbol)
                       event.venue, event.venue_symbol, event.base_symbol,
                       event.instrument_class, event.quote_asset, event.status, event.observed_at_ms
                  FROM news_market_instrument_listing_events event
                 WHERE event.venue = CASE
                         WHEN lower(coalesce(source.source_venue, '')) IN
                           ('binance', 'binance.perp', 'binance.usdm') THEN 'binance.perp'
                         WHEN lower(coalesce(source.source_venue, '')) IN
                           ('hyperliquid', 'hl.perp', 'hyperliquid.perp') THEN 'hl.perp'
                         ELSE ''
                       END
                   AND event.base_symbol = regexp_replace(upper(btrim(source.symbol)), '^XYZ-', '')
                   AND event.observed_at_ms <= source.observed_at_ms
                 ORDER BY event.venue, event.venue_symbol, event.observed_at_ms DESC
              ) historical ON historical.status = 'trading' AND historical.instrument_class = 'crypto'
             ORDER BY source.observed_at_ms, source.event_id, historical.venue,
                      CASE historical.quote_asset WHEN 'USDT' THEN 0 WHEN 'USDC' THEN 1 ELSE 2 END,
                      length(historical.venue_symbol), historical.venue_symbol
             LIMIT %(catalog_limit)s
            """,
            {
                "metric": metric_version,
                "available": int(available_at_or_before_ms),
                "start": int(start_observed_at_ms),
                "end": int(end_observed_at_ms),
                "known": int(known_at_or_before_ms),
                "source_limit": int(source_limit),
                "catalog_limit": int(catalog_limit),
            },
        ).fetchall()
        return [
            TradeEvidenceCatalogProjectionRow(
                event_id=str(row["event_id"]),
                metric_version=str(row["metric_version"]),
                source_observed_at_ms=int(row["source_observed_at_ms"]),
                venue=str(row["venue"]),
                venue_symbol=str(row["venue_symbol"]),
                base_symbol=str(row["base_symbol"]),
                instrument_class=str(row["instrument_class"]),
                quote_asset=None if row["quote_asset"] is None else str(row["quote_asset"]),
                status=str(row["status"]),
                last_seen_ms=int(row["last_seen_ms"]),
            )
            for row in rows
        ]

    def trade_fixed_window_oi_sources(
        self,
        *,
        metric_version: str,
        start_observed_at_ms: int,
        end_observed_at_ms: int,
        drain_cutoff_ms: int,
        limit: int,
    ) -> list[TradeFixedWindowOiSourceRow]:
        """Complete, bounded News source universe known by the preregistered drain cutoff."""

        rows = self.conn.execute(
            """
            SELECT v.event_id, s.metric_version, s.source_venue,
                   s.observed_at_ms, s.available_at_ms,
                   v.created_at_ms AS verdict_created_at_ms
              FROM news_verdicts v
              JOIN news_oi_signals s
                ON s.event_id = v.event_id AND s.metric_version = %s
               AND v.trace #> '{judgment,signal}' = jsonb_build_object(
                     'symbol', s.symbol,
                     'direction', s.direction,
                     'oi_change_bps', s.oi_change_bps,
                     'oi_value_usd', s.oi_value_usd,
                     'whale_long_profit_bps', s.whale_long_profit_bps,
                     'whale_oi_ratio_bps', s.whale_oi_ratio_bps
                   )
               AND (v.trace #>> '{judgment,rank_in_window}')::integer = s.rank_in_window
               AND v.trace #>> '{oi_signal,source_strategy_id}' IS NOT DISTINCT FROM s.source_strategy_id
               AND v.trace #>> '{oi_signal,source_contract_version}' IS NOT DISTINCT FROM s.source_contract_version
               AND (v.trace #>> '{oi_signal,measurement_window_ms}')::bigint
                     IS NOT DISTINCT FROM s.measurement_window_ms
             WHERE v.stage = 'triage'
               AND v.judgment_contract_version = 'news_judgment_v2'
               AND v.judgment_origin = 'oi'
               AND v.program_version = 'news_oi_signal_v2'
               AND v.policy_version = 'news_triage_policy_v11'
               AND v.editorial IS NULL
               AND v.program_sha256 ~ '^[0-9a-f]{64}$'
               AND v.scored_judgment_sha256 IS NOT NULL
               AND v.runtime_manifest_sha IS NOT NULL
               AND v.degraded = false
               AND s.observed_at_ms >= %s AND s.observed_at_ms < %s
               AND s.available_at_ms <= %s AND v.created_at_ms <= %s
             ORDER BY s.observed_at_ms, v.event_id
             LIMIT %s
            """,
            (
                metric_version,
                int(start_observed_at_ms),
                int(end_observed_at_ms),
                int(drain_cutoff_ms),
                int(drain_cutoff_ms),
                int(limit),
            ),
        ).fetchall()
        return [
            TradeFixedWindowOiSourceRow(
                event_id=str(row["event_id"]),
                metric_version=str(row["metric_version"]),
                source_venue=None if row["source_venue"] is None else str(row["source_venue"]),
                observed_at_ms=int(row["observed_at_ms"]),
                available_at_ms=int(row["available_at_ms"]),
                verdict_created_at_ms=int(row["verdict_created_at_ms"]),
            )
            for row in rows
        ]

    def trade_candidate_instrument(
        self,
        *,
        base_symbol: str,
        venues: Sequence[str],
        observed_at_ms: int | None = None,
    ) -> list[TradeInstrumentProjectionRow]:
        """Exactly-listed native crypto perpetuals for one underlying, in the caller's venue order.

        `instrument_class = 'crypto'` is not decoration: Binance labels its 169 TradFi perps `EQUITY`
        and friends, so a `WMT` Event whose Gate class says crypto still resolves to nothing here.
        HIP-3 builder venues (`hl.xyz`) are excluded by naming the two native perp venues explicitly.

        Live callers omit ``observed_at_ms`` and see only the current catalogue. Replay callers read the
        last immutable listing event at or before the source cutoff, so neither a later listing/relisting
        nor a present-day delisting can alter the instrument identity that source fact observed.
        """

        if not venues:
            return []
        normalized_base = str(base_symbol or "").strip().upper()
        if observed_at_ms is None:
            rows = self.conn.execute(
                """
                SELECT venue, venue_symbol, base_symbol, instrument_class,
                       quote_asset, status, last_seen_ms
                  FROM news_market_instruments
                 WHERE base_symbol = %s
                   AND venue = ANY(%s)
                   AND status = 'trading'
                   AND instrument_class = 'crypto'
                 ORDER BY venue,
                          CASE quote_asset WHEN 'USDT' THEN 0 WHEN 'USDC' THEN 1 ELSE 2 END,
                          length(venue_symbol),
                          venue_symbol
                """,
                (normalized_base, list(venues)),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                WITH candidate_symbols AS (
                  SELECT DISTINCT venue, venue_symbol
                    FROM news_market_instrument_listing_events
                   WHERE venue = ANY(%s)
                     AND base_symbol = %s
                     AND observed_at_ms <= %s
                ), historical AS (
                  SELECT DISTINCT ON (event.venue, event.venue_symbol)
                         event.venue, event.venue_symbol, event.observed_at_ms,
                         event.base_symbol, event.instrument_class, event.quote_asset, event.status
                    FROM news_market_instrument_listing_events AS event
                    JOIN candidate_symbols AS candidate
                      ON candidate.venue = event.venue
                     AND candidate.venue_symbol = event.venue_symbol
                   WHERE event.observed_at_ms <= %s
                   ORDER BY event.venue, event.venue_symbol, event.observed_at_ms DESC
                )
                SELECT venue, venue_symbol, base_symbol, instrument_class,
                       quote_asset, status, observed_at_ms AS last_seen_ms
                  FROM historical
                 WHERE base_symbol = %s
                   AND status = 'trading'
                   AND instrument_class = 'crypto'
                 ORDER BY venue,
                          CASE quote_asset WHEN 'USDT' THEN 0 WHEN 'USDC' THEN 1 ELSE 2 END,
                          length(venue_symbol),
                          venue_symbol
                """,
                (
                    list(venues),
                    normalized_base,
                    int(observed_at_ms),
                    int(observed_at_ms),
                    normalized_base,
                ),
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

    def trade_execution_instruments(self) -> list[TradeInstrumentProjectionRow]:
        """All active Binance USD-M rows for the cold capability-snapshot command.

        Crypto classification deliberately crosses the App seam instead of filtering here: every
        provider candidate must receive an included row or a named mechanical exclusion.
        """

        rows = self.conn.execute(
            """
            SELECT venue, venue_symbol, base_symbol, instrument_class, quote_asset, status, last_seen_ms
              FROM news_market_instruments
             WHERE venue = 'binance.perp'
               AND status = 'trading'
             ORDER BY venue_symbol, base_symbol
            """
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
        source_item_id=row["source_item_id"],
        verdict_created_at_ms=row["verdict_created_at_ms"],
        final_decision=row["final_decision"],
        source_rule=row["source_rule"],
        learning_epoch=row["learning_epoch"],
        program_version=row["program_version"],
        program_sha256=row["program_sha256"],
        policy_version=row["policy_version"],
        judgment_contract_version=row["judgment_contract_version"],
        judgment_origin=row["judgment_origin"],
        judgment_sha256=row["judgment_sha256"],
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
        source_available_at_ms=row["source_available_at_ms"],
        ingest_mode=row["ingest_mode"],
        venue=row["venue"],
    )
