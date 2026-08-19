import { formatNewsLocalTimestamp } from "./newsTime";
import type {
  NewsFeedOutcome,
  NewsHealthLevel,
  NewsOutcomeKind,
  NewsTimelineStep,
} from "./useNewsPage";

/**
 * UI-only copy. Every business word (rules, admissions, error codes, event types, directions) arrives from the
 * API already in Chinese (`outcome.text_zh`, `*_zh`, `label_zh`); this file only names UI affordances and maps
 * server enums to visual tone.
 */

export type Tone = "positive" | "info" | "caution" | "negative" | "neutral";

const OUTCOME_TONE: Record<NewsOutcomeKind, Tone> = {
  delivered: "positive",
  pending_delivery: "info",
  queued_triage: "info",
  queued_publish: "info",
  throttled: "caution",
  dropped: "neutral",
  held_gate: "neutral",
  held_recovery: "neutral",
  degraded_dropped: "negative",
  delivery_failed: "negative",
};

export function outcomeTone(kind: NewsOutcomeKind): Tone {
  return OUTCOME_TONE[kind] ?? "neutral";
}

/**
 * Market direction → visual tone and a colour-independent arrow. Both maps are UI affordances: the Chinese
 * word itself always comes from the server as `direction_zh`. `neutral` and `unclear` intentionally resolve to
 * the quiet tone so the ~42% of neutral verdicts do not drown the red/green ones.
 */
const DIRECTION_TONE: Record<string, Tone> = {
  bullish: "positive",
  bearish: "negative",
  neutral: "neutral",
  unclear: "neutral",
};

const DIRECTION_GLYPH: Record<string, string> = {
  bullish: "\u25b2",
  bearish: "\u25bc",
  neutral: "\u2014",
  unclear: "?",
};

export function directionTone(direction: string | null | undefined): Tone {
  return DIRECTION_TONE[direction ?? ""] ?? "neutral";
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
  if (level === "ok") return "positive";
  if (level === "warn") return "caution";
  if (level === "bad") return "negative";
  return "neutral";
}

const HEALTH_ITEM_TITLES = {
  ingest: "接入",
  broker: "队列",
  model: "模型",
  delivery: "推送",
} as const;

export type HealthItemKey = keyof typeof HEALTH_ITEM_TITLES;
export const HEALTH_ITEM_KEYS: readonly HealthItemKey[] = ["ingest", "broker", "model", "delivery"];

export function healthItemTitle(key: HealthItemKey): string {
  return HEALTH_ITEM_TITLES[key];
}

const REASON_STAGE_TITLES: Record<string, string> = {
  gate: "未送审",
  drop: "模型/规则不推",
  throttle: "限流",
  push: "推送依据",
  degraded: "模型降级",
};

export function reasonStageLabel(stage: string): string {
  return REASON_STAGE_TITLES[stage] ?? stage;
}

const TIMELINE_STAGE_TONE: Record<NewsTimelineStep["stage"], Tone> = {
  received: "neutral",
  gate: "neutral",
  triage: "info",
  decide: "info",
  delivery: "positive",
};

export function timelineStageTone(stage: NewsTimelineStep["stage"]): Tone {
  return TIMELINE_STAGE_TONE[stage] ?? "neutral";
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

const MAX_ASSET_CHIPS = 4;

/** Ticker chips for a row/hero: provider prefixes stripped, deduplicated, at most four. */
export function displayAssets(grounded: readonly string[]): string[] {
  return Array.from(
    new Set(grounded.map((symbol) => symbol.replace(/^XYZ-/, "").toUpperCase())),
  ).slice(0, MAX_ASSET_CHIPS);
}

export function percent(numerator: number, denominator: number): string {
  if (!denominator) return "—";
  const share = (numerator / denominator) * 100;
  return share >= 10 ? `${share.toFixed(0)}%` : `${share.toFixed(1)}%`;
}
