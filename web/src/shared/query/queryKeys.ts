export type NewsFeedQueryKeyFilters = {
  admission: string | null;
  eventFamilies: readonly string[];
  changeStates: readonly string[];
  assertionStatuses: readonly string[];
  sourceAuthorities: readonly string[];
  subjectCodes: readonly string[];
  finalDecisions: readonly string[];
  eventKinds: readonly string[];
  hours: number | null;
  outcome: string | null;
  directions: readonly string[];
  q: string;
  symbol: string | null;
};

export const newsFeedIdentity = (filters: NewsFeedQueryKeyFilters) =>
  [
    filters.q,
    filters.eventFamilies.join(","),
    filters.changeStates.join(","),
    filters.assertionStatuses.join(","),
    filters.sourceAuthorities.join(","),
    filters.subjectCodes.join(","),
    filters.finalDecisions.join(","),
    filters.eventKinds.join(","),
    filters.admission ?? "",
    filters.symbol ?? "",
    filters.outcome ?? "",
    filters.hours == null ? "" : String(filters.hours),
    filters.directions.join(","),
  ] as const;

export const queryKeys = {
  bootstrap: () => ["bootstrap"] as const,
  status: () => ["status"] as const,
  newsFeed: (filters: NewsFeedQueryKeyFilters) =>
    ["news-feed", ...newsFeedIdentity(filters)] as const,
  newsFeedHistory: (filters: NewsFeedQueryKeyFilters, firstCursor: string) =>
    ["news-feed-history", ...newsFeedIdentity(filters), firstCursor] as const,
  newsEvent: (eventId: string) => ["news-event", eventId] as const,
  // #553 PR-1: market observations are their own endpoint and their own key. The kind filter is part of the
  // identity because every filter is a real request — the per-kind `sources` strip describes the whole
  // window, so a browser-side split would leave it disagreeing with the rows under it.
  newsMarket: (kind: string) => ["news-market", kind] as const,
  newsMarketHistory: (kind: string, firstCursor: string) =>
    ["news-market-history", kind, firstCursor] as const,
  // One expanded group's Item. Not polled: a stored provider payload cannot change.
  newsMarketItem: (itemId: string) => ["news-market-item", itemId] as const,
  newsStatus: () => ["news-status"] as const,
  // #207 PR-W1: identity only, and identity does not change on a poll — the token page's Events, price and
  // rank window each keep their own key and their own rhythm.
  newsSymbol: (base: string) => ["news-symbol", base] as const,
  // #88: the quote key is the sorted symbol batch, so the feed and an open Event share one poll.
  newsQuotes: (symbols: readonly string[]) => ["news-quotes", symbols.join(",")] as const,
  // The Signal lane's own keys. Separate from News so a 15 s trading poll cannot evict the
  // feed page a reader is scrolled into.
  tradingStatus: () => ["trading-status"] as const,
  tradingCases: (underlying: string) => ["trading-cases", underlying] as const,
  // #528 PR-2: one key for the folded execution read model — the desk's Signal rows and Command rows
  // arrive in the same response, so they cannot disagree about the window they describe.
  tradingExecutions: () => ["trading-executions"] as const,
};
