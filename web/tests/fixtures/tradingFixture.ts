import type {
  TradingCase,
  TradingOrder,
  TradingOrders,
  TradingStatus,
} from "@features/trading/api/tradingQueries";

export const TRADING_NOW_MS = 1_779_000_000_000;

/**
 * The capital lane as it is actually configured today: `enabled: false`, `mode: paper`,
 * `live_readiness: not_applicable`. A fixture that shipped an enabled live lane would let a page pass its
 * tests in a state this deployment has never been in — and would hide the empty-state copy that is, right
 * now, the only thing most readers will see.
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
      closed_orders: 2,
      closed_realized_bps: 12,
      // A UTC calendar-day counter, named for the interval it covers — `merge_funnel` resets it
      // on `day_key`, unlike every rolling count beside it.
      funnel_day_key: "2026-08-25",
      funnel_today: { case_created: 9 },
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
          promotion_ready: false,
          mean_return_bps: 25,
        },
        liquidation_exhaustion_shadow_v1: {
          completed: 1,
          evaluated: 2,
          holdout: 2,
          source_contract_complete: 0,
          coverage_bps: 5_000,
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
    measured_at_ms: TRADING_NOW_MS,
    readiness: {
      control: "RUNNING",
      enabled: false,
      execution_backend: "disabled",
      execution_configured: false,
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
    policy_decision: "trade",
    policy_reason: null,
    position_closed_at_ms: null,
    position_opened_at_ms: TRADING_NOW_MS - 360_000,
    provider_attempt_count: 1,
    provider_symbol: "WIFUSDT",
    quantity: "237.6",
    realized_bps: null,
    regime: "buildup_up",
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
    strategy_id: "oi_momentum_v1",
    strategy_version: "oi_momentum_v1",
    trigger_kind: "oi",
    created_at_ms: TRADING_NOW_MS - 500_000,
    decided_at_ms: TRADING_NOW_MS - 499_000,
    mode: "paper",
    observed_at_ms: TRADING_NOW_MS - 501_000,
    policy_decision: "no_trade",
    policy_reason: "whale_profit_below_floor",
    regime: "buildup_up",
    state: "POLICY_REJECTED",
    underlying_key: "crypto:HYPE",
    ...overrides,
  };
}

export function tradingOrdersFixture(overrides: Partial<TradingOrders> = {}): TradingOrders {
  return {
    cases_without_orders: [tradingCaseFixture()],
    measured_at_ms: TRADING_NOW_MS,
    orders: [tradingOrderFixture()],
    window_hours: 24,
    ...overrides,
  };
}
