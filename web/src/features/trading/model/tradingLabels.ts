import type { TradingIntent } from "../api/tradingQueries";

/** Nautilus execution states and the exact fact each one proves. */
export const INTENT_STATE_NOTE: Record<string, string> = {
  PENDING: "等待 Nautilus 领取",
  IN_FLIGHT: "Nautilus 正在执行",
  OPEN_PROTECTED: "仓位与交易所原生保护均已证明",
  MANUAL_REVIEW: "交易所事实无法自动收敛，等待人工处置",
  TERMINAL: "执行已终结并写入 Outcome",
};

export const ACTIVE_INTENT_STATES: readonly string[] = [
  "PENDING",
  "IN_FLIGHT",
  "OPEN_PROTECTED",
  "MANUAL_REVIEW",
];

export const CASE_STATE_ZH: Record<string, string> = {
  BLOCKED: "被封锁",
  NO_TRADE: "不交易",
  INTENT_EMITTED: "已形成意图",
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
  // Retained because historical Cases still carry it; no new Case is routed to it since #265. The label
  // is unchanged on purpose: a "（历史）" suffix wrapped the cell to two lines on the token page and told
  // a reader nothing the case's own date does not.
  oi_momentum_v1: "OI 动量",
  oi_smart_money_momentum_v1: "OI × 聪明钱动量",
};

/** Compact approved case labels, each a direct presentation of the immutable strategy identity. */
export const STRATEGY_CASE_LABEL: Record<string, string> = {
  news_oi_alignment_v1: "news_oi",
  oi_momentum_v1: "oi_only",
  oi_smart_money_momentum_v1: "oi_smart_money",
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
  // #273: the News lane's own admission rule. It is the reason a News trigger no longer freezes a
  // case just so a strategy can say the same thing from inside a manifest.
  "eligibility:oi_context_missing": "新闻旁没有同标的持仓数据",
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

export function isActiveIntent(intent: TradingIntent): boolean {
  return ACTIVE_INTENT_STATES.includes(intent.execution_state);
}

/** Whether Nautilus has proved both the position and its venue-native protective stop. */
export function stopVerified(intent: TradingIntent): boolean {
  return intent.execution_state === "OPEN_PROTECTED" && intent.protected_quantity != null;
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

/*
 * The lane's own clock, in the reader's local zone (#282). Its own rather than News' `clockTime` because
 * the import runs the wrong way: News composes over this feature's vocabulary, never the reverse, and a
 * capital surface that needed a News helper to print a timestamp would invert that for a date format.
 * Same `en-CA` 24-hour basis on both sides, so a case and the frame that authored it agree on the minute.
 */
const CASE_CLOCK = new Intl.DateTimeFormat("en-CA", {
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
  hourCycle: "h23",
});

/** `08-27 14:27` — a case can be older than today, so the day is part of the answer. */
export function caseClock(value: number | null | undefined): string {
  if (value == null) return "—";
  const parts = Object.fromEntries(
    CASE_CLOCK.formatToParts(new Date(value)).map((part) => [part.type, part.value]),
  );
  return `${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
}

/*
 * `+212 bps` / `−34 bps` — a realised result in the unit the ledger measured it in, which is also the unit
 * 今日已了结 prints on the Trading workbench. The artifact draws this column as `+2.12%`; one console-wide
 * unit for the same field is worth more here than following the drawing.
 */
export function realizedLabel(bps: number | null | undefined): string {
  if (bps == null) return "—";
  const sign = bps > 0 ? "+" : bps < 0 ? "−" : "";
  return `${sign}${Math.abs(bps)} bps`;
}

/** `+1.87% · 带内` — the frozen pre-move against the band the case was decided under. */
export function preMoveLabel(
  bps: number | null | undefined,
  config: Record<string, string>,
): string {
  if (bps == null) return "未测量";
  const sign = bps > 0 ? "+" : bps < 0 ? "−" : "";
  const move = `${sign}${(Math.abs(bps) / 100).toFixed(2)}%`;
  const min = Number(config.min_price_move_bps);
  const max = Number(config.max_price_move_bps);
  if (!Number.isFinite(min) || !Number.isFinite(max) || max <= min) return move;
  return `${move} · ${bps >= min && bps <= max ? "带内" : "带外"}`;
}
