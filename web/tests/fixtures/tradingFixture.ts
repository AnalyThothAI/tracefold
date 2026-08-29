import type {
  TradingCase,
  TradingCases,
  TradingGate,
  TradingGateDecision,
  TradingIntent,
  TradingIntents,
  TradingStatus,
} from "@features/trading/api/tradingQueries";

export const TRADING_NOW_MS = Date.parse("2026-08-25T12:00:00Z");
export const CAPITAL_POLICY_ID = "binance_oi_smart_money_long_v2";

/**
 * One fixture per durable aggregate (#331), because one route per durable aggregate.
 *
 * The mixed `TradingIntents` fixture this replaces carried `cases_without_intents`, which let a test
 * assert Case behaviour through the Intent contract — exactly the coupling the split removes.
 */
export function tradingStatusFixture(overrides: Partial<TradingStatus> = {}): TradingStatus {
  return {
    budget: { max_entries_per_utc_day: 1, target_notional_usd: "10" },
    counts: {
      active_intents: 1,
      cases_24h: 7,
      closed_intents_today: 2,
      day_key: "2026-08-25",
      entries_today: 1,
      intents_24h: 3,
      latest_case_created_at_ms: TRADING_NOW_MS - 3_600_000,
      latest_entry_fenced_at_ms: TRADING_NOW_MS - 3_400_000,
      latest_intent_emitted_at_ms: TRADING_NOW_MS - 3_500_000,
      latest_position_closed_at_ms: TRADING_NOW_MS - 60_000,
      latest_position_opened_at_ms: TRADING_NOW_MS - 3_400_000,
    },
    measured_at_ms: TRADING_NOW_MS,
    policy: {
      config: {
        max_price_move_bps: "1000",
        measurement_window_ms: "300000",
        min_oi_change_bps: "500",
        min_price_move_bps: "0",
        min_whale_long_profit_bps: "0",
        min_whale_oi_ratio_bps: "5000",
      },
      config_digest: "a".repeat(64),
      policy_id: CAPITAL_POLICY_ID,
      policy_version: CAPITAL_POLICY_ID,
    },
    readiness: {
      control: "RUNNING",
      credentials_configured: true,
      enabled: true,
      engine_readiness_reason: null,
      engine_ready: true,
      execution_authority: "nautilus",
      execution_environment: "BINANCE_USDM_DEMO",
      heartbeat_at_ms: TRADING_NOW_MS - 1_000,
      active_capability_snapshot_sha256: "c".repeat(64),
      active_capability_included_count: 2,
      blacklist_revision: 3,
      unexpected_exposure: false,
    },
    window_hours: 24,
    ...overrides,
  };
}

export function tradingIntentFixture(overrides: Partial<TradingIntent> = {}): TradingIntent {
  return {
    actual_quantity: "0.05",
    avg_entry_price: "200",
    avg_exit_price: null,
    base_symbol: "SOL",
    case_id: "case-sol",
    closed_at_ms: null,
    commissions_by_currency: null,
    created_at_ms: TRADING_NOW_MS - 380_000,
    entry_fenced_at_ms: TRADING_NOW_MS - 370_000,
    event_id: "evt-oi-sol",
    execution_environment: "BINANCE_USDM_DEMO",
    execution_capability_snapshot_sha256: "c".repeat(64),
    execution_phase: "PROTECTION",
    execution_state: "OPEN_PROTECTED",
    flat_verified_at_ms: null,
    instrument_id: "SOLUSDT-PERP.BINANCE",
    intent_version: "trade_intent_v2",
    blacklist_revision_at_emission: 3,
    blacklist_snapshot_sha256_at_emission: "d".repeat(64),
    intent_id: "intent-sol",
    opened_at_ms: TRADING_NOW_MS - 360_000,
    policy_id: CAPITAL_POLICY_ID,
    policy_version: CAPITAL_POLICY_ID,
    protected_at_ms: TRADING_NOW_MS - 350_000,
    protected_quantity: "0.05",
    realized_pnl_amount: null,
    realized_pnl_currency: null,
    reason_code: null,
    reference_price: "200",
    side: "long",
    stop_price: "196",
    target_notional_usd: "10",
    terminal_outcome: null,
    underlying_key: "crypto:SOL",
    updated_at_ms: TRADING_NOW_MS - 60_000,
    valid_until_ms: TRADING_NOW_MS + 60_000,
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
    intent_id: null,
    manifest_version: "trading_manifest_v7",
    mark_price: "0.0950",
    observed_at_ms: TRADING_NOW_MS - 501_000,
    oi_change_bps: 1_548,
    oi_value_usd: 23_010_000,
    // The Case's own frozen thresholds, deliberately different from the running policy's, so a test can
    // prove the page renders the Case rather than today's configuration.
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
    policy_id: CAPITAL_POLICY_ID,
    policy_reason: "smart_money_ratio_below_or_equal_floor",
    policy_version: CAPITAL_POLICY_ID,
    pre_move_bps: 187,
    provider_symbol: "HYPEUSDT",
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
    reason_counts_24h: { smart_money_ratio_below_or_equal_floor: 4 },
    state_counts_24h: { BLOCKED: 1, INTENT_EMITTED: 1, NO_TRADE: 5 },
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

export function gateEvidence(
  overrides: Partial<NonNullable<TradingGateDecision["gate_evidence"]>> = {},
): NonNullable<TradingGateDecision["gate_evidence"]> {
  return {
    blacklist_reason: "",
    enabled: [],
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
    gate_version: "trading_admission_v2",
    research_only: false,
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
    live_exchange_id: "binance",
    max_age_ms: 300_000,
    max_rank_in_window: 2,
    min_oi_value_usd: 5_000_000,
    symbol_cooldown_ms: 1_800_000,
    version: "trading_admission_v2",
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

export function tradingIntentsFixture(overrides: Partial<TradingIntents> = {}): TradingIntents {
  return {
    complete: true,
    intents: [tradingIntentFixture()],
    measured_at_ms: TRADING_NOW_MS,
    outcome_counts_24h: { CLOSED_FLAT: 2 },
    reason_counts_24h: {},
    state_counts_24h: { OPEN_PROTECTED: 1, TERMINAL: 2 },
    window_hours: 24,
    ...overrides,
  };
}

export function tradingIntentsForUnderlying(underlying: string | null): TradingIntents {
  const batch = tradingIntentsFixture();
  if (!underlying) return batch;
  const key = underlying.includes(":") ? underlying : `crypto:${underlying.toUpperCase()}`;
  return { ...batch, intents: (batch.intents ?? []).filter((row) => row.underlying_key === key) };
}
