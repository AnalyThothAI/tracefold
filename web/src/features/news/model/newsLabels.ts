import type {
  NewsAssetRef,
  NewsFeedOutcome,
  NewsHealthLevel,
  NewsOutcomeKind,
  NewsTimelineStep,
} from "../api/newsQueries";

import { formatNewsLocalTimestamp } from "./newsTime";

/**
 * UI-only copy. Every business word (rules, admissions, error codes, event types, directions) arrives from the
 * API already in Chinese (`outcome.text_zh`, `*_zh`, `label_zh`); this file only names UI affordances and maps
 * server enums to visual tone.
 */

/**
 * Two colour axes that never share a hue (#74):
 *
 *   `Direction` — the market call. Red is 利多 and green is 利空, the mainland convention.
 *   `Tone`      — where the Event got to in the pipeline. Blue / amber / grey, deliberately never red or green,
 *                 because "已推送" is a completed step rather than a market opinion and would otherwise read as
 *                 a second, contradictory 利多 / 利空.
 */
export type Tone = "done" | "info" | "caution" | "alert" | "neutral";
export type Direction = "bullish" | "bearish" | "flat";

const OUTCOME_TONE: Record<NewsOutcomeKind, Tone> = {
  delivered: "done",
  pending_delivery: "info",
  queued_triage: "info",
  queued_publish: "info",
  throttled: "caution",
  dropped: "neutral",
  held_gate: "neutral",
  held_recovery: "neutral",
  degraded_dropped: "alert",
  delivery_failed: "alert",
};

export function outcomeTone(kind: NewsOutcomeKind): Tone {
  return OUTCOME_TONE[kind] ?? "neutral";
}

const HEALTH_TONE: Record<NewsHealthLevel, Tone> = {
  ok: "done",
  warn: "caution",
  bad: "alert",
  off: "neutral",
};

/**
 * Market direction → its own axis plus a colour-independent arrow. The Chinese word always comes from the
 * server as `direction_zh`; red and green only reinforce it, and the glyph carries the same meaning for a
 * reader who cannot separate the two hues (they sit at nearly equal luminance by necessity — both have to
 * clear 4.5:1 on white). `neutral` and `unclear` resolve to `flat` so the ~42% of neutral verdicts stay quiet
 * and do not drown the ones that moved.
 */
const DIRECTION: Record<string, Direction> = {
  bullish: "bullish",
  bearish: "bearish",
  neutral: "flat",
  unclear: "flat",
};

const DIRECTION_GLYPH: Record<string, string> = {
  bullish: "\u25b2",
  bearish: "\u25bc",
  neutral: "\u2014",
  unclear: "?",
};

export function directionTone(direction: string | null | undefined): Direction {
  return DIRECTION[direction ?? ""] ?? "flat";
}

export function directionGlyph(direction: string | null | undefined): string {
  return DIRECTION_GLYPH[direction ?? ""] ?? "\u2014";
}

const OUTCOME_TAB_LABELS: Record<NewsFeedOutcome, string> = {
  pushed: "已推送",
  held: "被拦截",
  pending: "处理中",
};

export function outcomeTabLabel(value: NewsFeedOutcome | null): string {
  return value ? OUTCOME_TAB_LABELS[value] : "全部";
}

const HEALTH_LEVEL_LABELS: Record<NewsHealthLevel, string> = {
  ok: "正常",
  warn: "注意",
  bad: "异常",
  off: "未启用",
};

export function healthLevelLabel(level: NewsHealthLevel): string {
  return HEALTH_LEVEL_LABELS[level] ?? level;
}

export function healthTone(level: NewsHealthLevel): Tone {
  return HEALTH_TONE[level] ?? "neutral";
}

const HEALTH_ITEM_TITLES = {
  ingest: "接入",
  broker: "队列",
  model: "模型",
  delivery: "推送",
} as const;

/**
 * The card's eyebrow names the pipeline stage in the vocabulary the workers, queues and logs already use, so a
 * card lines up with the thing an operator would grep. The Chinese title beside it is the server's sentence.
 */
const HEALTH_ITEM_EYEBROWS = {
  ingest: "Ingest",
  broker: "Broker",
  model: "Model",
  delivery: "Delivery",
} as const;

export type HealthItemKey = keyof typeof HEALTH_ITEM_TITLES;
export const HEALTH_ITEM_KEYS: readonly HealthItemKey[] = ["ingest", "broker", "model", "delivery"];

export function healthItemTitle(key: HealthItemKey): string {
  return HEALTH_ITEM_TITLES[key];
}

export function healthItemEyebrow(key: HealthItemKey): string {
  return HEALTH_ITEM_EYEBROWS[key];
}

const REASON_STAGE_TITLES: Record<string, string> = {
  gate: "未送审",
  drop: "模型/规则不推",
  throttle: "重复拦截",
  push: "推送依据",
  degraded: "模型降级",
  ungrounded: "符号未落标的表",
};

export function reasonStageLabel(stage: string): string {
  return REASON_STAGE_TITLES[stage] ?? stage;
}

/** Why an Event went where it went, in the pipeline's tone vocabulary — never red or green. */
const REASON_STAGE_TONE: Record<string, Tone> = {
  gate: "neutral",
  drop: "neutral",
  throttle: "caution",
  push: "done",
  degraded: "alert",
  // A provider tag that names nothing is something to fix, not something that failed: amber, like limiting.
  ungrounded: "caution",
};

export function reasonStageTone(stage: string): Tone {
  return REASON_STAGE_TONE[stage] ?? "neutral";
}

/**
 * Three node colours and no more: grey for a step that simply happened, indigo for one where the pipeline
 * decided something, amber for one that held the Event back. `gate` is grey because most Events pass it —
 * the step's own `summary_zh` says when one did not.
 */
const TIMELINE_STAGE_TONE: Record<NewsTimelineStep["stage"], Tone> = {
  received: "neutral",
  gate: "neutral",
  triage: "done",
  decide: "done",
  delivery: "done",
};

export function timelineStageTone(stage: NewsTimelineStep["stage"]): Tone {
  return TIMELINE_STAGE_TONE[stage] ?? "neutral";
}

/**
 * End to end, from the first recorded step to the last. Two server timestamps subtracted — the console reports
 * how long the pipeline took, it does not measure it.
 */
export function timelineEndToEnd(steps: NewsTimelineStep[]): string {
  if (steps.length < 2) return "每一步一句话；展开可看原始字段";
  return `端到端 ${optionalDuration(steps[steps.length - 1].at_ms - steps[0].at_ms)}`;
}

export function hoursLabel(hours: number | null): string {
  if (hours == null) return "全部时间";
  if (hours < 24) return `最近 ${hours} 小时`;
  return hours % 24 === 0 ? `最近 ${hours / 24} 天` : `最近 ${hours} 小时`;
}

export function relativeTime(value: number): string {
  const minutes = Math.max(0, Math.floor((Date.now() - value) / 60_000));
  if (minutes < 1) return "刚刚";
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  return hours < 48 ? `${hours} 小时前` : `${Math.floor(hours / 24)} 天前`;
}

export function absoluteTime(value: number): string {
  return formatNewsLocalTimestamp(value);
}

/** `HH:MM` for feed rows; the full timestamp lives in the title attribute. */
export function clockTime(value: number): string {
  return absoluteTime(value).slice(11, 16);
}

export function displayTime(value: number): string {
  return `${absoluteTime(value)} · ${relativeTime(value)}`;
}

export function optionalTime(value: number | null | undefined): string {
  return value == null ? "尚无" : absoluteTime(value);
}

export function optionalDuration(value: number | null | undefined): string {
  if (value == null) return "—";
  return value < 1_000 ? `${Math.round(value)} ms` : `${(value / 1_000).toFixed(1)} s`;
}

export function validExternalUrl(value: string | null | undefined): string | null {
  const normalized = value?.trim() ?? "";
  return /^https?:\/\//i.test(normalized) ? normalized : null;
}

export function formatCount(value: number): string {
  return new Intl.NumberFormat("zh-CN").format(value);
}

/**
 * Ticker chips for a row or hero: provider prefixes stripped and deduplicated. How many fit is the caller's
 * decision — a row shows three and counts the rest, the detail page lists them all.
 */
export function displayAssets(grounded: readonly string[]): string[] {
  return Array.from(new Set(grounded.map((symbol) => symbol.replace(/^XYZ-/, "").toUpperCase())));
}

/**
 * The same chips, but resolved: each provider tag paired with what it names on a venue (#87).
 *
 * The server sends `assets` alongside the raw `grounded_assets`; a response served before #87, or one whose
 * Event carried tags the resolver never saw, falls back to the bare symbol with `listed: false` — an unknown
 * tag reads as "we cannot place this", never as a confirmed listing.
 */
export function displayAssetRefs(
  grounded: readonly string[],
  assets: readonly NewsAssetRef[] | undefined,
): NewsAssetRef[] {
  const bySymbol = new Map(
    (assets ?? []).map((asset) => [asset.symbol.replace(/^XYZ-/, "").toUpperCase(), asset]),
  );
  return displayAssets(grounded).map(
    (symbol) => bySymbol.get(symbol) ?? { base_symbol: symbol, listed: false, symbol, venue: null },
  );
}

export function percent(numerator: number, denominator: number): string {
  if (!denominator) return "—";
  const share = (numerator / denominator) * 100;
  return share >= 10 ? `${share.toFixed(0)}%` : `${share.toFixed(1)}%`;
}

/**
 * Which hour an Event belongs to, and how that hour reads as a heading (design proposal 5).
 *
 * A real-time stream costs the reader any sense of when they are after two screens of scrolling. The bucket
 * is the local hour of `opened_at_ms` — the provider's publication time, the same anchor the row's clock and
 * the reaction measurement use — so a group heading and the rows under it can never disagree.
 */
export function hourBucketKey(value: number): string {
  return absoluteTime(value).slice(0, 13);
}

export function hourBucketLabel(value: number, withDay = false): string {
  const stamp = absoluteTime(value);
  const hour = Number(stamp.slice(11, 13));
  const span = `${stamp.slice(11, 13)}:00 — ${String((hour + 1) % 24).padStart(2, "0")}:00`;
  // The feed window reaches 72 h, and 加载更多 walks further back still. Two headings reading `03:00 — 04:00`
  // three days apart, with nothing between them to say so, is worse than no heading at all.
  return withDay ? `${dayBucketLabel(value)} ${span}` : span;
}

/** `08-21` — the day an hour group belongs to, shown when the window spans more than one. */
export function dayBucketLabel(value: number): string {
  return absoluteTime(value).slice(5, 10);
}
