import {
  NEWS_FEED_DECISIONS,
  NEWS_FEED_DEFAULT_HOURS,
  NEWS_FEED_HOURS,
  NEWS_FEED_OUTCOMES,
  type NewsFeedDecision,
  type NewsFeedFilters,
  type NewsFeedOutcome,
} from "../api/newsQueries";

/**
 * The feed's shareable state, parsed from and written back to the URL. Every value here mirrors a server
 * parameter exactly; unknown values are dropped rather than forwarded, so a stale bookmark degrades to the
 * default window instead of a 4xx.
 */
export type FeedFilterChanges = Partial<Omit<NewsFeedFilters, "q">> & { q?: string | null };

export const KNOWN_FAMILIES = ["market_telemetry", "filing", "disaster", "general"] as const;
export const KNOWN_ADMISSIONS = [
  "candidate",
  "listing_deterministic",
  "telemetry_deterministic",
  "recovery",
  "suppressed_low_signal",
  "suppressed_pr_template",
] as const;

// Power-user filter copy for the raw enums the feed still accepts (URL-owned; the API validates them).
export const FAMILY_FILTER_LABELS: Record<string, string> = {
  market_telemetry: "盘口数据",
  filing: "公告/申报",
  disaster: "灾害",
  general: "综合",
};
export const ADMISSION_FILTER_LABELS: Record<string, string> = {
  candidate: "已送审",
  listing_deterministic: "上币/下币公告",
  telemetry_deterministic: "持仓异动遥测",
  recovery: "补抄件",
  suppressed_low_signal: "低分噪音（未送审）",
  suppressed_pr_template: "律所模板（未送审）",
};
export const DECISION_FILTER_LABELS: Record<NewsFeedDecision, string> = {
  push: "推送",
  escalate: "重点推送",
  drop: "不推",
  throttled: "重复拦截",
  degraded: "降级",
};

export function parseFeedFilters(searchParams: URLSearchParams): NewsFeedFilters {
  return {
    admission: searchParams.get("admission") || null,
    decision: parseDecision(searchParams.get("decision")),
    family: searchParams.get("family") || null,
    hours: parseHours(searchParams.get("hours")),
    outcome: parseOutcome(searchParams.get("outcome")),
    q: searchParams.get("q")?.trim() ?? "",
    symbol: normalizeSymbol(searchParams.get("symbol")),
  };
}

/** Applies `changes` on top of `filters` and returns the search params to navigate to. */
export function nextFeedParams(
  filters: NewsFeedFilters,
  changes: FeedFilterChanges,
): URLSearchParams {
  const params = new URLSearchParams();
  for (const name of ["q", "family", "admission", "decision", "symbol", "outcome"] as const) {
    const value = changes[name] === undefined ? filters[name] : changes[name];
    if (value) params.set(name, String(value));
    else params.delete(name);
  }
  const hours = changes.hours === undefined ? filters.hours : changes.hours;
  params.set("hours", hours == null ? "all" : String(hours));
  return params;
}

export function hasAdvancedFilters(filters: NewsFeedFilters): boolean {
  return Boolean(
    filters.q || filters.family || filters.admission || filters.decision || filters.symbol,
  );
}

export function parseDecision(value: string | null): NewsFeedDecision | null {
  return NEWS_FEED_DECISIONS.find((candidate) => candidate === value) ?? null;
}

export function parseOutcome(value: string | null): NewsFeedOutcome | null {
  return NEWS_FEED_OUTCOMES.find((candidate) => candidate === value) ?? null;
}

/** `hours` absent → default window; `all` → whole retention; anything else must be a known window. */
export function parseHours(value: string | null): number | null {
  if (value == null || value === "") return NEWS_FEED_DEFAULT_HOURS;
  if (value === "all") return null;
  const parsed = Number.parseInt(value, 10);
  return NEWS_FEED_HOURS.includes(parsed) ? parsed : NEWS_FEED_DEFAULT_HOURS;
}

export function normalizeSymbol(value: string | null | undefined): string | null {
  const normalized = value?.trim().toUpperCase() ?? "";
  return normalized ? normalized.slice(0, 16) : null;
}

export function withSelectedOption(options: readonly string[], selected: string | null): string[] {
  if (!selected || options.includes(selected)) return [...options];
  return [selected, ...options];
}
