import {
  NEWS_FEED_DEFAULT_HOURS,
  NEWS_FEED_DIRECTIONS,
  NEWS_FEED_FINAL_DECISIONS,
  NEWS_FEED_HOURS,
  NEWS_FEED_OUTCOMES,
  NEWS_EVENT_KINDS,
  type NewsFeedFilters,
  type NewsFeedDirection,
  type NewsFeedOutcome,
  type NewsEventKind,
} from "../api/newsQueries";

/**
 * The feed's shareable state, parsed from and written back to the URL. Every value here mirrors a server
 * parameter exactly. The browser normalizes the bounded controls it renders; taxonomy codes stay opaque and
 * the server remains their sole vocabulary authority.
 */
export type FeedFilterChanges = Partial<Omit<NewsFeedFilters, "q">> & { q?: string | null };

/*
 * `telemetry_deterministic` left this list with the admission itself (#553 PR-1): market observations are
 * not admitted to the Event feed at all, so the chip could only ever have returned an empty page.
 */
export const KNOWN_ADMISSIONS = [
  "candidate",
  "listing_deterministic",
  "recovery",
  "suppressed_low_signal",
  "suppressed_pr_template",
] as const;

export const ADMISSION_FILTER_LABELS: Record<string, string> = {
  candidate: "已送审",
  listing_deterministic: "上币/下币公告",
  recovery: "补抄件",
  suppressed_low_signal: "低分噪音（未送审）",
  suppressed_pr_template: "律所模板（未送审）",
};
export function parseFeedFilters(searchParams: URLSearchParams): NewsFeedFilters {
  return {
    admission: searchParams.get("admission") || null,
    eventFamilies: parseOpaqueList(searchParams.get("event_family")),
    changeStates: parseOpaqueList(searchParams.get("change_state")),
    assertionStatuses: parseOpaqueList(searchParams.get("assertion_status")),
    sourceAuthorities: parseOpaqueList(searchParams.get("source_authority")),
    subjectCodes: parseOpaqueList(searchParams.get("subject_code")),
    finalDecisions: parseList(searchParams.get("final_decision"), NEWS_FEED_FINAL_DECISIONS),
    eventKinds: parseList(searchParams.get("event_kind"), NEWS_EVENT_KINDS),
    hours: parseHours(searchParams.get("hours")),
    outcome: parseOutcome(searchParams.get("outcome")),
    directions: parseList(searchParams.get("direction"), NEWS_FEED_DIRECTIONS),
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
  for (const name of ["q", "admission", "symbol"] as const) {
    const value = changes[name] === undefined ? filters[name] : changes[name];
    if (value) params.set(name, String(value));
    else params.delete(name);
  }
  const outcome = changes.outcome === undefined ? filters.outcome : changes.outcome;
  params.set("outcome", outcome ?? "all");
  const hours = changes.hours === undefined ? filters.hours : changes.hours;
  params.set("hours", String(hours ?? NEWS_FEED_DEFAULT_HOURS));
  const directions = changes.directions === undefined ? filters.directions : changes.directions;
  if (directions.length) params.set("direction", directions.join(","));
  for (const [name, values] of filterLists(filters, changes)) {
    if (values.length) params.set(name, values.join(","));
  }
  return params;
}

function filterLists(
  filters: NewsFeedFilters,
  changes: FeedFilterChanges,
): Array<[string, readonly string[]]> {
  return [
    ["event_family", changes.eventFamilies ?? filters.eventFamilies],
    ["change_state", changes.changeStates ?? filters.changeStates],
    ["assertion_status", changes.assertionStatuses ?? filters.assertionStatuses],
    ["source_authority", changes.sourceAuthorities ?? filters.sourceAuthorities],
    ["subject_code", changes.subjectCodes ?? filters.subjectCodes],
    ["final_decision", changes.finalDecisions ?? filters.finalDecisions],
    ["event_kind", changes.eventKinds ?? filters.eventKinds],
  ];
}

export function hasAdvancedFilters(filters: NewsFeedFilters): boolean {
  return Boolean(
    filters.q ||
    filters.admission ||
    filters.symbol ||
    filters.eventFamilies.length ||
    filters.changeStates.length ||
    filters.assertionStatuses.length ||
    filters.sourceAuthorities.length ||
    filters.subjectCodes.length ||
    filters.finalDecisions.length ||
    filters.eventKinds.length ||
    filters.directions.length,
  );
}

export function parseOutcome(value: string | null): NewsFeedOutcome | null {
  if (value === "all") return null;
  if (value == null || value === "") return "pushed";
  return NEWS_FEED_OUTCOMES.find((candidate) => candidate === value) ?? "pushed";
}

/** `hours` absent → default window; anything else must be one of the three visible choices. */
export function parseHours(value: string | null): number | null {
  if (value == null || value === "") return NEWS_FEED_DEFAULT_HOURS;
  const parsed = Number.parseInt(value, 10);
  return NEWS_FEED_HOURS.includes(parsed) ? parsed : NEWS_FEED_DEFAULT_HOURS;
}

function parseList<T extends string>(value: string | null, allowed: readonly T[]): T[] {
  if (!value) return [];
  const selected = new Set(value.split(","));
  if ([...selected].some((item) => !allowed.includes(item as T))) return [];
  return allowed.filter((item) => selected.has(item));
}

function parseOpaqueList(value: string | null): string[] {
  if (!value) return [];
  const selected = value.split(",");
  return selected.some((item) => !item) || new Set(selected).size !== selected.length
    ? []
    : selected.sort();
}

export function toggleFilterValue<T extends NewsFeedDirection | NewsEventKind>(
  selected: readonly T[],
  value: T,
  order: readonly T[],
): T[] {
  const next = new Set(selected);
  if (next.has(value)) next.delete(value);
  else next.add(value);
  return order.filter((item) => next.has(item));
}

export function normalizeSymbol(value: string | null | undefined): string | null {
  const normalized = value?.trim().toUpperCase() ?? "";
  return normalized || null;
}

export function withSelectedOption(options: readonly string[], selected: string | null): string[] {
  if (!selected || options.includes(selected)) return [...options];
  return [selected, ...options];
}
