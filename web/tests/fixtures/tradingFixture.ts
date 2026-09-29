import type {
  TradingCase,
  TradingCases,
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
      state: "disabled",
      active_policy: "trade_assessment_v1",
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
    policy_config_digest: "e".repeat(64),
    policy_id: ALPHA_POLICY_ID,
    policy_reason: "smart_money_ratio_below_or_equal_floor",
    pre_move_bps: 187,
    review_mode: "none",
    state: "NO_TRADE",
    ...overrides,
  };
}

/**
 * The counts read: three durable 24 h distributions and no Cases (#604 T3).
 *
 * `cases` is empty without `case_id` by contract — the response stopped publishing the 100-row list the
 * desk downloaded every 15 s to render at most one of. `admission_counts_24h` is the funnel's top: how
 * many frames admission looked at at all, and what it did with the ones it refused.
 */
export function tradingCasesFixture(overrides: Partial<TradingCases> = {}): TradingCases {
  return {
    admission_counts_24h: [
      { status: "CASE_CREATED", reason: null, count: 7 },
      { status: "REJECTED", reason: "oi_value_below_floor", count: 4 },
      { status: "EXPIRED", reason: "trigger_stale", count: 1 },
    ],
    cases: [],
    total: 0,
    next_cursor: null,
    window_from_ms: TRADING_NOW_MS - 86400000,
    window_to_ms: TRADING_NOW_MS,
    complete: true,
    decision_counts_24h: [
      { action: "TRADE", publish_status: "published", count: 1 },
      { action: "TRADE", publish_status: "unpublished", count: 2 },
      { action: "WATCH", publish_status: "not_applicable", count: 1 },
      { action: "NO_TRADE", publish_status: "not_applicable", count: 3 },
    ],
    state_counts_24h: { BLOCKED: 1, NO_TRADE: 5, SIGNAL_EMITTED: 1 },
    window_hours: 24,
    ...overrides,
  };
}

/** The known Cases this fixture set can answer `?case_id=` with; anything else is an empty `cases[]`. */
const KNOWN_CASE_IDS = new Set(["case-hype", "case-btc", "case-nvda", "case-sol"]);

/** `?case_id=` is an exact primary key: the one Case, or none, and an unknown id is not an error. */
export function tradingCasesForCaseId(caseId: string | null): TradingCases {
  const batch = tradingCasesFixture();
  if (!caseId) return batch;
  return {
    ...batch,
    cases: KNOWN_CASE_IDS.has(caseId) ? [tradingCaseFixture({ case_id: caseId })] : [],
  };
}

/**
 * One entry that ran to the end: entered, protected, and flattened out with a realized number on it.
 *
 * The shape is `console_executions_statement`'s own — a `closed` plan carries the quantity its entry
 * filled, the average exit price, the net realized PnL and the commissions folded from its fill journal
 * (#680), and the `exit_reason` its plan closed with. `source` says which entry identity `entry_id` is.
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
