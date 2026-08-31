/**
 * The three answers a Case may end in, plus the two historical states that stay readable (#331).
 *
 * `POLICY_REJECTED` and `ORDER_PREPARED` have no writer any more, and their Chinese says so rather than
 * pretending they are current vocabulary: a reader who meets one is looking at a row this lane can no
 * longer produce.
 */
export const CASE_STATE_ZH: Record<string, string> = {
  BLOCKED: "无法安全判定",
  NO_TRADE: "不交易",
  SIGNAL_EMITTED: "已发出 Signal",
  PENDING: "待决",
  RUNNING: "判定中",
  POLICY_REJECTED: "历史 · 地板拒绝",
  ORDER_PREPARED: "历史 · 已备单",
  INTENT_EMITTED: "历史 · 已形成意图",
};

/**
 * Why a Case that ran could not reach a decision, keyed exactly as the writer stores it (#331).
 *
 * Every one of these is a *system* fact, never an opinion about the trade: an opinion ends in `NO_TRADE`
 * with its frozen checks attached. `intent_admission_blocked` — the catch-all that covered a PostgreSQL
 * timeout and a genuine capability change alike — is deliberately absent, because nothing writes it.
 */
export const BLOCKED_REASON_ZH: Record<string, string> = {
  blacklisted: "标的在拒绝名单",
  capability_absent: "当前能力快照没有这个标的",
  capability_mismatch: "能力指针已改变，冻结的合约不再权威",
  capacity_exhausted: "通道额度已满",
  case_stale: "案例超过判定时限",
  manifest_invalid: "冻结清单无法解析",
  policy_identity_retired: "该案例的策略身份已退役",
  quantity_unexecutable: "按场所步长无可提交数量",
  source_generation_retired: "来源世代已被替换",
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

/** The one production Alpha policy, and historical identities a stored Case may still name. */
export const POLICY_ZH: Record<string, string> = {
  source_native_oi_smart_money_long_v4: "来源原生 OI × 聪明钱 · 做多",
  // Historical Case identities; current writers use V4 above.
  source_native_oi_smart_money_long_v3: "来源原生 OI × 聪明钱 · 做多（历史）",
  binance_oi_smart_money_long_v2: "Binance OI × 聪明钱 · 做多",
  liquidation_continuation_shadow_v1: "清算延续（影子）",
  liquidation_exhaustion_shadow_v1: "清算衰竭（影子）",
  news_oi_alignment_v1: "新闻 × OI 对齐",
  oi_momentum_v1: "OI 动量",
  oi_smart_money_momentum_v1: "OI × 聪明钱动量",
};

export function policyLabel(policyId: string): string {
  return POLICY_ZH[policyId] ?? policyId;
}

/**
 * Why a Source frame never became a Case, keyed `stage:reason` exactly as the ledger writes it.
 *
 * The raw key stays on screen beside the Chinese for the same reason `policy_reason` does: it is the
 * string an operator greps, and the two vocabularies must not drift into synonyms. A key with no entry
 * here renders as itself — a missing translation is a gap in this table, not a reason to hide a refusal.
 */
export const GATE_REASON_ZH: Record<string, string> = {
  "capability:capability_absent": "当前能力快照没有这个标的",
  "eligibility:already_consumed": "同一来源已成案",
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
  "source:source_generation_mismatch": "来源属于已退役世代",
  "source:source_not_live": "非 live 摄入",
  // #331: a real market fact from a venue this lane may study and never trade.
  "venue:research_only_venue": "仅研究场所 · 无资本权限",
  "venue:venue_unresolved": "场所标记无法识别",
  // Written before #331 and still in the table.
  "routing:no_native_perp": "该场所无原生永续",
  "routing:unsupported_venue": "场所未启用",
  "routing:venue_unresolved": "场所标记无法识别",
};

/** The five answers an admission decision can hold. `DEFERRED` is the only open one. */
export const GATE_STATUS_ZH: Record<string, string> = {
  CASE_CREATED: "已开案",
  DEFERRED: "待重试",
  EXPIRED: "已过期",
  REJECTED: "已拒绝",
  RESEARCH_ONLY: "仅研究",
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

/** `+1.87%` — a basis-point measurement as a percentage, sign preserved. */
export function bpsPercent(bps: number | null | undefined): string {
  if (bps == null) return "—";
  const sign = bps > 0 ? "+" : bps < 0 ? "−" : "";
  return `${sign}${(Math.abs(bps) / 100).toFixed(2)}%`;
}
