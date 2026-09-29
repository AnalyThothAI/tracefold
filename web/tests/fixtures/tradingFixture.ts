import type {
  TradingCase,
  TradingCases,
  TradingExecutionReadiness,
  TradingExecutionRow,
  TradingExecutions,
  TradingStatus,
  TradingScoreboard,
} from "@features/trading/api/tradingQueries";

export const TRADING_NOW_MS = Date.parse("2026-08-25T12:00:00Z");

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
      state: "disabled",
      active_policy: "forecast",
      model_name: null,
      publish_signals: false,
      config_digest: null,
      heartbeat_at_ms: null,
    },
    execution: {
      account_slot: "binance_usdm_primary",
      alive: false,
      emergency_halted: false,
      entries_armed: false,
      entries_paused: true,
      entry_block_reason: "disabled",
      facts_expire_at_ms: null,
      configured_connection: "LIVE",
      connection: null,
      connection_observed_at_ms: null,
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

/**
 * A live DEMO executor whose signed facts are still inside their published budget.
 */
export function tradingLiveExecutionFixture(
  overrides: Partial<TradingExecutionReadiness> = {},
): TradingExecutionReadiness {
  return tradingExecutionFixture({
    alive: true,
    signed_account: tradingSignedAccountFixture(),
    entries_armed: false,
    entries_paused: true,
    entry_block_reason: "entries_paused",
    facts_expire_at_ms: TRADING_NOW_MS + 5_000,
    facts_remaining_ms: 5_000,
    configured_connection: "DEMO",
    connection: "DEMO",
    connection_observed_at_ms: TRADING_NOW_MS,
    ...overrides,
  });
}

/** A bounded signed REST projection with a claimed position and two Algo guards. */
export function tradingSignedAccountFixture(
  overrides: Partial<NonNullable<TradingExecutionReadiness["signed_account"]>> = {},
): NonNullable<TradingExecutionReadiness["signed_account"]> {
  return {
    observed_at_ns: TRADING_NOW_MS * 1_000_000,
    equity_usdt: "997.50",
    complete: true,
    orders_total: overrides.orders?.length ?? 0,
    algos_total: overrides.algos?.length ?? 2,
    positions_total: overrides.positions?.length ?? 1,
    orders: [],
    algos: [
      {
        symbol: "BTCUSDT",
        clientAlgoId: "stop-order-1",
        orderType: "STOP_MARKET",
        triggerPrice: "9800",
        algoStatus: "NEW",
        owned: true,
      },
      {
        symbol: "BTCUSDT",
        clientAlgoId: "take-profit-order-1",
        orderType: "TAKE_PROFIT_MARKET",
        triggerPrice: "10200",
        algoStatus: "NEW",
        owned: true,
      },
    ],
    positions: [
      {
        symbol: "BTCUSDT",
        positionSide: "BOTH",
        positionAmt: "0.05",
        entryPrice: "10000",
        markPrice: "9999.5",
        unRealizedProfit: "-0.025",
        owned: true,
      },
    ],
    ...overrides,
  };
}

export const CASE_ID = "a".repeat(64);

export function tradingCaseFixture(overrides: Partial<TradingCase> = {}): TradingCase {
  return {
    case_id: CASE_ID,
    trigger_kind: "oi",
    asset_id: "crypto:HYPE",
    native_symbol: "HYPEUSDT",
    created_at_ms: TRADING_NOW_MS - 500_000,
    decided_at_ms: TRADING_NOW_MS - 499_000,
    state: "complete",
    failure_code: null,
    geometry_version: "leg_geometry_v1",
    view_sha256: "b".repeat(64),
    raw_snapshot_ref: "c".repeat(64),
    assessments: [],
    policy_actions: [],
    paper_legs: [],
    ...overrides,
  };
}

export function tradingCasesFixture(overrides: Partial<TradingCases> = {}): TradingCases {
  return { cases: [tradingCaseFixture()], total: 1, complete: true, ...overrides };
}

export function tradingCasesForCaseId(
  caseId: string | null,
  sourceItemId: string | null = null,
): TradingCases {
  if (sourceItemId && sourceItemId !== "mkt-oi-wif-3")
    return tradingCasesFixture({ cases: [], total: 0 });
  if (!caseId) return tradingCasesFixture();
  return tradingCasesFixture({
    cases: caseId === CASE_ID ? [tradingCaseFixture()] : [],
    total: caseId === CASE_ID ? 1 : 0,
  });
}

export function tradingScoreboardFixture(
  overrides: Partial<TradingScoreboard> = {},
): TradingScoreboard {
  return {
    window: { since_ms: TRADING_NOW_MS - 7 * 86_400_000, until_ms: TRADING_NOW_MS },
    funnel: {
      triggers: 12,
      selected: 10,
      assessed: 8,
      published: 1,
      execution_accepted: 1,
      filled: 1,
    },
    programs: [
      {
        program_sha: "d".repeat(64),
        route: "qwen",
        assessments: 8,
        failures: { timeout: 2 },
        policies: [
          "forecast",
          "always_long",
          "always_short",
          "abstain",
          "momentum15m",
          "fade15m",
        ].map((policy_id) => ({
          policy_id,
          policy_version: "policy_v1",
          cases: 10,
          actions: policy_id === "abstain" ? 0 : 6,
          scored: policy_id === "abstain" ? 0 : 6,
          coverage: policy_id === "abstain" ? "0" : "0.6",
          average_r: null,
          win_rate: null,
          ci_low: null,
          ci_high: null,
          status: "insufficient_data" as const,
        })),
        forecast: {
          legs: 12,
          multiclass_brier: null,
          log_loss: null,
          brier_skill_score: null,
          reliability: [],
          status: "insufficient_data",
        },
        execution_deviation: { scored: 1, average_r_delta: null, status: "insufficient_data" },
      },
    ],
    ...overrides,
  };
}

export function tradingExecutionRowFixture(
  overrides: Partial<TradingExecutionRow> = {},
): TradingExecutionRow {
  return {
    case_id: CASE_ID,
    direction: "long",
    disposition_reason: "accepted",
    entry_id: "1".repeat(64),
    exit_price: "9699.0",
    exit_reason: "operator_flatten",
    fill_avg_price: "10000",
    fill_quantity: "0.049",
    market_key: "crypto:perp:BTC:USDT",
    entry_filled_at_ns: (TRADING_NOW_MS - 118_000) * 1_000_000,
    observed_at_ns: (TRADING_NOW_MS - 120_000) * 1_000_000,
    position_closed_at_ns: (TRADING_NOW_MS - 25_500) * 1_000_000,
    realized_pnl_usd: "-14.749",
    fees_usd: "0.17374518",
    pnl_status: "complete",
    net_pnl_usd: overrides.net_pnl_usd !== undefined ? overrides.net_pnl_usd : "-14.92274518",
    stop_distance_bps: 200,
    take_profit_bps: 200,
    max_holding_ns: 14_400_000_000_000,
    source: "signal",
    stage: "closed",
    stop_trigger_price: "9800",
    take_profit_trigger_price: "10200",
    ...overrides,
  };
}

export function tradingExecutionsFixture(
  overrides: Partial<TradingExecutions> = {},
): TradingExecutions {
  return {
    complete: true,
    executions: [
      tradingExecutionRowFixture(),
      /*
       * A Signal admitted by the executor whose entry order the venue refused.
       */
      tradingExecutionRowFixture({
        case_id: "case-nvda",
        direction: "long",
        disposition_reason: "accepted",
        entry_error_code: -2019,
        entry_filled_at_ns: null,
        entry_id: "2".repeat(64),
        exit_price: null,
        exit_reason: "entry_rejected",
        fees_usd: null,
        net_pnl_usd: null,
        fill_avg_price: null,
        fill_quantity: null,
        market_key: "crypto:perp:NVDA:USDT",
        observed_at_ns: (TRADING_NOW_MS - 300_000) * 1_000_000,
        position_closed_at_ns: (TRADING_NOW_MS - 299_000) * 1_000_000,
        realized_pnl_usd: null,
        pnl_status: null,
        stage: "rejected",
        stop_trigger_price: null,
        take_profit_trigger_price: null,
      }),
      // A Signal whose TTL ran out before the Runtime could act on it.
      tradingExecutionRowFixture({
        case_id: "case-sol",
        disposition_reason: "expired",
        entry_filled_at_ns: null,
        entry_id: "3".repeat(64),
        exit_price: null,
        exit_reason: null,
        fees_usd: null,
        net_pnl_usd: null,
        fill_avg_price: null,
        fill_quantity: null,
        market_key: "crypto:perp:SOL:USDT",
        observed_at_ns: (TRADING_NOW_MS - 600_000) * 1_000_000,
        position_closed_at_ns: null,
        realized_pnl_usd: null,
        pnl_status: null,
        stage: "expired",
        stop_distance_bps: null,
        take_profit_bps: null,
        max_holding_ns: null,
        stop_trigger_price: null,
        take_profit_trigger_price: null,
      }),
      /*
       * The CLI manual entry from the same window: the operator's own `/long`, keyed on the Command
       * that opened it, with no Case behind it (#528 PR-3).
       */
      tradingExecutionRowFixture({
        case_id: null,
        direction: "short",
        entry_filled_at_ns: (TRADING_NOW_MS - 89_000) * 1_000_000,
        entry_id: "e".repeat(64),
        exit_price: "81100.0",
        fees_usd: "0.19791",
        fill_avg_price: "81126.9",
        fill_quantity: "0.0122",
        market_key: "crypto:perp:ETH:USDT",
        observed_at_ns: (TRADING_NOW_MS - 90_000) * 1_000_000,
        position_closed_at_ns: (TRADING_NOW_MS - 32_000) * 1_000_000,
        realized_pnl_usd: "1.11984726",
        net_pnl_usd: "1.11984726",
        source: "manual",
        stop_trigger_price: "82749.4",
        take_profit_trigger_price: "79504.4",
      }),
    ],
    /*
     * The server's own sums over the fill journals of the plans it closed (#680), not the sum of the
     * rows above them: one bounded to the current UTC day, one unbounded, both including the manual
     * entries an operator typed at the CLI (#604 T3).
     */
    totals: {
      closed_today: 2,
      closed_total: 9,
      pnl_known_today: 2,
      pnl_known_total: 9,
      pnl_missing_today: 0,
      pnl_missing_total: 0,
      realized_known_today_usd: "-13.80",
      realized_known_total_usd: "56.40",
      net_known_today: 2,
      net_known_total: 9,
      net_missing_today: 0,
      net_missing_total: 0,
      net_known_today_usd: "-13.80",
      net_known_total_usd: "56.40",
    },
    ...overrides,
  };
}
