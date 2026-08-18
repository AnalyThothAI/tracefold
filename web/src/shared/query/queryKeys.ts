export type NewsFeedQueryKeyFilters = {
  admission: string | null;
  decision: string | null;
  family: string | null;
  hours: number | null;
  outcome: string | null;
  priority: string | null;
  q: string;
  sort: "latest" | "priority";
  symbol: string | null;
};

const newsFeedIdentity = (filters: NewsFeedQueryKeyFilters) =>
  [
    filters.q,
    filters.family ?? "",
    filters.admission ?? "",
    filters.priority ?? "",
    filters.decision ?? "",
    filters.symbol ?? "",
    filters.sort,
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
};
