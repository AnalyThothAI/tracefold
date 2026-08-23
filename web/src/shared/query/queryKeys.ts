export type NewsFeedQueryKeyFilters = {
  admission: string | null;
  decision: string | null;
  family: string | null;
  hours: number | null;
  outcome: string | null;
  q: string;
  symbol: string | null;
};

export const newsFeedIdentity = (filters: NewsFeedQueryKeyFilters) =>
  [
    filters.q,
    filters.family ?? "",
    filters.admission ?? "",
    filters.decision ?? "",
    filters.symbol ?? "",
    filters.outcome ?? "",
    filters.hours == null ? "" : String(filters.hours),
  ] as const;

export const queryKeys = {
  bootstrap: () => ["bootstrap"] as const,
  status: () => ["status"] as const,
  newsFeed: (filters: NewsFeedQueryKeyFilters) =>
    ["news-feed", ...newsFeedIdentity(filters)] as const,
  newsFeedHistory: (filters: NewsFeedQueryKeyFilters, firstCursor: string) =>
    ["news-feed-history", ...newsFeedIdentity(filters), firstCursor] as const,
  newsEvent: (eventId: string) => ["news-event", eventId] as const,
  newsStatus: () => ["news-status"] as const,
  // #88: the quote key is the sorted symbol batch, so the feed and an open Event share one poll.
  newsQuotes: (symbols: readonly string[]) => ["news-quotes", symbols.join(",")] as const,
  newsReview: (identity: string) => ["news-review", identity] as const,
};
