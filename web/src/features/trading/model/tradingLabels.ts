/** The executor readiness projection's closed set of entry block reasons. */
export const ENTRY_BLOCK_REASON_ZH: Record<string, string> = {
  disabled: "执行通道未启用",
  executor_state_missing: "执行器状态未上报",
  executor_heartbeat_stale: "执行器心跳已过期",
  executor_error: "执行器故障",
  account_reconcile_stale: "账户签名对账已过期",
  emergency_halt: "已紧急停止",
  entries_paused: "开仓已暂停",
  unexpected_exposure: "账户检查发现异常",
  execution_fault: "仍有未解决执行风险",
  flatten_in_progress: "账户退出仍在核验",
};

export function entryBlockReasonLabel(reason: string | null | undefined): string {
  if (!reason) return "允许新增 exposure";
  return ENTRY_BLOCK_REASON_ZH[reason] ?? reason;
}

/** Durable admission decision; order stage separately reports venue evidence. */
export const SIGNAL_DISPOSITION_ZH: Record<string, string> = {
  accepted: "执行器已受理",
  expired: "Signal 已过期",
  execution_venue_unlisted: "DEMO 场所未列出该合约",
  manual_market_invalid: "手工入场市场无效",
  symbol_exposure: "该市场已有仓位或挂单",
  capacity: "账户并发容量已满",
  quote_stale: "报价已过期",
  quote_stale_before_send: "提交前报价已过期",
  spread: "点差超限",
  price_drift: "相对 LIVE 参考价偏移超限",
  equity_unavailable: "账户权益不可读",
  available_margin: "可用保证金不足",
  funding_facts_unavailable: "资金事实不完整",
  margin_mode_unsupported: "保证金模式不受支持",
  multi_assets_unsupported: "多资产模式不受支持",
  execution_fault: "未解决执行风险阻止新增仓位",
  market_rules_invalid: "合约规则无效",
  leverage_capacity: "杠杆容量不足",
  market_lot_or_notional: "下单数量或名义金额不满足场所规则",
  hedge_mode_unsupported: "对冲持仓模式不受支持",
  control_applied: "账户控制已执行",
  flatten_requested: "平仓请求已受理",
};

export function signalDispositionLabel(reason: string | null | undefined): string {
  if (!reason) return "等待执行器";
  return SIGNAL_DISPOSITION_ZH[reason] ?? ENTRY_BLOCK_REASON_ZH[reason] ?? reason;
}

/**
 * Stages derived from executor ledger facts. The server owns the word.
 *
 * A plan that ended because its entry was refused (`exit_reason = not_submitted`) is `rejected`, never
 * `closed`: nothing opened, so there is nothing to have closed.
 */
export const EXECUTION_STAGE_ZH: Record<string, string> = {
  pending: "待处置",
  accepted: "已受理",
  submission_unknown: "提交待核实",
  rejected: "已拒绝",
  expired: "已过期",
  ordered: "已下单",
  filled: "已成交",
  protected: "止损与止盈已挂",
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

/** Why a DEMO Plan ended, as recorded by signed REST reconciliation. */
export const EXIT_REASON_ZH: Record<string, string> = {
  stop_filled: "止损成交",
  take_profit: "止盈退出",
  time_exit: "持仓到期退出",
  operator_flatten: "操作员平仓",
  external: "外部平仓",
  entry_rejected: "入场被场所拒绝",
  protection_failed: "保护失败后平仓",
  protection_unverified: "保护结果未查明后平仓",
  protection_coverage_invalid: "保护覆盖不足后平仓",
  protection_trigger_immediate: "保护触发价已越过，安全平仓",
  protection_rules_missing: "保护规则缺失，安全平仓",
  partial_protection_exit: "保护部分成交后平仓",
};

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
