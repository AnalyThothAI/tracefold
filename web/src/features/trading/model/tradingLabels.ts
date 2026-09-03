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
 * deliberately absent — a blocked Case must name which of the four it is.
 */
export const BLOCKED_REASON_ZH: Record<string, string> = {
  case_stale: "案例超过判定时限",
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
  smart_money_profit_not_positive: "盈利指标不为正",
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
  source_native_oi_smart_money_long_v4: "来源原生 OI × 聪明钱 · 做多",
};

export function policyLabel(policyId: string): string {
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
  execution_unsafe: "现有 exposure 不可安全保护",
  migration_restart_required: "迁移后需重启 Runtime",
  reconciliation_stale: "私有对账已过期",
  runtime_control_state_missing: "控制状态未上报",
  runtime_heartbeat_stale: "Runtime 心跳已过期",
  runtime_identity_mismatch: "Runtime 身份与配置不符",
  runtime_starting: "Runtime 正在启动",
  runtime_state_missing: "Runtime 状态未上报",
  runtime_stopped: "Runtime 已停止",
  singleton_lost: "账户槽位已被他人持有",
  startup_reconciliation_unproven: "启动对账尚未完成",
  unexpected_exposure: "出现无主敞口",
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
 * server already derives `accepted | rejected` from the same set, so this table only has to say *why*.
 */
export const SIGNAL_DISPOSITION_ZH: Record<string, string> = {
  accepted: "已受理",
  recovered: "重启后恢复",
  replayed_query_first: "缓存订单重放 · 先查询",
  unknown_query_first: "提交结果未知 · 先查询",
  // The entry path's refusals.
  cached_entry_invalid: "缓存入场单与当前配置不符",
  cached_position_invalid: "缓存仓位与当前配置不符",
  expired: "Signal 已过期",
  instrument_busy: "该市场已有在场执行",
  instrument_or_market_missing: "缺少合约或行情",
  instrument_unmapped: "运行时目录里没有这个市场",
  market_subscription_pending: "行情订阅预热中",
  protection_unproven: "已有敞口未证明受保护",
  // The risk policy's halts and denials.
  account_stale: "账户快照已过期",
  aggregate_risk_limit: "总风险已达上限",
  daily_loss_limit: "当日亏损已达上限",
  leverage_limit: "杠杆超限",
  market_stale: "行情已过期",
  position_limit: "持仓数已达上限",
  risk_non_positive: "可用风险预算不为正",
  // Sizing refusals: the order the venue would accept is not the order the risk budget allows.
  notional_below_minimum: "名义金额低于最小值",
  quantity_below_increment: "数量低于最小变动",
  quantity_below_minimum: "数量低于最小值",
  quantity_exceeds_leverage_after_rounding: "取整后超出杠杆上限",
  quantity_exceeds_risk_after_rounding: "取整后超出风险预算",
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
};

/** The five stages `tracefold/trading/stages.py:command_stage` derives from `control_disposition` alone. */
export const COMMAND_STAGE_ZH: Record<string, string> = {
  recorded: "已持久化",
  accepted: "Runtime 受理",
  rejected: "Runtime 拒绝",
  completed: "已完成 · 私有对账证明",
  expired: "已过期",
};

/** The closed operator grammar, as `trading_execution_commands.action` stores it. */
export const COMMAND_ACTION_ZH: Record<string, string> = {
  emergency_halt: "紧急停止",
  flatten: "Flatten account",
  manual_entry: "手动方向",
  pause_entries: "Pause entries",
  resume_entries: "Resume / Arm",
};

/** The three exits `ProtectionCoordinator` can witness, as the `position/closed` observation records them. */
export const EXIT_REASON_ZH: Record<string, string> = {
  flatten: "flatten 退出",
  stop_filled: "止损成交",
  unclaimed_flatten: "无主敞口 flatten",
};

/**
 * Why a Source frame never became a Case, keyed `stage:reason` exactly as the ledger writes it.
 *
 * The raw key stays on screen beside the Chinese for the same reason `policy_reason` does: it is the
 * string an operator greps, and the two vocabularies must not drift into synonyms. A key with no entry
 * here renders as itself — a missing translation is a gap in this table, not a reason to hide a refusal.
 */
export const GATE_REASON_ZH: Record<string, string> = {
  "eligibility:already_consumed": "同一来源已成案",
  // No configured Runtime lists this market, so a Case would only ever be refused later.
  "eligibility:instrument_unmapped": "运行时目录里没有这个市场",
  "eligibility:blacklisted": "标的在拒绝名单",
  "eligibility:lane_capacity_exhausted": "本轮成案预算已满",
  "eligibility:oi_value_below_floor": "持仓额低于流动性地板",
  "eligibility:superseded_by_newer_trigger": "被同标的更新的帧合并",
  "eligibility:underlying_busy": "该标的已被占用",
  "eligibility:trigger_stale": "帧已过触发时效",
  // Retired by #348 and kept only to read the ledger's 90-day history. `lane_capacity_exhausted`
  // survives as a current answer but no longer means a daily quota: the lane's bound is one live
  // thesis, so the label above says that instead.
  "eligibility:active_underlying": "该标的已有在场仓位（旧）",
  "eligibility:case_in_flight": "该标的已有未决案例（旧）",
  "eligibility:cooldown": "标的冷却期内（旧）",
  "eligibility:rank_above_limit": "窗口内名次超限（旧）",
  "freeze:already_consumed": "同一来源已成案",
  "freeze:case_created": "已开案",
  "market_context:market_data_invalid": "截面无可用收盘价",
  "market_context:market_data_unavailable": "行情暂不可读",
  "source:source_contract_invalid": "来源契约不成立",
  "source:source_not_live": "非 live 摄入",
  "venue:venue_unresolved": "场所标记无法识别",
};

/** The four answers an admission decision can hold. `DEFERRED` is the only open one. */
export const GATE_STATUS_ZH: Record<string, string> = {
  CASE_CREATED: "已开案",
  DEFERRED: "待重试",
  EXPIRED: "已过期",
  REJECTED: "已拒绝",
};

export function gateReasonLabel(key: string): string {
  return GATE_REASON_ZH[key] ?? key;
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
