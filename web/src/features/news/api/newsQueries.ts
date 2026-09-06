import { getApi } from "@lib/api/client";
import type { components } from "@lib/types/openapi";
import { newsFeedIdentity, queryKeys } from "@shared/query/queryKeys";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";

type NewsSchemas = components["schemas"];

export type NewsEventKind = NewsSchemas["NewsEventData"]["event_kind"];
/**
 * The Event vocabulary, and it is two words since #553 PR-1: an Event is editorial. OI frames, liquidations,
 * smart-money prints and unknown market sources are market observations, stored as facts and read through
 * `/api/news/market` — never admitted to the Event feed and never a kind here.
 */
export const NEWS_EVENT_KINDS = ["news", "listing"] as const satisfies readonly NewsEventKind[];

export type NewsFeedEvent = NewsSchemas["NewsFeedEventData"];
export type NewsEvent = NewsSchemas["NewsEventData"];
export type NewsFeed = NewsSchemas["NewsFeedData"];
export type NewsFeedCounts = NewsSchemas["NewsFeedCountsData"];
export type NewsFeedSearch = NewsSchemas["NewsFeedSearchData"];
export type NewsAssetRef = NewsSchemas["NewsAssetRefData"];
export type NewsSymbolNormalization = NewsSchemas["NewsSymbolNormalizationData"];
export type NewsEventDetail = NewsSchemas["NewsEventDetailData"];
export type NewsEventMember = NewsSchemas["NewsEventMemberData"];
export type NewsVerdict = NewsSchemas["NewsVerdictData"];
export type NewsDelivery = NewsSchemas["NewsDeliveryData"];
export type NewsDeliverySummary = NewsSchemas["NewsDeliverySummaryData"];
export type NewsTriageSummary = NewsSchemas["NewsTriageSummaryData"];
export type NewsStatus = NewsSchemas["NewsStatusData"];
export type NewsIncident = NewsSchemas["NewsIncidentData"];
export type NewsOutcome = NewsSchemas["NewsOutcomeData"];
export type NewsOutcomeKind = NewsOutcome["kind"];
export type NewsOutcomeGroup = NewsOutcome["group"];
export type NewsTimelineStep = NewsSchemas["NewsTimelineStepData"];
export type NewsHealth = NewsSchemas["NewsHealthData"];
export type NewsHealthItem = NewsSchemas["NewsHealthItemData"];
export type NewsHealthLevel = NewsHealthItem["level"];
export type NewsFunnel = NewsSchemas["NewsFunnelData"];
export type NewsReasonCount = NewsSchemas["NewsReasonCountData"];
export type NewsQuote = NewsSchemas["NewsQuoteData"];
export type NewsQuoteState = NewsQuote["state"];
export type NewsQuotes = NewsSchemas["NewsQuotesData"];
export type NewsReaction = NewsSchemas["NewsReactionSummaryData"];
export type NewsReactionState = NewsReaction["state"];
export type NewsEventReaction = NewsSchemas["NewsEventReactionData"];
export type NewsPriceStatus = NewsSchemas["NewsPriceStatusData"];
export type NewsSymbol = NewsSchemas["NewsSymbolData"];
export type NewsMarket = NewsSchemas["NewsMarketData"];
export type NewsMarketGroup = NewsSchemas["NewsMarketGroupData"];
export type NewsMarketObservation = NewsSchemas["NewsMarketObservationData"];
export type NewsMarketSource = NewsSchemas["NewsMarketSourceData"];
export type NewsMarketItem = NewsSchemas["NewsMarketItemData"];
export type NewsMarketKind = NewsMarketObservation["market_kind"];
export type NewsMarketParseStatus = NewsMarketObservation["parse_status"];
export type NewsSymbolContract = NewsSchemas["NewsSymbolContractData"];
export type NewsWallets = NewsSchemas["NewsWalletsData"];
export type NewsWalletRoster = NewsSchemas["NewsWalletRosterData"];
export type NewsWalletRosterMember = NewsSchemas["NewsWalletRosterMemberData"];
export type NewsWalletTapeState = NewsSchemas["NewsWalletTapeStateData"];
export type NewsWalletFillTotal = NewsSchemas["NewsWalletFillTotalData"];
export type NewsWalletCardTotal = NewsSchemas["NewsWalletCardTotalData"];
export type NewsWalletCards = NewsSchemas["NewsWalletCardsData"];
export type NewsWalletCard = NewsSchemas["NewsWalletCardData"];
export type NewsWalletCardKind = NewsWalletCard["kind"];
export type NewsWalletFillKind = NewsWalletFillTotal["kind"];

export type NewsFeedOutcome = NewsOutcomeGroup;
export type NewsFeedDirection = "bullish" | "bearish" | "neutral";
export type NewsFeedFinalDecision = "push" | "escalate" | "drop" | "throttled";
export const NEWS_FEED_OUTCOMES: readonly NewsFeedOutcome[] = ["pushed", "held", "pending"];
export const NEWS_FEED_DIRECTIONS: readonly NewsFeedDirection[] = ["bullish", "bearish", "neutral"];
/** The three windows in the approved Event-feed visual. */
export const NEWS_FEED_HOURS: readonly number[] = [1, 24, 168];
export const NEWS_FEED_DEFAULT_HOURS = 24;

export const NEWS_FEED_FINAL_DECISIONS: readonly NewsFeedFinalDecision[] = [
  "push",
  "escalate",
  "drop",
  "throttled",
];
export const NEWS_FEED_PAGE_SIZE = 25;
export const NEWS_FEED_REFETCH_MS = 3_000;
export const NEWS_STATUS_REFETCH_MS = 15_000;
export const NEWS_EVENT_REFETCH_MS = 15_000;
/**
 * Quotes have their own query key and their own rhythm (#88): a price that changed must not make the feed
 * and its counts query return a new body, and polling faster than the collector writes is pure noise. The
 * server refreshes every 20 s, so 15 s keeps the reader within one cycle of the truth without asking three times
 * for the same bytes.
 */
export const NEWS_QUOTES_REFETCH_MS = 15_000;
/** `/api/news/quotes` accepts at most this many symbols; the hook batches the visible rows into one call. */
export const NEWS_QUOTES_SYMBOL_MAX = 100;

export type NewsFeedFilters = {
  admission: string | null;
  eventFamilies: string[];
  changeStates: string[];
  assertionStatuses: string[];
  sourceAuthorities: string[];
  subjectCodes: string[];
  finalDecisions: NewsFeedFinalDecision[];
  eventKinds: NewsEventKind[];
  hours: number | null;
  outcome: NewsFeedOutcome | null;
  directions: NewsFeedDirection[];
  q: string;
  symbol: string | null;
};

/**
 * The four market observation kinds `/api/news/market` serves (#553 PR-1), in the order the page shows them.
 *
 * They are the server's own `market_kind`, not a browser grouping: `unknown_market` is what an OpenNews
 * source we have no parser for stores as, and it is a kind rather than an error state — the raw line is
 * retained either way.
 */
export const NEWS_MARKET_KINDS = [
  "oi",
  "liquidation",
  "smart_money",
  "unknown_market",
  "wallet",
] as const satisfies readonly NewsMarketKind[];
/** `/api/news/market` accepts at most 100 groups per page. */
export const NEWS_MARKET_PAGE_SIZE = 50;
/**
 * Slower than the Event feed's 3 s. A provider emits a market record when its own trigger fires — minutes
 * apart for OI, seconds apart in a liquidation cascade — and the page collapses consecutive observations of
 * one group anyway, so a faster poll re-renders the same collapsed rows.
 */
export const NEWS_MARKET_REFETCH_MS = 10_000;
/**
 * The wallet tape's own page (#572 PR-3). Same 10 s as the market list and for the same reason: the tape
 * turns every two seconds but a card is a rule firing, which is tens of times a day.
 */
export const NEWS_WALLETS_REFETCH_MS = 10_000;
/** The closed window vocabulary `/api/news/wallets/cards` accepts. */
export const NEWS_WALLET_CARD_WINDOWS = ["24h", "72h", "7d"] as const;
export type NewsWalletCardWindow = (typeof NEWS_WALLET_CARD_WINDOWS)[number];
export const NEWS_WALLET_CARDS_PAGE_SIZE = 100;

const fetchNewsFeed = async (token: string, filters: NewsFeedFilters, cursor: string | null) =>
  (
    await getApi<NewsFeed>("/api/news/feed", {
      // One page of one filter set. `newsFeedIdentity` is the same tuple the React Query key uses, so a
      // filter added there cannot be forgotten here and serve a page its `If-None-Match` never matched.
      etagKey: `news-feed:${JSON.stringify([...newsFeedIdentity(filters), cursor ?? "first"])}`,
      params: {
        admission: filters.admission,
        assertion_status: filters.assertionStatuses.join(",") || null,
        change_state: filters.changeStates.join(",") || null,
        cursor,
        direction: filters.directions.join(",") || null,
        event_family: filters.eventFamilies.join(",") || null,
        event_kind: filters.eventKinds.join(",") || null,
        final_decision: filters.finalDecisions.join(",") || null,
        hours: filters.hours ?? undefined,
        limit: NEWS_FEED_PAGE_SIZE,
        outcome: filters.outcome,
        q: filters.q,
        source_authority: filters.sourceAuthorities.join(",") || null,
        subject_code: filters.subjectCodes.join(",") || null,
        symbol: filters.symbol,
      },
      token,
    })
  ).data;

export const useNewsFeedWithToken = (token: string, filters: NewsFeedFilters) =>
  useQuery({
    enabled: Boolean(token),
    queryKey: queryKeys.newsFeed(filters),
    queryFn: () => fetchNewsFeed(token, filters, null),
    refetchInterval: NEWS_FEED_REFETCH_MS,
    staleTime: 2_000,
  });

export const useNewsFeedHistoryWithToken = (
  token: string,
  filters: NewsFeedFilters,
  firstCursor: string | null,
  enabled: boolean,
) =>
  useInfiniteQuery({
    enabled: Boolean(token && firstCursor && enabled),
    queryKey: queryKeys.newsFeedHistory(filters, firstCursor ?? ""),
    queryFn: ({ pageParam }) => fetchNewsFeed(token, filters, pageParam),
    initialPageParam: firstCursor ?? "",
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    staleTime: Number.POSITIVE_INFINITY,
  });

const marketKindParam = (kinds: readonly NewsMarketKind[]): string | null =>
  kinds.length && kinds.length < NEWS_MARKET_KINDS.length
    ? NEWS_MARKET_KINDS.filter((kind) => kinds.includes(kind)).join(",")
    : null;

const fetchNewsMarket = async (
  token: string,
  kinds: readonly NewsMarketKind[],
  cursor: string | null,
) =>
  (
    await getApi<NewsMarket>("/api/news/market", {
      etagKey: `news-market:${marketKindParam(kinds) ?? "all"}:${cursor ?? "first"}`,
      params: {
        cursor,
        // Absent rather than the full list when nothing is narrowed: the server's default window is every
        // kind, and a request that spells all four out would report `filters.kind` back as a narrowing the
        // reader never made.
        kind: marketKindParam(kinds),
        limit: NEWS_MARKET_PAGE_SIZE,
      },
      token,
    })
  ).data;

/**
 * One page of market observation groups (#553 PR-1), plus the per-kind intake summary beside them.
 *
 * The window is the server's default — the last 72 h — because this page reads what arrived, and neither
 * `from_ms` nor `to_ms` is browser state yet. `sources` travels with the page rather than from
 * `/api/news/status`: it is counted off the stored facts, so the strip and the rows under it cannot
 * disagree about what the window holds.
 */
export const useNewsMarketWithToken = (token: string, kinds: readonly NewsMarketKind[]) =>
  useQuery({
    enabled: Boolean(token),
    queryKey: queryKeys.newsMarket(marketKindParam(kinds) ?? ""),
    queryFn: () => fetchNewsMarket(token, kinds, null),
    refetchInterval: NEWS_MARKET_REFETCH_MS,
    staleTime: 2_000,
  });

/**
 * The pages behind the first one, on an explicit action — never automatic scroll.
 *
 * Loaded pages are frozen: only the first page follows the poll, exactly as the Event feed's history does.
 * Feeding the polled first page's `next_cursor` straight in would move the key every time an observation
 * lands, discarding every page the reader had loaded.
 */
export const useNewsMarketHistoryWithToken = (
  token: string,
  kinds: readonly NewsMarketKind[],
  firstCursor: string | null,
  enabled: boolean,
) =>
  useInfiniteQuery({
    enabled: Boolean(token && firstCursor && enabled),
    queryKey: queryKeys.newsMarketHistory(marketKindParam(kinds) ?? "", firstCursor ?? ""),
    queryFn: ({ pageParam }) => fetchNewsMarket(token, kinds, pageParam),
    initialPageParam: firstCursor ?? "",
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    staleTime: Number.POSITIVE_INFINITY,
  });

/**
 * One observation in full, with its group's retained timeline (#553 PR-1).
 *
 * Fetched only when a reader expands a group, and never polled: the stored provider payload of one Item
 * cannot change, and the group's newer observations arrive on the list's own key.
 */
export const useNewsMarketItemWithToken = (token: string, itemId: string | null) =>
  useQuery({
    enabled: Boolean(token && itemId),
    queryKey: queryKeys.newsMarketItem(itemId ?? ""),
    queryFn: async () =>
      (
        await getApi<NewsMarketItem>(`/api/news/market/${encodeURIComponent(itemId ?? "")}`, {
          etagKey: `news-market-item:${itemId ?? ""}`,
          token,
        })
      ).data,
    staleTime: 60_000,
  });

/**
 * The tape's roster, its ingest position and one day of counts (#572 PR-3).
 *
 * One read for the whole header: the four statements behind it are the same window, so the tiles and the
 * roster under them cannot disagree about what the day held.
 */
export const useNewsWalletsWithToken = (token: string) =>
  useQuery({
    enabled: Boolean(token),
    queryKey: queryKeys.newsWallets(),
    queryFn: async () =>
      (
        await getApi<NewsWallets>("/api/news/wallets", {
          etagKey: "news-wallets",
          token,
        })
      ).data,
    refetchInterval: NEWS_WALLETS_REFETCH_MS,
    staleTime: 2_000,
  });

/**
 * The cards the tape opened in one window, each beside its +1h/+4h price receipt.
 *
 * Its own query rather than a field of the one above, so a slow card table cannot hold the header and a
 * window change re-requests only the table. The window is a closed server vocabulary, so it is part of
 * the key.
 */
export const useNewsWalletCardsWithToken = (token: string, window: NewsWalletCardWindow) =>
  useQuery({
    enabled: Boolean(token),
    queryKey: queryKeys.newsWalletCards(window),
    queryFn: async () =>
      (
        await getApi<NewsWalletCards>("/api/news/wallets/cards", {
          etagKey: `news-wallet-cards:${window}`,
          params: { limit: NEWS_WALLET_CARDS_PAGE_SIZE, window },
          token,
        })
      ).data,
    refetchInterval: NEWS_WALLETS_REFETCH_MS,
    staleTime: 2_000,
  });

export const useNewsEventWithToken = (token: string, eventId?: string | null) =>
  useQuery({
    enabled: Boolean(token && eventId),
    queryKey: queryKeys.newsEvent(eventId ?? ""),
    queryFn: async () =>
      (
        await getApi<NewsEventDetail>(`/api/news/events/${encodeURIComponent(eventId ?? "")}`, {
          etagKey: `news-event:${eventId ?? ""}`,
          token,
        })
      ).data,
    refetchInterval: NEWS_EVENT_REFETCH_MS,
    staleTime: 5_000,
  });

export const useNewsStatusWithToken = (token: string) =>
  useQuery({
    enabled: Boolean(token),
    queryKey: queryKeys.newsStatus(),
    queryFn: async () =>
      (
        await getApi<NewsStatus>("/api/news/status", {
          etagKey: "news-status",
          token,
        })
      ).data,
    refetchInterval: NEWS_STATUS_REFETCH_MS,
    staleTime: 5_000,
  });

/**
 * What one `base_symbol` is (#207 PR-W1). Identity, and nothing that moves.
 *
 * No `refetchInterval`: the universe snapshot lands on a schedule measured in hours, and the three things
 * on the token page that *do* move — Events, quote, rank window — each arrive on their own key at their own
 * rhythm. Polling this would re-render the page for bytes that cannot have changed.
 */
export const useNewsSymbolWithToken = (token: string, base: string) => {
  const normalized = base.trim().toUpperCase().replace(/^XYZ-/, "");
  return useQuery({
    enabled: Boolean(token && normalized),
    queryKey: queryKeys.newsSymbol(normalized),
    queryFn: async () =>
      (
        await getApi<NewsSymbol>(`/api/news/symbols/${encodeURIComponent(normalized)}`, {
          etagKey: `news-symbol:${normalized}`,
          token,
        })
      ).data,
    staleTime: 60_000,
  });
};

/**
 * Current quotes for the first 100 distinct symbols in Feed order (#304).
 *
 * Selection happens before sorting so old alphabetical symbols cannot displace the newest Events. Only the
 * selected batch is sorted for a stable query/ETag key shared by surfaces asking for the same symbols.
 */
export const useNewsQuotesWithToken = (token: string, symbols: readonly string[]) => {
  const selected = [...new Set(symbols.map((symbol) => symbol.trim()).filter(Boolean))].slice(
    0,
    NEWS_QUOTES_SYMBOL_MAX,
  );
  const batch = selected.sort();
  return useQuery({
    enabled: Boolean(token && batch.length),
    queryKey: queryKeys.newsQuotes(batch),
    queryFn: async () =>
      (
        await getApi<NewsQuotes>("/api/news/quotes", {
          etagKey: `news-quotes:${batch.join(",")}`,
          params: { symbols: batch.join(",") },
          token,
        })
      ).data,
    refetchInterval: NEWS_QUOTES_REFETCH_MS,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: true,
    staleTime: 2_000,
  });
};
