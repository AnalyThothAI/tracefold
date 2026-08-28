export type NewsFeedQueryKeyFilters = {
  admission: string | null;
  decision: string | null;
  family: string | null;
  hours: number | null;
  outcome: string | null;
  directions: readonly string[];
  channels: readonly string[];
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
    filters.directions.join(","),
    filters.channels.join(","),
  ] as const;

export const queryKeys = {
  bootstrap: () => ["bootstrap"] as const,
  status: () => ["status"] as const,
  newsFeed: (filters: NewsFeedQueryKeyFilters) =>
    ["news-feed", ...newsFeedIdentity(filters)] as const,
  newsFeedHistory: (filters: NewsFeedQueryKeyFilters, firstCursor: string) =>
    ["news-feed-history", ...newsFeedIdentity(filters), firstCursor] as const,
  newsEvent: (eventId: string) => ["news-event", eventId] as const,
  // #207: the OI monitor's own slice of the feed. Its own key, because it is a different filter set on a
  // different rhythm and must not evict the feed page a reader is scrolled into.
  newsOiFeed: (outcome: string) => ["news-oi-feed", outcome] as const,
  newsOiFeedHistory: (outcome: string, firstCursor: string) =>
    ["news-oi-feed-history", outcome, firstCursor] as const,
  newsStatus: () => ["news-status"] as const,
  // #207 PR-W1: identity only, and identity does not change on a poll — the token page's Events, price and
  // rank window each keep their own key and their own rhythm.
  newsSymbol: (base: string) => ["news-symbol", base] as const,
  // #88: the quote key is the sorted symbol batch, so the feed and an open Event share one poll.
  newsQuotes: (symbols: readonly string[]) => ["news-quotes", symbols.join(",")] as const,
  // #207 PR-W4: the capital lane's own keys. Separate from News' so a 15 s trading poll cannot evict the
  // feed page a reader is scrolled into.
  tradingStatus: () => ["trading-status"] as const,
  tradingIntents: (underlying: string, budgetDay = "") =>
    ["trading-orders", underlying, budgetDay] as const,
  tradingEventCase: (eventId: string) => ["trading-event-case", eventId] as const,
  // #269: the admission ledger's own window, shared by the frame table and the leverage list.
  tradingGate: () => ["trading-gate"] as const,
};
