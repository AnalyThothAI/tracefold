import type {
  TradingCase,
  TradingCases,
  TradingExecutionObservation,
  TradingExecutionObservations,
  TradingExecutionReadiness,
  TradingGate,
  TradingGateDecision,
  TradingOperatorIntent,
  TradingOperatorIntents,
  TradingSignal,
  TradingSignals,
  TradingStatus,
} from "@features/trading/api/tradingQueries";

export const TRADING_NOW_MS = Date.parse("2026-08-25T12:00:00Z");
export const ALPHA_POLICY_ID = "source_native_oi_smart_money_long_v4";

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
    alpha_metadata: { policy_rule: "smart_money_momentum_long" },
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

export function tradingObservationFixture(
  overrides: Partial<TradingExecutionObservation> = {},
): TradingExecutionObservation {
  return {
    account_slot: "binance_usdm_primary",
    command_id: null,
    event_id: "e".repeat(64),
    execution_strategy: "oi-nautilus-v1",
    native_identity_references: ["order:test"],
    normalized_kind: "signal_disposition",
    observed_at_ns: TRADING_NOW_MS * 1_000_000,
    occurred_at_ns: (TRADING_NOW_MS - 1) * 1_000_000,
    runtime_release: "sha256:" + "1".repeat(64),
    seq: 1,
    signal_id: "d".repeat(64),
    summary: { disposition: "accepted" },
    ...overrides,
  };
}

export function tradingObservationsFixture(
  overrides: Partial<TradingExecutionObservations> = {},
): TradingExecutionObservations {
  return {
    complete: true,
    measured_at_ms: TRADING_NOW_MS,
    next_cursor: null,
    observations: [],
    window_hours: 24,
    ...overrides,
  };
}

export function tradingCommandFixture(
  overrides: Partial<TradingOperatorIntent> = {},
): TradingOperatorIntent {
  return {
    account_slot: "binance_usdm_primary",
    action: "pause_entries",
    command_id: "9".repeat(64),
    direction: null,
    disposition: null,
    disposition_reason: null,
    expired: false,
    expires_at_ns: (TRADING_NOW_MS + 300_000) * 1_000_000,
    market_key: null,
    operator_identity: "telegram:user:42",
    reason: "maintenance",
    requested_at_ns: TRADING_NOW_MS * 1_000_000,
    scope: "entries",
    seq: 1,
    ...overrides,
  };
}

export function tradingCommandsFixture(
  overrides: Partial<TradingOperatorIntents> = {},
): TradingOperatorIntents {
  return {
    commands: [],
    complete: true,
    measured_at_ms: TRADING_NOW_MS,
    next_cursor: null,
    window_hours: 24,
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
    source_decision: "drop",
    source_rule: "opening_move_with_whale_concentration",
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
    status_counts_24h: { CASE_CREATED: 1, REJECTED: 87, RESEARCH_ONLY: 4 },
    window_hours: 24,
    ...overrides,
  };
}
