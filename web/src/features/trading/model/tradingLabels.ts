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
  ACKNOWLEDGED: "交易所已应答——不是成交，还没有权威仓位数量，持有时钟未起算",
  AMBIGUOUS: "提交结果不明，进入只读对账：不盲目重发、不换场所",
  APPROVED: "已批准，尚未提交",
  AWAITING_APPROVAL: "等待审批，绑定确切 payload 摘要，过期即作废",
  CLOSED: "已了结",
  MANUAL_REVIEW_REQUIRED: "对账无法自行收敛，等待人确认交易所侧的事实",
  NO_FILL: "未成交",
  OPEN: "仓位与覆盖数量的原生止损都已证明",
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

export const CASE_KIND_ZH: Record<string, string> = {
  news_oi: "新闻+OI",
  news_only: "仅新闻",
  oi_only: "仅 OI",
};

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
 * The configured maximum hold, in a unit that can express it.
 *
 * `Math.round(ms / 3_600_000)` printed the shipped default — `max_holding_seconds: 1800` — as `0 h`, which
 * reads as "no ceiling at all", the exact opposite of a thirty-minute cap. A risk control the page states
 * has to be stated in a unit that survives the value: minutes below an hour, one decimal where the hours do
 * not divide, a whole number when they do.
 */
export function holdCeiling(ms: number): string {
  if (!Number.isFinite(ms) || ms <= 0) return "—";
  const minutes = ms / 60_000;
  if (minutes < 60) return `${Math.round(minutes)} 分钟`;
  const hours = minutes / 60;
  return Number.isInteger(hours) ? `${hours} h` : `${hours.toFixed(1)} h`;
}
