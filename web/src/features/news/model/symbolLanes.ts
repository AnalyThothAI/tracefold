import type { NewsFeedEvent } from "../api/newsQueries";

/**
 * The token page's channel tabs (#207 PR-W1).
 *
 * Unlike the OI monitor's tabs these filter the loaded window in the browser, and that is deliberate rather
 * than a shortcut: `/api/news/feed` has no `lane` parameter, so a server-side count would describe a window
 * the table is not showing. Both the tab count and the rows under it come from the same loaded set.
 */
export const SYMBOL_LANES = ["all", "pushed", "news", "oi"] as const;
export type NewsSymbolLane = (typeof SYMBOL_LANES)[number];

export const SYMBOL_LANE_LABELS: Record<NewsSymbolLane, string> = {
  all: "全部",
  news: "新闻",
  oi: "OI 帧",
  pushed: "已推送",
};

export function parseSymbolLane(value: string | null): NewsSymbolLane {
  return SYMBOL_LANES.includes(value as NewsSymbolLane) ? (value as NewsSymbolLane) : "all";
}

/** #137's deterministic lane, by the admission the server assigned — never by parsing the title. */
export function isOiFrame(event: NewsFeedEvent): boolean {
  return event.admission === "telemetry_deterministic";
}

export function matchesLane(event: NewsFeedEvent, lane: NewsSymbolLane): boolean {
  if (lane === "pushed") return event.outcome.group === "pushed";
  if (lane === "news") return !isOiFrame(event);
  if (lane === "oi") return isOiFrame(event);
  return true;
}
