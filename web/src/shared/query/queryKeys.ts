import type { TokenPostRange, WindowKey } from "@lib/types";
import type { TokenRadarVenueFilter } from "@lib/venue";

export const queryKeys = {
  bootstrap: () => ["bootstrap"] as const,
  status: () => ["status"] as const,
  tokenRadarRoot: () => ["token-radar"] as const,
  tokenRadar: (window: WindowKey, venue: TokenRadarVenueFilter, limit: number) =>
    ["token-radar", window, venue, limit] as const,
  tokenCaseRoot: () => ["token-case"] as const,
  tokenCase: (targetKey: string | null, window: WindowKey, postsLimit: number) =>
    ["token-case", targetKey, window, postsLimit] as const,
  searchInspect: (token: string, q: string, window: WindowKey) =>
    ["search-inspect", token, q, window] as const,
  stocksRadar: (window: WindowKey, limit: number) => ["stocks-radar", window, limit] as const,
  macroPage: (pageId: string) => ["macro", "page", pageId] as const,
  macroSeries: (conceptKeys: string[], window: string) =>
    ["macro", "series", [...conceptKeys].sort(), window] as const,
  newsFeed: (category: string | null, sort: "importance" | "latest") =>
    ["news-feed", category ?? "", sort] as const,
  newsStory: (storyId: string) => ["news-story", storyId] as const,
  newsBrief: () => ["news-brief"] as const,
  newsStatus: () => ["news-status"] as const,
  targetSocialTimeline: (targetKey: string | null, window: WindowKey) =>
    ["target-social-timeline", targetKey, window] as const,
  targetPosts: (
    targetKey: string | null,
    window: WindowKey,
    range: TokenPostRange,
    limit: number,
  ) => ["target-posts", targetKey, window, range, limit] as const,
  opsDiagnostics: () => ["ops-diagnostics"] as const,
  opsQueue: (queueName: string | null, status: string | null, limit: number) =>
    ["ops-queue", queueName ?? "", status ?? "", limit] as const,
};
