import type {
  TradingCase,
  TradingGate,
  TradingGateDecision,
  TradingIntent,
  TradingIntents,
  TradingStatus,
} from "@features/trading/api/tradingQueries";

export const TRADING_NOW_MS = Date.parse("2026-08-25T12:00:00Z");

export function tradingStatusFixture(overrides: Partial<TradingStatus> = {}): TradingStatus {
  return {
    budget: { max_entries_per_utc_day: 1, target_notional_usd: "10" },
    counts: {
      active_intents: 1,
      candidate_counts_24h: { CASE_CREATED: 1, REJECTED: 87 },
      candidate_counts_7d: { CASE_CREATED: 4, REJECTED: 392 },
      candidate_reasons_24h: {
        "eligibility:oi_value_below_floor": 22,
        "eligibility:rank_above_limit": 65,
        "freeze:case_created": 1,
      },
      candidate_reasons_7d: { "eligibility:rank_above_limit": 300 },
      cases_by_state: { INTENT_EMITTED: 3, POLICY_REJECTED: 4 },
      cases_by_strategy: { news_oi_alignment_v1: 5, oi_momentum_v1: 2 },
      cases_by_trigger: { news: 5, oi: 2 },
      cases_today_by_state: { INTENT_EMITTED: 1, POLICY_REJECTED: 4 },
      closed_intents_today: 2,
      entries_today: 1,
      funnel_day_key: "2026-08-25",
      funnel_today: { case_created: 5 },
      intents_by_state: { OPEN_PROTECTED: 1, TERMINAL: 2 },
      latest_case_created_at_ms: TRADING_NOW_MS - 3_600_000,
      latest_entry_fenced_at_ms: TRADING_NOW_MS - 3_400_000,
      latest_gate_eligible_at_ms: TRADING_NOW_MS - 3_600_000,
      latest_intent_emitted_at_ms: TRADING_NOW_MS - 3_500_000,
      latest_position_closed_at_ms: TRADING_NOW_MS - 60_000,
      latest_position_opened_at_ms: TRADING_NOW_MS - 3_400_000,
      latest_source_at_ms: TRADING_NOW_MS - 60_000,
      liquidation_promotion_ready: false,
      liquidation_promotion_reason: "source_contract_incomplete",
      outcomes_by_state: { CLOSED_FLAT: 2 },
      policy_allowed_24h: 3,
      policy_allowed_today: 1,
    },
    floors: {
      lookback_ms: 3_600_000,
      max_price_move_bps: 600,
      min_oi_value_usd: "20000000",
      min_price_move_bps: 100,
      min_whale_long_profit_bps: 9_500,
    },
    gate: {
      config_digest: "c".repeat(64),
      max_age_ms: 300_000,
      max_rank_in_window: 2,
      min_oi_value_usd: 5_000_000,
      symbol_cooldown_ms: 1_800_000,
      venue_priority: ["binance", "hyperliquid"],
      version: "trading_candidate_gate_v1",
    },
    measured_at_ms: TRADING_NOW_MS,
    readiness: {
      control: "RUNNING",
      credentials_configured: true,
      enabled: true,
      engine_readiness_reason: null,
      engine_ready: true,
      execution_authority: "nautilus",
      execution_environment: "BINANCE_USDM_DEMO",
      heartbeat_at_ms: TRADING_NOW_MS - 1_000,
      instrument_id: "SOLUSDT-PERP.BINANCE",
      unexpected_exposure: false,
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
    ],
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
    case_observed_at_ms: TRADING_NOW_MS - 400_000,
    case_state: "INTENT_EMITTED",
    closed_at_ms: null,
    commissions_by_currency: null,
    created_at_ms: TRADING_NOW_MS - 380_000,
    event_id: "evt-oi-sol",
    execution_environment: "BINANCE_USDM_DEMO",
    execution_phase: "PROTECTION",
    execution_state: "OPEN_PROTECTED",
    flat_verified_at_ms: null,
    instrument_id: "SOLUSDT-PERP.BINANCE",
    intent_id: "intent-sol",
    opened_at_ms: TRADING_NOW_MS - 360_000,
    policy_decision: "long",
    policy_reason: null,
    pre_move_bps: 187,
    protected_at_ms: TRADING_NOW_MS - 350_000,
    protected_quantity: "0.05",
    realized_pnl_amount: null,
    realized_pnl_currency: null,
    reason_code: null,
    reference_price: "200",
    regime: "buildup_up",
    regime_reason: "quadrant",
    side: "long",
    stop_price: "196",
    strategy_config: {
      allow_short: "False",
      max_price_move_bps: "1000",
      measurement_window_ms: "300000",
      min_oi_change_bps: "500",
      min_price_move_bps: "0",
      min_whale_long_profit_bps: "9500",
      min_whale_oi_ratio_bps: "5000",
    },
    strategy_id: "oi_smart_money_momentum_v1",
    strategy_version: "oi_smart_money_momentum_v1",
    target_notional_usd: "10",
    terminal_outcome: null,
    trigger_kind: "oi",
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
    observed_at_ms: TRADING_NOW_MS - 501_000,
    policy_decision: "no_trade",
    policy_reason: "whale_profit_below_floor",
    pre_move_bps: 187,
    regime: "buildup_up",
    regime_reason: "quadrant",
    state: "POLICY_REJECTED",
    strategy_config: {
      allow_short: "False",
      max_price_move_bps: "1000",
      measurement_window_ms: "300000",
      min_oi_change_bps: "500",
      min_price_move_bps: "0",
      min_whale_long_profit_bps: "9500",
      min_whale_oi_ratio_bps: "5000",
    },
    strategy_id: "oi_smart_money_momentum_v1",
    strategy_version: "oi_smart_money_momentum_v1",
    trigger_kind: "oi",
    underlying_key: "crypto:HYPE",
    ...overrides,
  };
}

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
    decisions: [tradingGateDecisionFixture()],
    measured_at_ms: TRADING_NOW_MS,
    window_hours: 24,
    ...overrides,
  };
}

export function tradingIntentsFixture(overrides: Partial<TradingIntents> = {}): TradingIntents {
  return {
    cases_without_intents: [tradingCaseFixture()],
    complete: true,
    intents: [tradingIntentFixture()],
    measured_at_ms: TRADING_NOW_MS,
    window_hours: 24,
    ...overrides,
  };
}

export function tradingIntentsForUnderlying(underlying: string | null): TradingIntents {
  const batch = tradingIntentsFixture();
  if (!underlying) return batch;
  const key = underlying.includes(":") ? underlying : `crypto:${underlying.toUpperCase()}`;
  return {
    ...batch,
    cases_without_intents: (batch.cases_without_intents ?? []).filter(
      (row) => row.underlying_key === key,
    ),
    intents: (batch.intents ?? []).filter((row) => row.underlying_key === key),
  };
}
