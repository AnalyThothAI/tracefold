import { NEWS_EVENT_KINDS, type NewsFeedEvent } from "../api/newsQueries";

import { eventKindLabel } from "./newsLabels";

/**
 * The token page's Event-kind tabs (#207 PR-W1).
 *
 * Unlike the OI monitor's tabs these filter the loaded window in the browser, and that is deliberate rather
 * than a shortcut: `/api/news/feed` has no `lane` parameter, so a server-side count would describe a window
 * the table is not showing. Both the tab count and the rows under it come from the same loaded set.
 */
export const SYMBOL_LANES = ["all", "pushed", ...NEWS_EVENT_KINDS] as const;
export type NewsSymbolLane = (typeof SYMBOL_LANES)[number];

export function symbolLaneLabel(lane: NewsSymbolLane): string {
  if (lane === "all") return "全部";
  if (lane === "pushed") return "已推送";
  return eventKindLabel(lane);
}

export function parseSymbolLane(value: string | null): NewsSymbolLane {
  return SYMBOL_LANES.includes(value as NewsSymbolLane) ? (value as NewsSymbolLane) : "all";
}

export function matchesLane(event: NewsFeedEvent, lane: NewsSymbolLane): boolean {
  if (lane === "pushed") return event.outcome.group === "pushed";
  if (lane === "all") return true;
  return event.event_kind === lane;
}
