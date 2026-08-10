import type { TokenPostRange, WindowKey } from "@lib/types";

export const queryKeys = {
  bootstrap: () => ["bootstrap"] as const,
  status: () => ["status"] as const,
  tokenRadar: () => ["token-radar"] as const,
  tokenCaseRoot: () => ["token-case"] as const,
  tokenCase: (targetKey: string | null, window: WindowKey, postsLimit: number) =>
    ["token-case", targetKey, window, postsLimit] as const,
  triggerTargetPost: (targetKey: string | null, eventId: string | null) =>
    ["trigger-target-post", targetKey, eventId] as const,
  searchInspect: (token: string, q: string, window: WindowKey) =>
    ["search-inspect", token, q, window] as const,
  stocksRadar: (window: WindowKey, limit: number) => ["stocks-radar", window, limit] as const,
  macroPage: (pageId: string) => ["macro", "page", pageId] as const,
  macroSeries: (conceptKeys: string[], window: string) =>
    ["macro", "series", [...conceptKeys].sort(), window] as const,
  newsFeed: (
    q: string,
    category: string | null,
    level: string | null,
    reportingOrigin: string | null,
    providerScoreGt: number | null,
    sort: "importance" | "latest",
  ) =>
    [
      "news-feed",
      q,
      category ?? "",
      level ?? "",
      reportingOrigin ?? "",
      providerScoreGt,
      sort,
    ] as const,
  newsStory: (storyId: string) => ["news-story", storyId] as const,
  newsBrief: () => ["news-brief"] as const,
  newsStatus: () => ["news-status"] as const,
  newsSources: () => ["news-sources"] as const,
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
