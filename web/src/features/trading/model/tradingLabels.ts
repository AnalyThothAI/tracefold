import type { TradingOrder } from "../api/tradingQueries";

/**
 * The ledger's state words, and the one sentence each of them actually means (#185).
 *
 * Every key here is the state machine's own string and stays on screen beside the Chinese. That is not
 * decoration: `ACKNOWLEDGED` means the venue answered, not that anything filled, and `OPEN` is the only
 * state that has proven both a real position and a native stop covering it. A console that rendered either
 * as 已成交 would be asserting something the ledger does not, which is the failure #185 P0-3 exists to stop.
 */
export const ORDER_STATE_NOTE: Record<string, string> = {
  ACKNOWLEDGED: "交易所已应答；成交未证明——ACK≠成交",
  AMBIGUOUS: "提交超时未读到回执：只读对账中，永不盲重发",
  APPROVED: "已批准，尚未提交",
  AWAITING_APPROVAL: "payload 摘要已绑定 · 60s 过期作废",
  CLOSED: "已了结",
  MANUAL_REVIEW_REQUIRED: "对账无法自行收敛，等待人确认交易所侧的事实",
  NO_FILL: "未成交",
  OPEN: "仓位与原生止损双证明",
  PARTIAL: "部分成交",
  PREPARED: "载荷已冻结，尚未提交",
  RECONCILING: "正在对账",
  REJECTED: "交易所拒绝",
  REJECTED_BY_OPERATOR: "操作员拒绝",
  SAFETY_CLOSING: "保护不成立，一次性全平",
  SUBMITTING: "提交中",
  UNPROTECTED: "有仓位但止损不成立",
};

/** Which states hold, or may yet turn out to hold, exposure. Mirrors `ux_trading_active_underlying`. */
export const ACTIVE_ORDER_STATES: readonly string[] = [
  "PREPARED",
  "AWAITING_APPROVAL",
  "APPROVED",
  "SUBMITTING",
  "AMBIGUOUS",
  "RECONCILING",
  "MANUAL_REVIEW_REQUIRED",
  "ACKNOWLEDGED",
  "PARTIAL",
  "OPEN",
  "UNPROTECTED",
  "SAFETY_CLOSING",
];

export const CASE_STATE_ZH: Record<string, string> = {
  BLOCKED: "被封锁",
  NO_TRADE: "不交易",
  ORDER_PREPARED: "已下单",
  PENDING: "待决",
  POLICY_REJECTED: "地板拒绝",
  RUNNING: "判定中",
};

export const TRIGGER_KIND_ZH: Record<string, string> = {
  liquidation: "清算触发",
  news: "新闻触发",
  oi: "OI 触发",
};

export const STRATEGY_ZH: Record<string, string> = {
  liquidation_continuation_shadow_v1: "清算延续（影子）",
  liquidation_exhaustion_shadow_v1: "清算衰竭（影子）",
  news_oi_alignment_v1: "新闻 × OI 对齐",
  oi_momentum_v1: "OI 动量",
};

/** Compact approved case labels, each a direct presentation of the immutable strategy identity. */
export const STRATEGY_CASE_LABEL: Record<string, string> = {
  news_oi_alignment_v1: "news_oi",
  oi_momentum_v1: "oi_only",
};

export function strategyCaseLabel(strategyId: string): string {
  return STRATEGY_CASE_LABEL[strategyId] ?? STRATEGY_ZH[strategyId] ?? strategyId;
}

/**
 * Why a source frame never became a case (#264), keyed `stage:reason` exactly as the ledger writes it.
 *
 * The raw key stays on screen beside the Chinese for the same reason `policy_reason` does: it is the
 * string an operator greps, and the two vocabularies must not drift into synonyms. A key with no entry
 * here renders as itself rather than as 其他 — a missing translation is a gap in this table, not a
 * reason to hide a refusal the ledger recorded.
 */
export const GATE_REASON_ZH: Record<string, string> = {
  "eligibility:active_underlying": "该标的已有在场仓位",
  "eligibility:already_consumed": "同一来源已成案",
  "eligibility:blacklisted": "标的在拒绝名单",
  "eligibility:case_in_flight": "该标的已有未决案例",
  "eligibility:cooldown": "标的冷却期内",
  "eligibility:lane_capacity_exhausted": "通道当日额度已满",
  "eligibility:oi_value_below_floor": "持仓额低于流动性地板",
  "eligibility:rank_above_limit": "窗口内名次超限",
  "eligibility:superseded_by_newer_trigger": "被同标的更新的帧合并",
  "eligibility:trigger_stale": "帧已过触发时效",
  "freeze:case_created": "已开案",
  "market_context:market_data_invalid": "截面无可用收盘价",
  "market_context:market_data_unavailable": "行情暂不可读",
  "routing:no_native_perp": "该场所无原生永续",
  "routing:unsupported_venue": "场所未启用",
  "routing:venue_unresolved": "场所标记无法识别",
  "source:source_contract_invalid": "来源契约不成立",
  "source:source_generation_mismatch": "来源属于已退役世代",
  "source:source_not_live": "非 live 摄入",
};

/** The four terminal answers an admission decision can hold. `DEFERRED` is the only open one. */
export const GATE_STATUS_ZH: Record<string, string> = {
  CASE_CREATED: "已开案",
  DEFERRED: "待重试",
  EXPIRED: "已过期",
  REJECTED: "已拒绝",
};

export function gateReasonLabel(key: string): string {
  return GATE_REASON_ZH[key] ?? key;
}

export const REGIME_ZH: Record<string, string> = {
  buildup_down: "增仓 · 价跌",
  buildup_up: "增仓 · 价升",
  deleveraging_down: "减仓 · 价跌",
  deleveraging_up: "减仓 · 价升",
  unclear: "象限不明",
};

export function isActiveOrder(order: TradingOrder): boolean {
  return ACTIVE_ORDER_STATES.includes(order.state);
}

/**
 * Whether this row has proven a native stop covering the position.
 *
 * Exactly `state === "OPEN"`, and deliberately not "has a `stop_price`". Every prepared order carries a
 * stop *price* — that is the frozen intent. What `OPEN` adds is that a read came back proving the venue
 * holds a reduce-only stop covering the filled quantity. Ticking the check on a price would put a
 * protection mark on an unprotected position.
 */
export function stopVerified(order: TradingOrder): boolean {
  return order.state === "OPEN";
}

/** `4h 00m`, or `—` when there is no clock yet: the hold starts at the first fill, not at submission. */
export function holdRemaining(order: TradingOrder, nowMs: number): string {
  if (order.position_opened_at_ms == null || order.must_close_at_ms == null) return "—";
  const left = order.must_close_at_ms - nowMs;
  if (left <= 0) return "已到期";
  const minutes = Math.floor(left / 60_000);
  return `剩 ${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, "0")}m`;
}

export function heldFor(order: TradingOrder): string {
  if (order.position_opened_at_ms == null || order.position_closed_at_ms == null) return "—";
  const minutes = Math.floor((order.position_closed_at_ms - order.position_opened_at_ms) / 60_000);
  return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, "0")}m`;
}

/**
 * The paper caveat, in the column heading rather than a footnote.
 *
 * paper fills at the frozen `entry_reference` with no spread, no precision, no partial fill and no
 * liquidation, so a number from it is a property of the execution kernel and not of a strategy. The label
 * says 纸面 wherever the mode is paper, and the page never draws an equity curve from these.
 */
export function pnlLabel(mode: string): string {
  return mode === "paper" ? "未实现（纸面）" : "未实现";
}

/**
 * The configured maximum hold, exactly — no unit that can round it.
 *
 * Two attempts got this wrong in the same direction. `Math.round(ms / 3_600_000)` printed the shipped
 * default (`max_holding_seconds: 1800`) as `0 h`, which reads as no ceiling at all. One decimal then
 * printed 23 h 59 m as `24.0 h`, which is worse: it states a limit *larger* than the one enforced, and a
 * risk control that reads high is the one direction that cannot be allowed to round.
 *
 * So the remainder is carried rather than converted: minutes below an hour, `Xh YYm` when the hours do not
 * divide, and a bare hour count only when they do. Seconds do not appear because
 * `max_holding_seconds` is compared as milliseconds against a wall clock and no operator sets a hold to a
 * granularity the venue could honour anyway; a value with a stray second still rounds to the minute, and
 * that minute is never rounded up past the ceiling.
 */
export function holdCeiling(ms: number): string {
  if (!Number.isFinite(ms) || ms <= 0) return "—";
  const minutes = Math.floor(ms / 60_000);
  if (minutes < 60) return `${minutes || 1} 分钟`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest === 0 ? `${hours} h` : `${hours}h ${String(rest).padStart(2, "0")}m`;
}
