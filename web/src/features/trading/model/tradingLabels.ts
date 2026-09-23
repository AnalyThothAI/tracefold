/** The whole `trading_cases.state` vocabulary: two while a Case is claimed, three terminal. */
export const CASE_STATE_ZH: Record<string, string> = {
  PENDING: "待决",
  RUNNING: "判定中",
  DONE: "分析完成",
  FAILED: "分析不可用",
  EXCLUDED: "目标排除",
  BLOCKED: "无法安全判定",
  NO_TRADE: "不交易",
  SIGNAL_EMITTED: "已发出 Signal",
};

/**
 * Why a Case that ran could not reach a decision, keyed exactly as the writer stores it.
 *
 * Every one of these is a *system* fact, never an opinion about the trade: an opinion ends in `NO_TRADE`
 * with its frozen checks attached. A catch-all covering a PostgreSQL timeout and a real refusal alike is
 * deliberately absent — a blocked Case must name which of the three it is.
 */
export const BLOCKED_REASON_ZH: Record<string, string> = {
  manifest_invalid: "冻结清单无法解析",
  policy_identity_retired: "该案例的策略身份已退役",
  source_stale: "来源事实已过时",
};

/** The pure policy's own rule names. A rule with no entry renders as itself; it is what an operator greps. */
export const POLICY_RULE_ZH: Record<string, string> = {
  move_above_band_chasing: "价格已越过追高上限",
  not_oi_rise: "持仓不是上升",
  price_direction_not_confirmed: "价格方向未确认",
  smart_money_momentum_long: "聪明钱动量 · 做多",
  smart_money_oi_change_below_floor: "持仓变动低于地板",
  smart_money_ratio_below_or_equal_floor: "鲸鱼占比未超过地板",
  source_window_mismatch: "测量窗口不可证",
};

export function policyReasonLabel(reason: string | null | undefined): string {
  if (!reason) return "—";
  return BLOCKED_REASON_ZH[reason] ?? POLICY_RULE_ZH[reason] ?? reason;
}

/**
 * The one production Alpha policy.
 *
 * The seven retired identities this table used to carry are unreachable from every surface that reads it:
 * `/trading` and the token page both read a rolling 24 h window, and no writer has emitted any of them
 * since V4 landed. A translation nothing can render is a claim about the ledger that the ledger no longer
 * makes; a stored id with no entry here still renders as itself, which is what an operator greps anyway.
 */
export const POLICY_ZH: Record<string, string> = {
  source_native_oi_smart_money_long_v5: "来源原生 OI × 聪明钱 · 做多",
};

export function policyLabel(policyId: string | null | undefined): string {
  if (!policyId) return "—";
  return POLICY_ZH[policyId] ?? policyId;
}

/**
 * Why `entries_armed` is false, keyed exactly as `app/execution_status.py` and the Runtime write it.
 *
 * The projection's own words come first, then the Runtime lifecycle words, then the five the Runtime
 * itself blocks entries on. The reconciliation and ownership gates went with the Runtime's private account
 * proof (#680); `venue_unverified` is its venue-truth read (#680 PR-3): no fresh read of the venue's
 * positions, or one that does not yet agree with the Runtime's Cache. A reason with no entry renders as
 * itself: a missing translation is a gap in this table, never a reason to hide a refusal.
 */
export const ENTRY_BLOCK_REASON_ZH: Record<string, string> = {
  // The read projection's own words.
  disabled: "执行通道未启用",
  entry_blocked: "开仓被拒绝（未具名）",
  runtime_heartbeat_stale: "Runtime 心跳已过期",
  runtime_identity_mismatch: "Runtime 身份与配置不符",
  runtime_state_missing: "Runtime 状态未上报",
  // The Runtime process lifecycle.
  runtime_rebuilding: "Runtime 正在重建",
  runtime_starting: "Runtime 正在启动",
  runtime_stopped: "Runtime 已停止",
  // What the Runtime blocks entries on.
  emergency_halted: "已紧急停止",
  entries_paused: "开仓已暂停",
  singleton_lost: "账户槽位已被他人持有",
  unexpected_exposure: "出现无计划认领的敞口",
  venue_unverified: "交易所持仓尚未核实",
};

export function entryBlockReasonLabel(reason: string | null | undefined): string {
  if (!reason) return "允许新增 exposure";
  return ENTRY_BLOCK_REASON_ZH[reason] ?? reason;
}

/**
 * Why the Runtime accepted or refused one entry, keyed as `signal_disposition.summary.disposition`.
 *
 * `accepted` is the one word that means the venue took the entry order
 * (`tracefold/trading/stages.py:ACCEPTED_ENTRY_DISPOSITIONS`), and the Runtime writes it only after the
 * venue answered (#680). Every other word is a refusal: the Strategy's own gates, the risk policy's, the
 * sizing checks, and what the venue said about the order itself. A gate that waits — for the account, the
 * market, a narrower spread, a free instrument — waits within the Signal's TTL and names itself as the
 * refusal only if the TTL runs out first. The server's `stage` already says `ordered` or `rejected` about
 * the same row, so this table only has to say *why*; the venue's own reason for `venue_rejected` is
 * printed verbatim beside it from `order_reject_reason`.
 */
export const SIGNAL_DISPOSITION_ZH: Record<string, string> = {
  accepted: "交易所已受理",
  // The Strategy's gates.
  expired: "Signal 已过期",
  account_unavailable: "账户不可读",
  instrument_busy: "该市场已有在场执行",
  instrument_unavailable: "合约定义不可读",
  instrument_unmapped: "运行时目录里没有这个市场",
  market_unavailable: "行情不可用",
  post_stop_cooldown: "止损后冷却期内",
  spread_limit: "点差在 Signal 有效期内始终超限",
  trade_plan_busy: "交易计划写入未完成",
  trade_plan_conflict: "交易计划冲突",
  trade_plan_rejected: "交易计划被拒绝",
  // The risk policy's halts and denials.
  daily_loss_limit: "当日亏损已达上限",
  position_limit: "持仓数已达上限",
  risk_non_positive: "可用风险预算不为正",
  oi_runtime_day_start_baseline_invalid: "当日起始权益无法作为基线",
  // Sizing refusals: the order the venue would accept is not the order the risk budget allows.
  notional_below_minimum: "名义金额低于最小值",
  quantity_below_increment: "数量低于最小变动",
  quantity_below_minimum: "数量低于最小值",
  // What happened to the entry order itself.
  entry_canceled: "入场单未成交即撤销",
  entry_outcome_unknown: "入场结果未知",
  runtime_error: "Runtime 内部错误",
  venue_rejected: "交易所拒绝入场",
};

export function signalDispositionLabel(reason: string | null | undefined): string {
  if (!reason) return "等待 Runtime";
  return SIGNAL_DISPOSITION_ZH[reason] ?? ENTRY_BLOCK_REASON_ZH[reason] ?? reason;
}

/**
 * The seven stages `tracefold/trading/stages.py:execution_stage` derives. The server owns the word.
 *
 * A plan that ended because its entry was refused (`exit_reason = not_submitted`) is `rejected`, never
 * `closed`: nothing opened, so there is nothing to have closed.
 */
export const EXECUTION_STAGE_ZH: Record<string, string> = {
  pending: "待处置",
  rejected: "已拒绝",
  expired: "已过期",
  ordered: "已下单",
  filled: "已成交",
  protected: "止损已挂",
  closed: "已平仓",
};

/**
 * Which entry identity a desk row is, as `executions[].source` names it (#528 PR-3).
 *
 * A manual entry is a Command the operator typed; a Signal came from the lane. Both fold the same venue
 * facts, so the two words are the only thing that distinguishes their rows.
 */
export const EXECUTION_SOURCE_ZH: Record<string, string> = {
  manual: "手工",
  signal: "Signal",
};

/**
 * Why a trade plan ended, as `trading_trade_plans.exit_reason` stores it.
 *
 * `external` is a close this Runtime observed but did not originate (a venue-side close, a liquidation, an
 * order placed on the venue by hand); `venue_unknown` is a plan whose end the Runtime never saw because the
 * account was already flat for it when it looked. The last two are historical: plans closed before #680
 * still carry them, and nothing writes them now.
 */
export const EXIT_REASON_ZH: Record<string, string> = {
  stop_filled: "止损成交",
  take_profit: "止盈退出",
  time_exit: "持仓到期退出",
  operator_flatten: "操作员平仓",
  external: "外部平仓（非本 Runtime 发起）",
  venue_unknown: "未观察到平仓过程",
  not_submitted: "入场被拒，计划终止",
  protection_failure: "保护失败平仓",
  recovery_safety_flatten: "恢复保护时安全平仓",
};

/** What one open or in-flight order is for, as `current_account.orders[].leg` names it. */
export const ORDER_LEG_ZH: Record<string, string> = {
  entry: "入场",
  stop: "止损",
  take_profit: "止盈",
  exit: "退出",
  unknown: "用途未知",
};

export function orderLegLabel(leg: string | null | undefined): string {
  if (!leg) return "—";
  return ORDER_LEG_ZH[leg] ?? leg;
}

/**
 * Whether what the account holds is protected, as `protection_status` names it.
 *
 * `protected` means every position is claimed by a trade plan and has both its stop and its take-profit
 * resting on the venue; `not_applicable` is an account with no position at all. The Runtime answers from its own Nautilus Cache,
 * so the `pending` and `unknown` words of its private proof went with it (#680).
 *
 * It lived inline in `TradingRisk` and answered in English — `PROTECTION PENDING`, `UNPROTECTED` — while
 * the nine tables beside it answered in Chinese. One reader, one language, one place (#604 T4).
 */
export const PROTECTION_STATUS_ZH: Record<string, string> = {
  not_applicable: "无需保护",
  protected: "已受保护",
  unprotected: "未受保护",
};

export function protectionStatusLabel(value: string | null | undefined): string {
  if (!value) return "—";
  return PROTECTION_STATUS_ZH[value] ?? value;
}

/** The admission ledger's three terminal words, as `trading_candidate_gate_decisions.status` stores them. */
export const ADMISSION_STATUS_ZH: Record<string, string> = {
  CASE_CREATED: "成案",
  REJECTED: "准入拒绝",
  EXPIRED: "过期",
};

/**
 * Why admission refused a frame before any policy ran, keyed as the gate writes it.
 *
 * These are the reasons above the Case: a frame that never became a Case has no frozen checks to show,
 * so this table is the only account of it the desk can give. An untranslated key renders as itself.
 */
export const ADMISSION_REASON_ZH: Record<string, string> = {
  instrument_unmapped: "无可执行路由",
  oi_value_below_floor: "持仓价值低于地板",
  source_contract_invalid: "来源契约无效",
  source_not_live: "来源未上线",
  trigger_stale: "触发已陈旧",
};

export function admissionStatusLabel(status: string | null | undefined): string {
  if (!status) return "—";
  return ADMISSION_STATUS_ZH[status] ?? status;
}

export function admissionReasonLabel(reason: string | null | undefined): string {
  if (!reason) return "—";
  return ADMISSION_REASON_ZH[reason] ?? reason;
}

/**
 * The one thing every ledger on the desk says when it has no rows (#537 PR-5).
 *
 * Three blocks each carried their own `ledgerEmpty(pending, failed)` with the same three sentences in
 * three wordings, and the page's failure banner named the same ledgers again in a fourth. One function,
 * one subject word per ledger, and a reader learns the distinction between "empty" and "not read" once.
 */
export function ledgerSentence({
  failed,
  pending,
  subject,
}: {
  failed: boolean;
  pending: boolean;
  subject: string;
}): string {
  if (pending) return `正在读取${subject}账本…`;
  if (failed) return `${subject}账本读取失败，不能据此断言为空。`;
  return `当前 24 小时窗口没有${subject}。`;
}

/*
 * The lane's own clock, in the reader's local zone (#282). Its own rather than News' `clockTime` because
 * the import runs the wrong way: News composes over this feature's vocabulary, never the reverse, and a
 * capital surface that needed a News helper to print a timestamp would invert that for a date format.
 */
const CASE_CLOCK = new Intl.DateTimeFormat("en-CA", {
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
  hourCycle: "h23",
});

/** `08-27 14:27` — a Case can be older than today, so the day is part of the answer. */
export function caseClock(value: number | null | undefined): string {
  if (value == null) return "—";
  const parts = Object.fromEntries(
    CASE_CLOCK.formatToParts(new Date(value)).map((part) => [part.type, part.value]),
  );
  return `${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
}

/** The same clock for the nanosecond timestamps the execution ledger writes. */
export function nsClock(value: number | null | undefined): string {
  return value == null ? "—" : caseClock(Math.trunc(value / 1_000_000));
}

/** `+1.87%` — a basis-point measurement as a percentage, sign preserved. */
export function bpsPercent(bps: number | null | undefined): string {
  if (bps == null) return "—";
  const sign = bps > 0 ? "+" : bps < 0 ? "−" : "";
  return `${sign}${(Math.abs(bps) / 100).toFixed(2)}%`;
}

/** `−$14.92` — a decimal string the ledger stored, never a number the browser recomputed. */
export function moneyLabel(value: string | null | undefined): string {
  if (value == null) return "—";
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "—";
  return `${numeric < 0 ? "−" : ""}$${Math.abs(numeric).toLocaleString("en-US", {
    maximumFractionDigits: 2,
    minimumFractionDigits: 2,
  })}`;
}

/**
 * `1m32s` — how long the venue actually held the position, from the two clocks the ledger stores.
 *
 * Both are the Runtime's own observation timestamps: the first `fill` on the entry leg and the `position`
 * observation that closed it (#604 T3). Nothing is inferred from the Signal clock — a Signal can wait
 * minutes before the entry order fills, and calling that holding time would overstate every row. One
 * clock missing means the entry is still open or never filled, and the cell says so with a dash rather
 * than measuring against `now`.
 */
export function holdingLabel(
  filledAtNs: number | null | undefined,
  closedAtNs: number | null | undefined,
): string {
  if (filledAtNs == null || closedAtNs == null) return "—";
  const seconds = Math.round((closedAtNs - filledAtNs) / 1_000_000_000);
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  const hours = Math.floor(seconds / 3_600);
  const minutes = Math.floor((seconds % 3_600) / 60);
  const rest = seconds % 60;
  if (hours) return `${hours}h${String(minutes).padStart(2, "0")}m`;
  if (minutes) return `${minutes}m${String(rest).padStart(2, "0")}s`;
  return `${rest}s`;
}

/**
 * Which side of the market axis a realized number sits on, or neither.
 *
 * `tokens.css` reads red as bullish and green as bearish, and a realized result belongs to that same axis:
 * a profit is what a long that worked produced. The ledger used to print both in `--text-secondary`, where
 * `−$11.04` and `+$110.33` scan identically, and spent the direction axis on a `LONG` column that has been
 * the same word on every production row (#604 T4).
 */
export function moneyTone(value: string | null | undefined): "profit" | "loss" | undefined {
  if (value == null) return undefined;
  const numeric = Number(value);
  if (!Number.isFinite(numeric) || numeric === 0) return undefined;
  return numeric > 0 ? "profit" : "loss";
}
