import {
  COCKPIT_STATUS_REFETCH_MS,
  useCockpitStatusQuery,
} from "@features/cockpit/api/useCockpitStatusQuery";
import {
  NEWS_FEED_REFETCH_MS,
  NEWS_QUOTES_REFETCH_MS,
  type NewsFeedFilters,
  useNewsEventWithToken,
  useNewsFeedWindowWithToken,
  useNewsQuotesWithToken,
} from "@features/news/api/newsQueries";
import {
  TRADING_STATUS_REFETCH_MS,
  useTradingStatusWithToken,
} from "@features/trading/api/tradingQueries";
import { queryKeys } from "@shared/query/queryKeys";
import {
  QueryClient,
  QueryClientProvider,
  onlineManager,
  type QueryKey,
  type QueryObserverOptions,
} from "@tanstack/react-query";
import { renderHook } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

const baseFilters: NewsFeedFilters = {
  admission: null,
  eventKinds: [],
  hours: null,
  outcome: null,
  q: "",
  sourceAuthorities: [],
  subjectCodes: [],
  symbol: null,
};

describe("query hook category contracts", () => {
  beforeEach(() => onlineManager.setOnline(false));
  afterEach(() => onlineManager.setOnline(true));

  it.each([
    {
      interval: TRADING_STATUS_REFETCH_MS,
      key: queryKeys.tradingStatus(),
      name: "Trading status",
      useObservedQuery: () => useTradingStatusWithToken("token"),
    },
    {
      interval: COCKPIT_STATUS_REFETCH_MS,
      key: queryKeys.status(),
      name: "cockpit runtime status",
      useObservedQuery: () => useCockpitStatusQuery({ token: "token" }),
    },
  ])(
    "keeps the $name polling query enabled on its owned key and rhythm",
    ({ useObservedQuery, key, interval }) => {
      const options = captureQueryOptions(useObservedQuery, key);

      expect(options.enabled).toBe(true);
      expect(options.refetchInterval).toBe(interval);
    },
  );

  it.each([
    {
      key: queryKeys.newsFeedWindow(baseFilters, 0),
      name: "missing bearer",
      useObservedQuery: () => useNewsFeedWindowWithToken("", baseFilters),
    },
    {
      key: queryKeys.newsEvent(""),
      name: "missing Event identity",
      useObservedQuery: () => useNewsEventWithToken("token", null),
    },
  ])("disables conditional queries for $name", ({ useObservedQuery, key }) => {
    expect(captureQueryOptions(useObservedQuery, key).enabled).toBe(false);
  });

  it("keeps one bounded Feed cursor chain with polling only at the latest entry", () => {
    const options = captureQueryOptions(
      () => useNewsFeedWindowWithToken("token", baseFilters),
      queryKeys.newsFeedWindow(baseFilters, 0),
    );

    expect(options.enabled).toBe(true);
    expect((options as { maxPages?: number }).maxPages).toBe(3);
    expect(options.staleTime).toBe(2_000);
    expect(typeof options.refetchInterval).toBe("function");
    expect(NEWS_FEED_REFETCH_MS).toBeGreaterThan(0);
  });

  it("keeps the first-seen quote top 100, then sorts only that selected query identity", () => {
    const symbols = [
      "ZZZ",
      ...Array.from({ length: 100 }, (_, index) => `A${String(index).padStart(3, "0")}`),
    ];
    const selected = symbols.slice(0, 100).sort();
    const options = captureQueryOptions(
      () => useNewsQuotesWithToken("", symbols),
      queryKeys.newsQuotes(selected),
    );

    expect(selected).toContain("ZZZ");
    expect(options.refetchInterval).toBe(NEWS_QUOTES_REFETCH_MS);
    expect(options.refetchIntervalInBackground).toBe(false);
    expect(options.refetchOnWindowFocus).toBe(true);
  });
});

function captureQueryOptions(useObservedQuery: () => unknown, key: QueryKey) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children);
  const { unmount } = renderHook(useObservedQuery, { wrapper });
  const query = client.getQueryCache().find({ exact: true, queryKey: key });

  expect(query, `query ${JSON.stringify(key)} was not registered`).toBeDefined();
  const observer = query!.observers[0];
  expect(observer, `query ${JSON.stringify(key)} has no observer`).toBeDefined();
  const options: QueryObserverOptions = { ...observer.options };
  unmount();
  client.clear();
  return options;
}
