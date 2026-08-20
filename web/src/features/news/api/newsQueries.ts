import { getApi } from "@lib/api/client";
import type { components } from "@lib/types/openapi";
import { queryKeys } from "@shared/query/queryKeys";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";

type NewsSchemas = components["schemas"];

export type NewsFeed = NewsSchemas["NewsFeedData"];
export type NewsFeedCounts = NewsSchemas["NewsFeedCountsData"];
export type NewsFeedEvent = NewsSchemas["NewsFeedEventData"];
export type NewsFeedSort = NewsSchemas["NewsFeedFiltersData"]["sort"];
export type NewsEvent = NewsSchemas["NewsEventData"];
export type NewsAssetRef = NewsSchemas["NewsAssetRefData"];
export type NewsSymbolNormalization = NewsSchemas["NewsSymbolNormalizationData"];
export type NewsEventDetail = NewsSchemas["NewsEventDetailData"];
export type NewsEventMember = NewsSchemas["NewsEventMemberData"];
export type NewsVerdict = NewsSchemas["NewsVerdictData"];
export type NewsDelivery = NewsSchemas["NewsDeliveryData"];
export type NewsDeliverySummary = NewsSchemas["NewsDeliverySummaryData"];
export type NewsLabel = NewsSchemas["NewsLabelData"];
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
export type NewsReview = NewsSchemas["NewsReviewData"];
export type NewsReviewCoverage = NewsSchemas["NewsReviewCoverageData"];
export type NewsReviewDirection = NewsSchemas["NewsReviewDirectionData"];
export type NewsReviewMagnitude = NewsSchemas["NewsReviewMagnitudeData"];
export type NewsReviewEventType = NewsSchemas["NewsReviewEventTypeData"];
export type NewsReviewMiss = NewsSchemas["NewsReviewMissData"];
export type NewsPriceStatus = NewsSchemas["NewsPriceStatusData"];

export type NewsFeedPriority = "high" | "normal";
export type NewsFeedDecision = "push" | "escalate" | "drop" | "throttled" | "degraded";
export type NewsFeedOutcome = NewsOutcomeGroup;
export const NEWS_FEED_OUTCOMES: readonly NewsFeedOutcome[] = ["pushed", "held", "pending"];
/** Time windows the feed offers; `null` means the whole retention window. */
export const NEWS_FEED_HOURS: readonly number[] = [1, 6, 24, 72];
export const NEWS_FEED_DEFAULT_HOURS = 24;

export const NEWS_FEED_PRIORITIES: readonly NewsFeedPriority[] = ["high", "normal"];
export const NEWS_FEED_DECISIONS: readonly NewsFeedDecision[] = [
  "push",
  "escalate",
  "drop",
  "throttled",
  "degraded",
];
export const NEWS_FEED_PAGE_SIZE = 25;
export const NEWS_FEED_REFETCH_MS = 3_000;
export const NEWS_STATUS_REFETCH_MS = 15_000;
export const NEWS_EVENT_REFETCH_MS = 15_000;
/**
 * Quotes poll on the feed's own rhythm but from a separate query key (#88): a price that changed must not
 * make the feed and its counts query return a new body every three seconds.
 */
export const NEWS_QUOTES_REFETCH_MS = 3_000;
/** The review window is a stable aggregate; a minute is plenty and keeps a 720 h query off the hot path. */
export const NEWS_REVIEW_REFETCH_MS = 60_000;
/** `/api/news/quotes` accepts at most this many symbols; the hook batches the visible rows into one call. */
export const NEWS_QUOTES_SYMBOL_MAX = 100;
export const NEWS_REVIEW_HOURS: readonly number[] = [24, 72, 168, 720];
export const NEWS_REVIEW_DEFAULT_HOURS = 168;

export type NewsFeedFilters = {
  admission: string | null;
  decision: NewsFeedDecision | null;
  family: string | null;
  hours: number | null;
  outcome: NewsFeedOutcome | null;
  priority: NewsFeedPriority | null;
  q: string;
  sort: NewsFeedSort;
  symbol: string | null;
};

const fetchNewsFeed = async (token: string, filters: NewsFeedFilters, cursor: string | null) =>
  (
    await getApi<NewsFeed>("/api/news/feed", {
      etagKey: `news-feed:${JSON.stringify([
        filters.q,
        filters.family,
        filters.admission,
        filters.priority,
        filters.decision,
        filters.symbol,
        filters.sort,
        filters.outcome,
        filters.hours,
        cursor ?? "first",
      ])}`,
      params: {
        admission: filters.admission,
        cursor,
        decision: filters.decision,
        family: filters.family,
        hours: filters.hours ?? undefined,
        limit: NEWS_FEED_PAGE_SIZE,
        outcome: filters.outcome,
        priority: filters.priority,
        q: filters.q,
        sort: filters.sort,
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
 * Current quotes for one deduplicated symbol batch (#88).
 *
 * The batch is sorted and deduplicated before it becomes a query key, so two surfaces asking for the same
 * symbols share one cache entry and one request. The server deduplicates again — a client cannot multiply
 * repository or provider work by repeating a symbol.
 */
export const useNewsQuotesWithToken = (token: string, symbols: readonly string[]) => {
  const batch = [...new Set(symbols.map((symbol) => symbol.trim()).filter(Boolean))]
    .sort()
    .slice(0, NEWS_QUOTES_SYMBOL_MAX);
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
    staleTime: 2_000,
  });
};

export const useNewsReviewWithToken = (token: string, hours: number) =>
  useQuery({
    enabled: Boolean(token),
    queryKey: queryKeys.newsReview(hours),
    queryFn: async () =>
      (
        await getApi<NewsReview>("/api/news/review", {
          etagKey: `news-review:${hours}`,
          params: { hours },
          token,
        })
      ).data,
    refetchInterval: NEWS_REVIEW_REFETCH_MS,
    staleTime: 30_000,
  });
