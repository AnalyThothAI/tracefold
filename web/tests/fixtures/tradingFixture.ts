import type {
  TradingCase,
  TradingCases,
  TradingExecutionCommand,
  TradingExecutionReadiness,
  TradingExecutionRow,
  TradingExecutions,
  TradingStatus,
} from "@features/trading/api/tradingQueries";

export const TRADING_NOW_MS = Date.parse("2026-08-25T12:00:00Z");
export const ALPHA_POLICY_ID = "source_native_oi_smart_money_long_v5";

/**
 * The desk's fixtures, in the shapes the real endpoints return.
 *
 * The base status is `mode=disabled`, which is the one state where the projection publishes no
 * `facts_expire_at_ms` at all: with no Runtime state there is no budget to expire. Every fixture that
 * turns the lane on carries the instant too, because a live projection always has one.
 */
export function tradingStatusFixture(overrides: Partial<TradingStatus> = {}): TradingStatus {
  return {
    decision: {
      last_case_at_ms: TRADING_NOW_MS - 1_000,
    },
    execution: {
      account_flat_proven: false,
      account_slot: "binance_usdm_primary",
      alive: false,
      emergency_halted: false,
      entries_armed: false,
      entries_paused: true,
      entry_block_reason: "disabled",
      execution_safe: false,
      facts_expire_at_ms: null,
      mode: "disabled",
      protection_status: "unknown",
      routes_count: 0,
      startup_reconciled: false,
      unexpected_exposure: false,
    },
    ...overrides,
  };
}

export function tradingExecutionFixture(
  overrides: Partial<TradingExecutionReadiness> = {},
): TradingExecutionReadiness {
  return { ...tradingStatusFixture().execution, ...overrides };
}

/** A live paper Runtime whose facts are still inside their published budget. */
export function tradingLiveExecutionFixture(
  overrides: Partial<TradingExecutionReadiness> = {},
): TradingExecutionReadiness {
  return tradingExecutionFixture({
    alive: true,
    current_account: tradingCurrentAccountFixture(),
    entries_armed: false,
    entries_paused: true,
    entry_block_reason: "entries_paused",
    execution_safe: true,
    facts_expire_at_ms: TRADING_NOW_MS + 5_000,
    mode: "paper",
    protection_status: "protected",
    reconciliation_age_ms: 1_000,
    routes_count: 12,
    startup_reconciled: true,
    ...overrides,
  });
}

export function tradingCurrentAccountFixture(
  overrides: Partial<NonNullable<TradingExecutionReadiness["current_account"]>> = {},
): NonNullable<TradingExecutionReadiness["current_account"]> {
  return {
    aggregate_risk_usd: "9.9995",
    audit_healthy: true,
    complete: true,
    daily_drawdown_bps: 25,
    daily_drawdown_usd: "2.50",
    equity_usd: "997.50",
    inflight_orders_count: 0,
    open_orders_count: 1,
    orders: [
      {
        client_order_id: "stop-order-1",
        instrument_id: "BTCUSDT-PERP.BINANCE",
        leg: "protection",
        owned: true,
        quantity: "0.05",
        reduce_only: true,
        state: "open",
        trigger_price: "9800",
      },
    ],
    positions: [
      {
        entry_price: "10000",
        instrument_id: "BTCUSDT-PERP.BINANCE",
        mark_price: "9999.5",
        owned: true,
        position_id: "position-1",
        protection_full_coverage: true,
        protection_quantity: "0.05",
        protection_status: "protected",
        protection_trigger_price: "9800",
        quantity: "0.05",
        side: "long",
        unrealized_pnl_usd: "-0.025",
      },
    ],
    unknown_orders_count: 0,
    ...overrides,
  };
}

export function tradingCaseFixture(overrides: Partial<TradingCase> = {}): TradingCase {
  return {
    base_symbol: "HYPE",
    case_id: "case-hype",
    created_at_ms: TRADING_NOW_MS - 500_000,
    decided_at_ms: TRADING_NOW_MS - 499_000,
    event_id: "evt-oi-hype",
    manifest_version: "trading_manifest_v10",
    mark_price: "0.0950",
    market_key: "crypto:perp:HYPE:USDT",
    observed_at_ms: TRADING_NOW_MS - 501_000,
    policy_checks: [
      {
        check: "whale_oi_ratio_bps",
        measured: "5424",
        operator: ">",
        passed: false,
        threshold: "8000",
      },
    ],
    policy_config: { max_price_move_bps: "600", min_whale_oi_ratio_bps: "8000" },
    policy_config_digest: "e".repeat(64),
    policy_id: ALPHA_POLICY_ID,
    policy_reason: "smart_money_ratio_below_or_equal_floor",
    pre_move_bps: 187,
    state: "NO_TRADE",
    ...overrides,
  };
}

export function tradingCasesFixture(overrides: Partial<TradingCases> = {}): TradingCases {
  return {
    cases: [tradingCaseFixture()],
    complete: true,
    reason_counts_24h: { smart_money_ratio_below_or_equal_floor: 4 },
    state_counts_24h: { BLOCKED: 1, NO_TRADE: 5, SIGNAL_EMITTED: 1 },
    window_hours: 24,
    ...overrides,
  };
}

/** `?underlying=` is still a bounded server filter; `base_symbol` is what the response identifies a row by. */
export function tradingCasesForUnderlying(underlying: string | null): TradingCases {
  const batch = tradingCasesFixture();
  if (!underlying) return batch;
  const base = underlying.split(":").pop()?.toUpperCase() ?? "";
  return { ...batch, cases: (batch.cases ?? []).filter((row) => row.base_symbol === base) };
}

/**
 * One entry that ran to the end: entered, protected, and flattened out with a realized number on it.
 *
 * The shape is `console_executions_statement`'s own — a `closed` position carries the quantity it held
 * before the close, the exit price and PnL Nautilus reported on `PositionClosed`, and the `exit_reason`
 * the coordinator that closed it stamped. `source` says which entry identity `entry_id` is.
 */
export function tradingExecutionRowFixture(
  overrides: Partial<TradingExecutionRow> = {},
): TradingExecutionRow {
  return {
    case_id: "case-btc",
    direction: "long",
    disposition_reason: "accepted",
    entry_id: "1".repeat(64),
    exit_price: "9699.0",
    exit_reason: "flatten",
    fill_avg_price: "10000",
    fill_quantity: "0.049",
    market_key: "crypto:perp:BTC:USDT",
    observed_at_ns: (TRADING_NOW_MS - 120_000) * 1_000_000,
    realized_pnl_usd: "-14.92274518",
    source: "signal",
    stage: "closed",
    stop_trigger_price: "9800",
    ...overrides,
  };
}

export function tradingExecutionsFixture(
  overrides: Partial<TradingExecutions> = {},
): TradingExecutions {
  return {
    commands: [
      tradingCommandRowFixture(),
      tradingCommandRowFixture({
        action: "flatten",
        command_id: "b".repeat(64),
        requested_at_ns: (TRADING_NOW_MS - 60_000) * 1_000_000,
        stage: "completed",
      }),
    ],
    complete: true,
    executions: [
      tradingExecutionRowFixture(),
      /*
       * A Signal for a market no configured Runtime route lists. It never reached an order, so every
       * venue column is absent rather than zero.
       */
      tradingExecutionRowFixture({
        case_id: "case-nvda",
        direction: "long",
        disposition_reason: "instrument_unmapped",
        entry_id: "2".repeat(64),
        exit_price: null,
        exit_reason: null,
        fill_avg_price: null,
        fill_quantity: null,
        market_key: "crypto:perp:NVDA:USDT",
        observed_at_ns: (TRADING_NOW_MS - 300_000) * 1_000_000,
        realized_pnl_usd: null,
        stage: "rejected",
        stop_trigger_price: null,
      }),
      // A Signal whose TTL ran out before the Runtime could act on it.
      tradingExecutionRowFixture({
        case_id: "case-sol",
        disposition_reason: "expired",
        entry_id: "3".repeat(64),
        exit_price: null,
        exit_reason: null,
        fill_avg_price: null,
        fill_quantity: null,
        market_key: "crypto:perp:SOL:USDT",
        observed_at_ns: (TRADING_NOW_MS - 600_000) * 1_000_000,
        realized_pnl_usd: null,
        stage: "expired",
        stop_trigger_price: null,
      }),
      /*
       * The CLI manual entry from the same window: the operator's own `/long`, keyed on the Command
       * that opened it, with no Case behind it (#528 PR-3).
       */
      tradingExecutionRowFixture({
        case_id: null,
        direction: "short",
        entry_id: "e".repeat(64),
        exit_price: "81100.0",
        fill_avg_price: "81126.9",
        fill_quantity: "0.0122",
        market_key: "crypto:perp:ETH:USDT",
        observed_at_ns: (TRADING_NOW_MS - 90_000) * 1_000_000,
        realized_pnl_usd: "-1.11984726",
        source: "manual",
        stop_trigger_price: "80315.6",
      }),
    ],
    ...overrides,
  };
}

export function tradingCommandRowFixture(
  overrides: Partial<TradingExecutionCommand> = {},
): TradingExecutionCommand {
  return {
    action: "pause_entries",
    command_id: "9".repeat(64),
    requested_at_ns: TRADING_NOW_MS * 1_000_000,
    stage: "recorded",
    ...overrides,
  };
}
