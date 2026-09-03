import type {
  TradingCase,
  TradingCases,
  TradingExecutionCommand,
  TradingExecutionReadiness,
  TradingExecutionRow,
  TradingExecutions,
  TradingGate,
  TradingGateDecision,
  TradingSignal,
  TradingSignals,
  TradingStatus,
} from "@features/trading/api/tradingQueries";

export const TRADING_NOW_MS = Date.parse("2026-08-25T12:00:00Z");
export const ALPHA_POLICY_ID = "source_native_oi_smart_money_long_v4";

/**
 * The desk's fixtures, in the shapes the real endpoints return.
 *
 * The base status is `mode=disabled`, which is the one state where the projection publishes no
 * `facts_expire_at_ms` at all: with no Runtime state there is no budget to expire. Every fixture that
 * turns the lane on carries the instant too, because a live projection always has one.
 */
export function tradingStatusFixture(overrides: Partial<TradingStatus> = {}): TradingStatus {
  return {
    counts: {
      cases_24h: 7,
      signals_24h: 1,
    },
    decision: {
      last_case_at_ms: TRADING_NOW_MS - 1_000,
    },
    execution: {
      account_flat: false,
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
      open_orders_count: 0,
      positions_count: 0,
      protection_status: "unknown",
      routes_count: 0,
      startup_reconciled: false,
      unexpected_exposure: false,
    },
    measured_at_ms: TRADING_NOW_MS,
    window_hours: 24,
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
    heartbeat_at_ns: TRADING_NOW_MS * 1_000_000,
    mode: "paper",
    open_orders_count: 1,
    positions_count: 1,
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
    day_start_equity_usd: "1000",
    equity_usd: "997.50",
    inflight_orders_count: 0,
    market_observed_at_ns: TRADING_NOW_MS * 1_000_000,
    observed_at_ns: TRADING_NOW_MS * 1_000_000,
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
    truncated: false,
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
    oi_change_bps: 1_548,
    oi_value_usd: 23_010_000,
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
    policy_decision: "no_trade",
    policy_id: ALPHA_POLICY_ID,
    policy_reason: "smart_money_ratio_below_or_equal_floor",
    policy_version: ALPHA_POLICY_ID,
    pre_move_bps: 187,
    source_venue: "binance.usdm",
    state: "NO_TRADE",
    trigger_kind: "oi",
    underlying_key: "crypto:HYPE",
    whale_long_profit_bps: 9_074,
    whale_oi_ratio_bps: 5_424,
    ...overrides,
  };
}

export function tradingCasesFixture(overrides: Partial<TradingCases> = {}): TradingCases {
  return {
    cases: [tradingCaseFixture()],
    complete: true,
    measured_at_ms: TRADING_NOW_MS,
    next_cursor: null,
    reason_counts_24h: { smart_money_ratio_below_or_equal_floor: 4 },
    state_counts_24h: { BLOCKED: 1, NO_TRADE: 5, SIGNAL_EMITTED: 1 },
    window_hours: 24,
    ...overrides,
  };
}

export function tradingCasesForUnderlying(underlying: string | null): TradingCases {
  const batch = tradingCasesFixture();
  if (!underlying) return batch;
  const key = underlying.includes(":") ? underlying : `crypto:${underlying.toUpperCase()}`;
  return { ...batch, cases: (batch.cases ?? []).filter((row) => row.underlying_key === key) };
}

export function tradingSignalFixture(overrides: Partial<TradingSignal> = {}): TradingSignal {
  return {
    case_id: "case-sol",
    direction: "long",
    expired: false,
    expires_at_ns: (TRADING_NOW_MS + 180_000) * 1_000_000,
    market_key: "crypto:perp:SOL:USDT",
    observed_at_ns: TRADING_NOW_MS * 1_000_000,
    seq: 1,
    signal_id: "d".repeat(64),
    ...overrides,
  };
}

export function tradingSignalsFixture(overrides: Partial<TradingSignals> = {}): TradingSignals {
  return {
    complete: true,
    measured_at_ms: TRADING_NOW_MS,
    next_cursor: null,
    signals: [tradingSignalFixture()],
    window_hours: 24,
    ...overrides,
  };
}

export function tradingSignalsForMarket(market: string | null): TradingSignals {
  const batch = tradingSignalsFixture();
  if (!market) return batch;
  return { ...batch, signals: (batch.signals ?? []).filter((row) => row.market_key === market) };
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
    disposition: "accepted",
    disposition_reason: "accepted",
    entry_id: "1".repeat(64),
    exit_price: "9699.0",
    exit_reason: "flatten",
    fill_avg_price: "10000",
    fill_quantity: "0.049",
    last_observed_at_ns: (TRADING_NOW_MS - 30_000) * 1_000_000,
    market_key: "crypto:perp:BTC:USDT",
    observed_at_ns: (TRADING_NOW_MS - 120_000) * 1_000_000,
    order_status: "submitted",
    position_status: "closed",
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
        reason: "account",
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
        disposition: "rejected",
        disposition_reason: "instrument_unmapped",
        entry_id: "2".repeat(64),
        exit_price: null,
        exit_reason: null,
        fill_avg_price: null,
        fill_quantity: null,
        last_observed_at_ns: (TRADING_NOW_MS - 300_000) * 1_000_000,
        market_key: "crypto:perp:NVDA:USDT",
        observed_at_ns: (TRADING_NOW_MS - 300_000) * 1_000_000,
        order_status: null,
        position_status: null,
        realized_pnl_usd: null,
        stage: "rejected",
        stop_trigger_price: null,
      }),
      // A Signal whose TTL ran out before the Runtime could act on it.
      tradingExecutionRowFixture({
        case_id: "case-sol",
        disposition: "rejected",
        disposition_reason: "expired",
        entry_id: "3".repeat(64),
        exit_price: null,
        exit_reason: null,
        fill_avg_price: null,
        fill_quantity: null,
        last_observed_at_ns: (TRADING_NOW_MS - 600_000) * 1_000_000,
        market_key: "crypto:perp:SOL:USDT",
        observed_at_ns: (TRADING_NOW_MS - 600_000) * 1_000_000,
        order_status: null,
        position_status: null,
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
        last_observed_at_ns: (TRADING_NOW_MS - 20_000) * 1_000_000,
        market_key: "crypto:perp:ETH:USDT",
        observed_at_ns: (TRADING_NOW_MS - 90_000) * 1_000_000,
        realized_pnl_usd: "-1.11984726",
        source: "manual",
        stop_trigger_price: "80315.6",
      }),
    ],
    measured_at_ms: TRADING_NOW_MS,
    window_hours: 24,
    ...overrides,
  };
}

export function tradingCommandRowFixture(
  overrides: Partial<TradingExecutionCommand> = {},
): TradingExecutionCommand {
  return {
    action: "pause_entries",
    command_id: "9".repeat(64),
    operator_identity: "console:operator",
    reason: "maintenance",
    requested_at_ns: TRADING_NOW_MS * 1_000_000,
    stage: "recorded",
    ...overrides,
  };
}

export function gateEvidence(
  overrides: Partial<NonNullable<TradingGateDecision["gate_evidence"]>> = {},
): NonNullable<TradingGateDecision["gate_evidence"]> {
  return {
    blacklist_reason: "",
    enabled: [],
    holds: "",
    lane_full: "",
    live_exchange_id: "",
    rule: "",
    source_decision: "",
    source_rule: "",
    venue: "binance",
    ...overrides,
  };
}

export function tradingGateDecisionFixture(
  overrides: Partial<TradingGateDecision> = {},
): TradingGateDecision {
  return {
    base_symbol: "STORJ",
    case_id: null,
    event_id: "evt-oi-storj",
    gate_attempt_count: 1,
    gate_config_digest: "c".repeat(64),
    gate_evidence: gateEvidence({ floor: 5_000_000, oi_value_usd: 3_190_000 }),
    gate_first_evaluated_at_ms: TRADING_NOW_MS - 119_000,
    gate_last_evaluated_at_ms: TRADING_NOW_MS - 60_000,
    gate_reason: "oi_value_below_floor",
    gate_retryable: false,
    gate_stage: "eligibility",
    gate_status: "REJECTED",
    gate_version: "trading_admission_v6",
    source_key: "oi:evt-oi-storj:oi_signal_v1",
    source_observed_at_ms: TRADING_NOW_MS - 120_000,
    trigger_kind: "oi",
    underlying_key: "crypto:STORJ",
    ...overrides,
  };
}

export function tradingGateConfigFixture(): TradingGate["config"] {
  return {
    config_digest: "c".repeat(64),
    max_age_ms: 300_000,
    min_oi_value_usd: 5_000_000,
    source_venues: ["binance.usdm", "hyperliquid.perp", "hyperliquid.xyz"],
    version: "trading_admission_v6",
  };
}

export function tradingGateFixture(overrides: Partial<TradingGate> = {}): TradingGate {
  return {
    complete: true,
    config: tradingGateConfigFixture(),
    decisions: [tradingGateDecisionFixture()],
    latest_gate_eligible_at_ms: TRADING_NOW_MS - 3_600_000,
    latest_source_at_ms: TRADING_NOW_MS - 60_000,
    measured_at_ms: TRADING_NOW_MS,
    reason_counts_24h: { "eligibility:oi_value_below_floor": 22 },
    // The four answers `trading_admission` can file; there is no research bucket in the ledger.
    status_counts_24h: { CASE_CREATED: 1, DEFERRED: 3, REJECTED: 87 },
    window_hours: 24,
    ...overrides,
  };
}
