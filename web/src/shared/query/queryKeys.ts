import type { ScopeKey, TokenPostRange, WindowKey } from "@lib/types";
import type { TokenRadarVenueFilter } from "@lib/venue";
import type { TokenCaseScope } from "@shared/model/tokenCaseViewModel";

export const queryKeys = {
  bootstrap: () => ["bootstrap"] as const,
  status: () => ["status"] as const,
  tokenRadarRoot: () => ["token-radar"] as const,
  tokenRadar: (window: WindowKey, scope: ScopeKey, venue: TokenRadarVenueFilter, limit: number) =>
    ["token-radar", window, scope, venue, limit] as const,
  tokenCaseRoot: () => ["token-case"] as const,
  tokenCase: (
    targetKey: string | null,
    window: WindowKey,
    scope: TokenCaseScope,
    postsLimit: number,
  ) => ["token-case", targetKey, window, scope, postsLimit] as const,
  searchInspect: (token: string, q: string, window: WindowKey, scope: ScopeKey) =>
    ["search-inspect", token, q, window, scope] as const,
  stocksRadar: (window: WindowKey, scope: ScopeKey, limit: number) =>
    ["stocks-radar", window, scope, limit] as const,
  macroPage: (pageId: string) => ["macro", "page", pageId] as const,
  macroSeries: (conceptKeys: string[], window: string) =>
    ["macro", "series", [...conceptKeys].sort(), window] as const,
  newsFeed: (category: string | null, sort: "importance" | "latest") =>
    ["news-feed", category ?? "", sort] as const,
  newsStory: (storyId: string) => ["news-story", storyId] as const,
  newsBrief: () => ["news-brief"] as const,
  newsSources: () => ["news-sources"] as const,
  targetSocialTimeline: (targetKey: string | null, window: WindowKey, scope: ScopeKey) =>
    ["target-social-timeline", targetKey, window, scope] as const,
  targetPosts: (
    targetKey: string | null,
    window: WindowKey,
    scope: ScopeKey | TokenCaseScope,
    range: TokenPostRange,
    limit: number,
  ) => ["target-posts", targetKey, window, scope, range, limit] as const,
  notifications: () => ["notifications"] as const,
  opsDiagnostics: () => ["ops-diagnostics"] as const,
  opsQueue: (queueName: string | null, status: string | null, limit: number) =>
    ["ops-queue", queueName ?? "", status ?? "", limit] as const,
  watchlistHandlesOverview: () => ["watchlist-handles-overview"] as const,
  watchlistHandleOverview: (handle: string) => ["watchlist-handle-overview", handle] as const,
  watchlistHandleTimeline: (handle: string, limit: number) =>
    ["watchlist-handle-timeline", handle, limit] as const,
};
