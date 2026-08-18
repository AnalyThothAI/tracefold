import { getApi } from "@lib/api/client";
import type { components } from "@lib/types/openapi";
import { queryKeys } from "@shared/query/queryKeys";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";

type NewsSchemas = components["schemas"];

export type NewsFeed = NewsSchemas["NewsFeedData"];
export type NewsFeedEvent = NewsSchemas["NewsFeedEventData"];
export type NewsFeedSort = NewsSchemas["NewsFeedFiltersData"]["sort"];
export type NewsEvent = NewsSchemas["NewsEventData"];
export type NewsEventDetail = NewsSchemas["NewsEventDetailData"];
export type NewsEventMember = NewsSchemas["NewsEventMemberData"];
export type NewsVerdict = NewsSchemas["NewsVerdictData"];
export type NewsDelivery = NewsSchemas["NewsDeliveryData"];
export type NewsDeliverySummary = NewsSchemas["NewsDeliverySummaryData"];
export type NewsLabel = NewsSchemas["NewsLabelData"];
export type NewsMarketMark = NewsSchemas["NewsMarketMarkData"];
export type NewsTriageSummary = NewsSchemas["NewsTriageSummaryData"];
export type NewsStatus = NewsSchemas["NewsStatusData"];
export type NewsIncident = NewsSchemas["NewsIncidentData"];

export type NewsFeedPriority = "high" | "normal";
export type NewsFeedDecision = "push" | "escalate" | "drop" | "throttled" | "degraded";

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

export type NewsFeedFilters = {
  admission: string | null;
  decision: NewsFeedDecision | null;
  family: string | null;
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
        cursor ?? "first",
      ])}`,
      params: {
        admission: filters.admission,
        cursor,
        decision: filters.decision,
        family: filters.family,
        limit: NEWS_FEED_PAGE_SIZE,
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
