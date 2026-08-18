import type { TokenPostRange, WindowKey } from "@lib/types";

export type NewsFeedQueryKeyFilters = {
  admission: string | null;
  decision: string | null;
  family: string | null;
  priority: string | null;
  q: string;
  sort: "latest" | "priority";
  symbol: string | null;
};

export const queryKeys = {
  bootstrap: () => ["bootstrap"] as const,
  status: () => ["status"] as const,
  tokenCaseRoot: () => ["token-case"] as const,
  tokenCase: (targetKey: string | null, window: WindowKey, postsLimit: number) =>
    ["token-case", targetKey, window, postsLimit] as const,
  triggerTargetPost: (targetKey: string | null, eventId: string | null) =>
    ["trigger-target-post", targetKey, eventId] as const,
  searchInspect: (token: string, q: string, window: WindowKey) =>
    ["search-inspect", token, q, window] as const,
  macroPage: (pageId: string) => ["macro", "page", pageId] as const,
  macroSeries: (conceptKeys: string[], window: string) =>
    ["macro", "series", [...conceptKeys].sort(), window] as const,
  newsFeed: (filters: NewsFeedQueryKeyFilters) =>
    [
      "news-feed",
      filters.q,
      filters.family ?? "",
      filters.admission ?? "",
      filters.priority ?? "",
      filters.decision ?? "",
      filters.symbol ?? "",
      filters.sort,
    ] as const,
  newsFeedHistory: (filters: NewsFeedQueryKeyFilters, firstCursor: string) =>
    [
      "news-feed-history",
      filters.q,
      filters.family ?? "",
      filters.admission ?? "",
      filters.priority ?? "",
      filters.decision ?? "",
      filters.symbol ?? "",
      filters.sort,
      firstCursor,
    ] as const,
  newsEvent: (eventId: string) => ["news-event", eventId] as const,
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
