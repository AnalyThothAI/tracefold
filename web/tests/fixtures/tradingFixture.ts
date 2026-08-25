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
      max_hold_ms: 4 * 3_600_000,
      max_orders_per_day: 4,
      nominal_daily_stop_loss_usd: "4",
      notional_usd: "200",
      orders_today: 3,
      stop_loss_bps: 200,
    },
    counts: {
      cases_by_kind: { news_oi: 5, oi_only: 4 },
      cases_by_state: { NO_TRADE: 2, ORDER_PREPARED: 3, POLICY_REJECTED: 4 },
      closed_orders: 2,
      closed_realized_bps: 12,
      funnel_24h: { case_created: 9 },
      orders_by_state: { CLOSED: 2, OPEN: 1 },
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
    case_kind: "news_oi",
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
    case_kind: "oi_only",
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
