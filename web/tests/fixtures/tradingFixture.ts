import type {
  TradingCase,
  TradingGate,
  TradingGateDecision,
  TradingOrder,
  TradingOrders,
  TradingStatus,
} from "@features/trading/api/tradingQueries";

export const TRADING_NOW_MS = Date.parse("2026-08-25T12:00:00Z");

/**
 * The approved paper workbench: real ledger states against the fake exchange, never a live claim.
 */
export function tradingStatusFixture(overrides: Partial<TradingStatus> = {}): TradingStatus {
  return {
    budget: {
      // `max_holding_seconds: 1800`, the shipped default and what this deployment runs. The design's mock
      // used 4 h, which is the one value that could not expose `Math.round(ms / 3.6e6)` rendering the real
      // ceiling as `0 h`.
      max_hold_ms: 1_800_000,
      max_orders_per_day: 4,
      nominal_daily_stop_loss_usd: "4",
      notional_usd: "200",
      orders_today: 3,
      stop_loss_bps: 200,
    },
    counts: {
      cases_by_strategy: { news_oi_alignment_v1: 5, oi_momentum_v1: 4 },
      cases_by_trigger: { news: 5, oi: 4 },
      cases_by_state: { NO_TRADE: 2, ORDER_PREPARED: 3, POLICY_REJECTED: 4 },
      cases_today_by_state: { NO_TRADE: 2, ORDER_PREPARED: 3, POLICY_REJECTED: 4 },
      policy_allowed_24h: 3,
      policy_allowed_today: 3,
      active_orders: 4,
      closed_orders: 2,
      closed_orders_today: 2,
      closed_realized_bps: 12,
      // A UTC calendar-day counter, named for the interval it covers — `merge_funnel` resets it
      // on `day_key`, unlike every rolling count beside it.
      funnel_day_key: "2026-08-25",
      funnel_today: { case_created: 9 },
      // #264: the durable admission ledger, keyed on when the *frame* was observed rather than on when
      // the gate looked, so a restart that re-reads a backlog cannot move yesterday's frames into today.
      candidate_counts_24h: { CASE_CREATED: 1, DEFERRED: 2, EXPIRED: 1, REJECTED: 87 },
      candidate_counts_7d: { CASE_CREATED: 4, EXPIRED: 9, REJECTED: 392 },
      candidate_reasons_24h: {
        "eligibility:oi_value_below_floor": 22,
        "eligibility:rank_above_limit": 65,
        "routing:no_native_perp": 2,
        "market_context:market_data_unavailable": 1,
        "eligibility:trigger_stale": 1,
        "freeze:case_created": 1,
      },
      candidate_reasons_7d: { "eligibility:rank_above_limit": 300 },
      latest_source_at_ms: TRADING_NOW_MS - 60_000,
      latest_gate_eligible_at_ms: TRADING_NOW_MS - 3_600_000,
      latest_case_created_at_ms: TRADING_NOW_MS - 3_600_000,
      latest_order_prepared_at_ms: TRADING_NOW_MS - 3_500_000,
      latest_position_opened_at_ms: TRADING_NOW_MS - 3_400_000,
      latest_position_closed_at_ms: null,
      orders_by_state: { CLOSED: 2, OPEN: 1 },
      liquidation_promotion_ready: false,
      liquidation_promotion_reason: "source_contract_incomplete",
      shadow_by_rule: { source_contract_incomplete: 4 },
      shadow_by_strategy: {
        liquidation_continuation_shadow_v1: 2,
        liquidation_exhaustion_shadow_v1: 2,
      },
      shadow_cohorts: {
        liquidation_continuation_shadow_v1: {
          completed: 1,
          evaluated: 2,
          holdout: 2,
          source_contract_complete: 0,
          coverage_bps: 5_000,
          mean_source_latency_ms: 1_200,
          promotion_ready: false,
          mean_return_bps: 25,
        },
        liquidation_exhaustion_shadow_v1: {
          completed: 1,
          evaluated: 2,
          holdout: 2,
          source_contract_complete: 0,
          coverage_bps: 5_000,
          mean_source_latency_ms: 1_200,
          promotion_ready: false,
          mean_return_bps: 25,
        },
      },
      event_study_cohorts: [
        {
          cohort_key: "liquidation_continuation_shadow_v1|binance|unknown",
          strategy_id: "liquidation_continuation_shadow_v1",
          venue: "binance",
          liquidity_bucket: "unknown",
          evaluated: 2,
          completed: 1,
          holdout: 2,
          source_contract_complete: 0,
          coverage_bps: 5_000,
          mean_source_latency_ms: 1_200,
          promotion_ready: false,
          promotion_reasons: ["source_contract_incomplete", "intraminute_coverage_missing"],
          horizons: {
            "5m": {
              measured: 1,
              missing: 0,
              bootstrap: { mean_bps: 25, lower_95_bps: 25, upper_95_bps: 25 },
            },
            "15m": {
              measured: 1,
              missing: 0,
              bootstrap: { mean_bps: 38, lower_95_bps: 38, upper_95_bps: 38 },
            },
            "1h": {
              measured: 1,
              missing: 0,
              bootstrap: { mean_bps: 52, lower_95_bps: 52, upper_95_bps: 52 },
            },
          },
          mfe_mean_bps: 80,
          mae_mean_bps: -20,
          exit_by_reason: { max_holding: 1 },
          net_ex_funding_bootstrap: { mean_bps: 12, lower_95_bps: 8, upper_95_bps: 16 },
          missing_data: {
            "horizon:5s:source_bar_resolution_unsupported": 1,
            "cost:funding_unavailable": 1,
          },
        },
      ],
    },
    floors: {
      lookback_ms: 3_600_000,
      max_price_move_bps: 600,
      min_oi_value_usd: "20000000",
      min_price_move_bps: 100,
      min_whale_long_profit_bps: 9_500,
    },
    // #269: the rules the lane actually holds, as opposed to the settings document above. The two
    // deliberately disagree in this fixture — the gate admits at 5M while `floors` still reports the
    // operator's 20M — because that drift is exactly what a page comparing against the wrong one shows.
    gate: {
      config_digest: "c".repeat(64),
      max_age_ms: 300_000,
      max_rank_in_window: 2,
      min_oi_value_usd: 5_000_000,
      symbol_cooldown_ms: 1_800_000,
      venue_priority: ["binance", "hyperliquid"],
      version: "trading_candidate_gate_v1",
    },
    strategies: [
      {
        config: {
          allow_short: "False",
          max_price_move_bps: "1000",
          measurement_window_ms: "300000",
          min_oi_change_bps: "500",
          min_price_move_bps: "0",
          min_whale_long_profit_bps: "0",
          min_whale_oi_ratio_bps: "5000",
        },
        config_digest: "a".repeat(64),
        permission: "paper",
        strategy_id: "oi_smart_money_momentum_v1",
        strategy_version: "oi_smart_money_momentum_v1",
        trigger_kinds: ["oi"],
      },
      {
        config: {
          allow_short: "False",
          live_max_price_in: "1",
          live_min_surprise: "2",
          min_whale_long_profit_bps: "9500",
        },
        config_digest: "b".repeat(64),
        permission: "live_reviewed",
        strategy_id: "news_oi_alignment_v1",
        strategy_version: "news_oi_alignment_v1",
        trigger_kinds: ["news", "oi"],
      },
      {
        config: {
          allow_short: "False",
          max_price_move_bps: "600",
          min_price_move_bps: "100",
          min_whale_long_profit_bps: "9500",
        },
        config_digest: "d".repeat(64),
        permission: "paper",
        strategy_id: "oi_momentum_v1",
        strategy_version: "oi_momentum_v1",
        trigger_kinds: ["oi"],
      },
    ],
    measured_at_ms: TRADING_NOW_MS,
    readiness: {
      control: "RUNNING",
      enabled: true,
      execution_backend: "paper",
      execution_configured: true,
      live_mode_supported: false,
      live_ready: false,
      live_readiness: "not_applicable",
      mode: "paper",
      venues: [],
    },
    window_hours: 24,
    ...overrides,
  };
}

export function tradingOrderFixture(overrides: Partial<TradingOrder> = {}): TradingOrder {
  return {
    average_price: "0.8412",
    base_symbol: "WIF",
    case_id: "case-wif",
    event_id: "evt-oi-wif",
    strategy_id: "news_oi_alignment_v1",
    strategy_version: "news_oi_alignment_v1",
    trigger_kind: "news",
    case_observed_at_ms: TRADING_NOW_MS - 400_000,
    case_state: "ORDER_PREPARED",
    created_at_ms: TRADING_NOW_MS - 380_000,
    entry_reference: "0.8412",
    exchange_id: "paper",
    exit_attempt_total: 0,
    exit_price: null,
    exit_reason: null,
    filled_quantity: "237.6",
    mode: "paper",
    must_close_at_ms: TRADING_NOW_MS + 3 * 3_600_000,
    notional_usd: "200",
    order_id: "order-wif",
    policy_decision: "long",
    policy_reason: null,
    position_closed_at_ms: null,
    position_opened_at_ms: TRADING_NOW_MS - 360_000,
    // The two frozen case facts an order row now carries too (#282). Present by default for the same
    // reason the case fixture carries them: every surface that explains a case reads them.
    pre_move_bps: 187,
    /*
     * `news_oi_alignment_v1`'s own four keys, per `root.py`'s `_exact_keys`. Each strategy freezes a
     * disjoint set, so a fixture pairing one lane's id with another's config describes a case the loader
     * would refuse — and every console surface that explains a case reads this map.
     */
    strategy_config: {
      allow_short: "False",
      live_max_price_in: "1",
      live_min_surprise: "2",
      min_whale_long_profit_bps: "9500",
    },
    provider_attempt_count: 1,
    provider_symbol: "WIFUSDT",
    quantity: "237.6",
    realized_bps: null,
    regime: "buildup_up",
    // `assess()` writes this when the regime *is* one of the four; the unclear reasons are the ones a
    // console has to name, and `policy_reason` is null on a Case the strategy went on to trade (#282).
    regime_reason: "quadrant",
    side: "buy",
    state: "OPEN",
    state_reason: null,
    stop_price: "0.8244",
    take_profit_price: null,
    underlying_key: "crypto:WIF",
    updated_at_ms: TRADING_NOW_MS - 60_000,
    ...overrides,
  };
}

export function tradingCaseFixture(overrides: Partial<TradingCase> = {}): TradingCase {
  return {
    base_symbol: "HYPE",
    case_id: "case-hype",
    event_id: "evt-oi-hype",
    strategy_id: "oi_smart_money_momentum_v1",
    strategy_version: "oi_smart_money_momentum_v1",
    trigger_kind: "oi",
    created_at_ms: TRADING_NOW_MS - 500_000,
    decided_at_ms: TRADING_NOW_MS - 499_000,
    mode: "paper",
    observed_at_ms: TRADING_NOW_MS - 501_000,
    policy_decision: "no_trade",
    policy_reason: "whale_profit_below_floor",
    /*
     * The frozen pre-move and the thresholds it was measured against (#273, #282). Present by default
     * because every surface that explains a case reads them, and a fixture without them exercises only
     * the 未冻结 path — which is what the token page's 交易视角 baseline froze on its first run.
     *
     * Seven keys, matching `oi_smart_money_momentum_v1` exactly: `root.py`'s `_exact_keys` gives each
     * strategy a disjoint set, and a case carrying one lane's id over another's config is a row the
     * loader would refuse.
     */
    pre_move_bps: 187,
    strategy_config: {
      allow_short: "False",
      max_price_move_bps: "1000",
      measurement_window_ms: "300000",
      min_oi_change_bps: "500",
      min_price_move_bps: "0",
      min_whale_long_profit_bps: "9500",
      min_whale_oi_ratio_bps: "5000",
    },
    regime: "buildup_up",
    regime_reason: "quadrant",
    state: "POLICY_REJECTED",
    underlying_key: "crypto:HYPE",
    ...overrides,
  };
}

/** The evidence document's own defaults, so a fixture states only the keys its rule compared. */
export function gateEvidence(
  overrides: Partial<NonNullable<TradingGateDecision["gate_evidence"]>> = {},
): NonNullable<TradingGateDecision["gate_evidence"]> {
  return {
    blacklist_reason: "",
    rule: "",
    source_decision: "drop",
    source_rule: "opening_move_with_whale_concentration",
    venue: "binance",
    ...overrides,
  };
}

/**
 * The admission ledger's window (#269): one answer per source, the refusals included.
 *
 * The refusals are the point of the fixture. Production's whole 24 h is refusals — 87 of them against one
 * case — and a fixture that only held admitted frames would let a page that cannot render a named refusal
 * pass its tests.
 */
export function tradingGateDecisionFixture(
  overrides: Partial<TradingGateDecision> = {},
): TradingGateDecision {
  return {
    base_symbol: "STORJ",
    case_id: null,
    event_id: "evt-oi-storj",
    gate_attempt_count: 30,
    gate_config_digest: "c".repeat(64),
    gate_evidence: gateEvidence({
      floor: 5_000_000,
      oi_value_usd: 3_190_000,
      source_decision: "drop",
      source_rule: "whale_ratio_below_threshold",
      whale_oi_ratio_bps: 6_593,
    }),
    gate_first_evaluated_at_ms: TRADING_NOW_MS - 119_000,
    gate_last_evaluated_at_ms: TRADING_NOW_MS - 60_000,
    gate_reason: "oi_value_below_floor",
    gate_retryable: false,
    gate_stage: "eligibility",
    gate_status: "REJECTED",
    gate_version: "trading_candidate_gate_v1",
    source_key: "oi:evt-oi-storj:oi_signal_v1",
    source_observed_at_ms: TRADING_NOW_MS - 120_000,
    trigger_kind: "oi",
    underlying_key: "crypto:STORJ",
    ...overrides,
  };
}

export function tradingGateFixture(overrides: Partial<TradingGate> = {}): TradingGate {
  return {
    complete: true,
    decisions: [
      tradingGateDecisionFixture(),
      // A Binance stock perpetual this crypto-only lane can never route, closed by the clock while it
      // waited — and still naming the instrument it was waiting for (#268).
      tradingGateDecisionFixture({
        base_symbol: "NVDA",
        event_id: "evt-oi-nvda",
        gate_evidence: gateEvidence({ oi_value_usd: 63_700_000 }),
        gate_reason: "no_native_perp",
        gate_stage: "routing",
        gate_status: "EXPIRED",
        source_key: "oi:evt-oi-nvda:oi_signal_v1",
        underlying_key: "crypto:NVDA",
      }),
      tradingGateDecisionFixture({
        base_symbol: "HYPE",
        case_id: "case-hype",
        event_id: "evt-oi-hype",
        gate_evidence: gateEvidence({ oi_value_usd: 45_200_000 }),
        gate_reason: "case_created",
        gate_retryable: false,
        gate_stage: "freeze",
        gate_status: "CASE_CREATED",
        source_key: "oi:evt-oi-hype:oi_signal_v1",
        underlying_key: "crypto:HYPE",
      }),
    ],
    measured_at_ms: TRADING_NOW_MS,
    window_hours: 24,
    ...overrides,
  };
}

export function tradingOrdersFixture(overrides: Partial<TradingOrders> = {}): TradingOrders {
  return {
    cases_without_orders: [tradingCaseFixture()],
    complete: true,
    measured_at_ms: TRADING_NOW_MS,
    orders: [
      /*
       * The token page reads this one (`crypto:WIF`), and 交易视角 explains the deterministic OI lane —
       * the only lane whose frozen config carries a price band. The shared default above stays on the
       * News trigger because the 杠杆异动 funnel counts on it.
       */
      tradingOrderFixture({
        strategy_id: "oi_smart_money_momentum_v1",
        strategy_version: "oi_smart_money_momentum_v1",
        trigger_kind: "oi",
        strategy_config: {
          allow_short: "False",
          max_price_move_bps: "1000",
          measurement_window_ms: "300000",
          min_oi_change_bps: "500",
          min_price_move_bps: "0",
          min_whale_long_profit_bps: "9500",
          min_whale_oi_ratio_bps: "5000",
        },
      }),
      tradingOrderFixture({
        average_price: null,
        base_symbol: "HYPE",
        case_id: "case-hype-order",
        event_id: "evt-oi-hype-order",
        filled_quantity: null,
        order_id: "order-hype",
        provider_symbol: "HYPEUSDT",
        side: "sell",
        state: "AWAITING_APPROVAL",
        strategy_id: "news_oi_alignment_v1",
        underlying_key: "crypto:HYPE",
      }),
      tradingOrderFixture({
        average_price: null,
        base_symbol: "DOGE",
        case_id: "case-doge",
        event_id: "evt-oi-doge",
        filled_quantity: null,
        order_id: "order-doge",
        position_opened_at_ms: null,
        must_close_at_ms: null,
        provider_symbol: "DOGEUSDT",
        state: "ACKNOWLEDGED",
        strategy_id: "oi_momentum_v1",
        underlying_key: "crypto:DOGE",
      }),
      tradingOrderFixture({
        average_price: null,
        base_symbol: "SOL",
        case_id: "case-sol",
        event_id: "evt-oi-sol",
        filled_quantity: null,
        order_id: "order-sol",
        position_opened_at_ms: null,
        must_close_at_ms: null,
        provider_symbol: "SOLUSDT",
        state: "AMBIGUOUS",
        strategy_id: "news_oi_alignment_v1",
        underlying_key: "crypto:SOL",
      }),
      tradingOrderFixture({
        base_symbol: "BTC",
        case_id: "case-btc-closed",
        event_id: "evt-oi-btc-closed",
        exit_price: "1.0040",
        exit_reason: "max_holding",
        order_id: "order-btc-closed",
        position_closed_at_ms: TRADING_NOW_MS - 60_000,
        position_opened_at_ms: TRADING_NOW_MS - 7_200_000,
        realized_bps: 52,
        provider_symbol: "BTCUSDT",
        state: "CLOSED",
        strategy_id: "oi_momentum_v1",
        underlying_key: "crypto:BTC",
      }),
      tradingOrderFixture({
        base_symbol: "PEPE",
        case_id: "case-pepe-closed",
        event_id: "evt-oi-pepe-closed",
        exit_price: "0.8120",
        exit_reason: "native_stop",
        order_id: "order-pepe-closed",
        position_closed_at_ms: TRADING_NOW_MS - 120_000,
        position_opened_at_ms: TRADING_NOW_MS - 4_200_000,
        provider_symbol: "PEPEUSDT",
        realized_bps: -200,
        state: "CLOSED",
        strategy_id: "news_oi_alignment_v1",
        underlying_key: "crypto:PEPE",
      }),
    ],
    window_hours: 24,
    ...overrides,
  };
}

/**
 * The orders batch as the endpoint answers it for one underlying (#282).
 *
 * `/api/trading/orders` accepts `underlying` and filters both halves by it, so a mock that ignored the
 * parameter handed the WIF token page a HYPE case — whose `event_id` matches no frame that page loaded, so
 * 交易视角 rendered its "no frame" path on every baseline and froze the panel in the state it says least
 * in. Filtering here is what the server does, spelled out.
 */
export function tradingOrdersForUnderlying(underlying: string | null): TradingOrders {
  const batch = tradingOrdersFixture();
  if (!underlying) return batch;
  const key = underlying.includes(":") ? underlying : `crypto:${underlying.toUpperCase()}`;
  return {
    ...batch,
    cases_without_orders: (batch.cases_without_orders ?? []).filter(
      (row) => row.underlying_key === key,
    ),
    orders: (batch.orders ?? []).filter((row) => row.underlying_key === key),
  };
}
