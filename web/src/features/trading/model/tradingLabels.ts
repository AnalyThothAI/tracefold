/** The whole `trading_cases.state` vocabulary: two while a Case is claimed, three terminal. */
export const CASE_STATE_ZH: Record<string, string> = {
  PENDING: "待决",
  RUNNING: "判定中",
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
 * The projection's own words come first, then the Runtime readiness gates it forwards. A reason with no
 * entry renders as itself: a missing translation is a gap in this table, never a reason to hide a refusal.
 */
export const ENTRY_BLOCK_REASON_ZH: Record<string, string> = {
  disabled: "执行通道未启用",
  emergency_halted: "已紧急停止",
  entries_paused: "开仓已暂停",
  entry_blocked: "开仓被拒绝（未具名）",
  reconciliation_stale: "私有对账已过期",
  runtime_heartbeat_stale: "Runtime 心跳已过期",
  runtime_identity_mismatch: "Runtime 身份与配置不符",
  runtime_starting: "Runtime 正在启动",
  runtime_state_missing: "Runtime 状态未上报",
  runtime_stopped: "Runtime 已停止",
  singleton_lost: "账户槽位已被他人持有",
  startup_reconciliation_unproven: "启动对账尚未完成",
  unexpected_exposure: "出现无主敞口",
  ownership_ambiguous: "交易归属存在冲突",
};

export function entryBlockReasonLabel(reason: string | null | undefined): string {
  if (!reason) return "允许新增 exposure";
  return ENTRY_BLOCK_REASON_ZH[reason] ?? reason;
}

/**
 * Why the Runtime accepted or refused one Signal, keyed as `signal_disposition.summary.disposition`.
 *
 * Four of these mean accepted (`tracefold/trading/stages.py:ACCEPTED_ENTRY_DISPOSITIONS`); the rest are the
 * entry path's own refusals in `oi_runtime/entry.py` plus the risk policy's in `oi_runtime/risk.py`. The
 * server's `stage` already says `ordered` or `rejected` about the same row, so this table only has to
 * say *why*. Exactly the words a writer can still emit: the two cached-replay refusals went with
 * `entry._replay_cached` (#537 PR-4), and the reasons above them with the gates #537 PR-3 deleted.
 */
export const SIGNAL_DISPOSITION_ZH: Record<string, string> = {
  accepted: "已受理",
  recovered: "重启后恢复",
  replayed_query_first: "缓存订单重放 · 先查询",
  unknown_query_first: "提交结果未知 · 先查询",
  // The entry path's refusals.
  expired: "Signal 已过期",
  instrument_busy: "该市场已有在场执行",
  instrument_or_market_missing: "缺少合约或行情",
  instrument_unmapped: "运行时目录里没有这个市场",
  market_subscription_pending: "行情订阅预热中",
  protection_unproven: "已有敞口未证明受保护",
  // The risk policy's halts and denials.
  account_stale: "账户快照已过期",
  daily_loss_limit: "当日亏损已达上限",
  market_stale: "行情已过期",
  position_limit: "持仓数已达上限",
  risk_non_positive: "可用风险预算不为正",
  // Sizing refusals: the order the venue would accept is not the order the risk budget allows.
  notional_below_minimum: "名义金额低于最小值",
  quantity_below_increment: "数量低于最小变动",
  quantity_below_minimum: "数量低于最小值",
  spread_limit: "点差超限",
  // Facts the entry path could not read at all.
  oi_runtime_account_balance_missing: "账户余额不可读",
  oi_runtime_account_missing: "账户不可读",
  oi_runtime_candidate_market_missing: "候选市场行情不可读",
  oi_runtime_day_start_baseline_invalid: "当日起始权益无法作为基线",
  oi_runtime_instrument_missing: "合约定义不可读",
  oi_runtime_market_invalid: "行情不可用于判定",
  oi_runtime_market_missing: "行情不可读",
};

export function signalDispositionLabel(reason: string | null | undefined): string {
  if (!reason) return "等待 Runtime";
  return SIGNAL_DISPOSITION_ZH[reason] ?? ENTRY_BLOCK_REASON_ZH[reason] ?? reason;
}

/** The seven stages `tracefold/trading/stages.py:execution_stage` derives. The server owns the word. */
export const EXECUTION_STAGE_ZH: Record<string, string> = {
  pending: "待处置",
  rejected: "已拒绝",
  expired: "已过期",
  ordered: "已下单",
  filled: "已成交",
  protected: "止损已挂",
  closed: "已平仓",
  closing: "平仓中",
  unresolved: "归属待核实",
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

/** The three exits `ProtectionCoordinator` can witness, as the `position/closed` observation records them. */
export const HISTORY_GAP_ZH: Record<string, string> = {
  audit_gap: "成交历史存在审计缺口",
  entry_fill_missing: "入场成交记录缺失",
  entry_outcome_unknown: "缺少入场结果凭据",
  close_observation_missing: "平仓记录缺失",
  exit_fills_incomplete: "退出成交记录不完整",
  native_pnl_basis_incomplete_after_restart: "重启后成交成本基础不完整",
};

export const EXIT_REASON_ZH: Record<string, string> = {
  take_profit: "止盈退出",
  time_exit: "持仓到期退出",
  operator_flatten: "操作员平仓",
  protection_failure: "保护失败平仓",
  recovery_safety_flatten: "恢复保护时安全平仓",
  venue_unknown: "退出原因待确认",
  not_submitted: "计划终止，未提交入场",
  flatten: "flatten 退出",
  stop_filled: "止损成交",
  unclaimed_flatten: "无主敞口 flatten",
};

/**
 * How far the Runtime has got with protecting what the account holds, as `protection_status` names it.
 *
 * It lived inline in `TradingRisk` and answered in English — `PROTECTION PENDING`, `UNPROTECTED` — while
 * the nine tables beside it answered in Chinese. One reader, one language, one place (#604 T4).
 */
export const PROTECTION_STATUS_ZH: Record<string, string> = {
  not_applicable: "无需保护",
  pending: "保护挂单中",
  protected: "已受保护",
  unknown: "保护状态未知",
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
