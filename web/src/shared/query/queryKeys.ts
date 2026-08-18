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
};
