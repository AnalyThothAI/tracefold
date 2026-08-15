import { getApi } from "@lib/api/client";
import type { components } from "@lib/types/openapi";
import { queryKeys } from "@shared/query/queryKeys";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";

type NewsSchemas = components["schemas"];

export type NewsLevel = NewsSchemas["NewsStoryData"]["level"];
export type NewsStory = NewsSchemas["NewsStoryData"];
export type NewsFeed = NewsSchemas["NewsFeedData"];
export type NewsStoryMember = NewsSchemas["NewsStoryMemberData"];
export type NewsStoryDetail = NewsSchemas["NewsStoryDetailData"];
export type BriefPublication = NewsSchemas["NewsBriefPublicationData"];
export type BriefTopStory = NewsSchemas["NewsBriefTopStoryData"];
export type NewsBrief = NewsSchemas["NewsBriefData"];
export type NewsStatus = NewsSchemas["NewsStatusData"];
export type NewsRealtimeStatus = NewsSchemas["NewsRealtimeStatusResponseData"];
export type NewsSource = NewsSchemas["NewsSourceData"];
export type NewsSources = NewsSchemas["NewsSourcesData"];

export type NewsFeedFilters = {
  category: string | null;
  level: NewsLevel | null;
  providerScoreGt: number | null;
  q: string;
  reportingOrigin: string | null;
  sort: "importance" | "latest";
};

const fetchNewsFeed = async (token: string, filters: NewsFeedFilters, cursor: string | null) =>
  (
    await getApi<NewsFeed>("/api/news/feed", {
      etagKey: `news-feed:${JSON.stringify([
        filters.q,
        filters.category,
        filters.level,
        filters.reportingOrigin,
        filters.providerScoreGt,
        filters.sort,
        cursor ?? "first",
      ])}`,
      params: {
        category: filters.category,
        cursor,
        level: filters.level,
        limit: 25,
        provider_score_gt: filters.providerScoreGt,
        q: filters.q,
        reporting_origin: filters.reportingOrigin,
        sort: filters.sort,
      },
      token,
    })
  ).data;

export const useNewsFeedWithToken = (token: string, filters: NewsFeedFilters) =>
  useQuery({
    enabled: Boolean(token),
    queryKey: queryKeys.newsFeed(
      filters.q,
      filters.category,
      filters.level,
      filters.reportingOrigin,
      filters.providerScoreGt,
      filters.sort,
    ),
    queryFn: () => fetchNewsFeed(token, filters, null),
    refetchInterval: 3_000,
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
    queryKey: queryKeys.newsFeedHistory(
      filters.q,
      filters.category,
      filters.level,
      filters.reportingOrigin,
      filters.providerScoreGt,
      filters.sort,
      firstCursor ?? "",
    ),
    queryFn: ({ pageParam }) => fetchNewsFeed(token, filters, pageParam),
    initialPageParam: firstCursor ?? "",
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    staleTime: Number.POSITIVE_INFINITY,
  });

export const useNewsStoryWithToken = (token: string, storyId?: string | null) =>
  useInfiniteQuery({
    enabled: Boolean(token && storyId),
    queryKey: queryKeys.newsStory(storyId ?? ""),
    queryFn: async ({ pageParam }) =>
      (
        await getApi<NewsStoryDetail>(`/api/news/stories/${encodeURIComponent(storyId ?? "")}`, {
          params: { members_cursor: pageParam, members_limit: 25 },
          token,
        })
      ).data,
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.members_page.next_cursor ?? undefined,
    refetchInterval: 60_000,
    staleTime: 30_000,
  });

export const useNewsBriefWithToken = (token: string) =>
  useQuery({
    enabled: Boolean(token),
    queryKey: queryKeys.newsBrief(),
    queryFn: async () =>
      (
        await getApi<NewsBrief>("/api/news/brief", {
          etagKey: "news-brief",
          token,
        })
      ).data,
    refetchInterval: 60_000,
    staleTime: 30_000,
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
    refetchInterval: 60_000,
    staleTime: 30_000,
  });

export const useNewsRealtimeStatusWithToken = (token: string) =>
  useQuery({
    enabled: Boolean(token),
    queryKey: queryKeys.newsRealtimeStatus(),
    queryFn: async () =>
      (
        await getApi<NewsRealtimeStatus>("/api/news/status", {
          etagKey: "news-realtime-status",
          params: { view: "realtime" },
          token,
        })
      ).data,
    refetchInterval: 3_000,
    staleTime: 2_000,
  });

export const useNewsSourcesWithToken = (token: string) =>
  useInfiniteQuery({
    enabled: Boolean(token),
    queryKey: queryKeys.newsSources(),
    queryFn: async ({ pageParam }) =>
      (
        await getApi<NewsSources>("/api/news/sources", {
          params: { cursor: pageParam, limit: 100 },
          token,
        })
      ).data,
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.page.next_cursor ?? undefined,
    refetchInterval: 60_000,
    staleTime: 30_000,
  });
