import { getApi } from "@lib/api/client";
import type { components } from "@lib/types/openapi";
import { queryKeys } from "@shared/query/queryKeys";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";

type NewsSchemas = components["schemas"];

export type NewsLevel = NewsSchemas["NewsStoryData"]["level"];
export type NewsProviderCoin = NewsSchemas["NewsProviderCoinData"];
export type NewsProviderMetadata = NewsSchemas["NewsProviderMetadataData"];
export type NewsProviderEvidence = NewsSchemas["NewsProviderEvidenceData"];
export type NewsPushDeliveryState = NonNullable<
  NewsSchemas["NewsStoryData"]["push_delivery_state"]
>;
export type NewsStory = NewsSchemas["NewsStoryData"];
export type NewsFeed = NewsSchemas["NewsFeedData"];
export type NewsStoryMember = NewsSchemas["NewsStoryMemberData"];
export type NewsStoryDetail = NewsSchemas["NewsStoryDetailData"];
export type BriefPublication = NewsSchemas["NewsBriefPublicationData"];
export type BriefTopStory = NewsSchemas["NewsBriefTopStoryData"];
export type NewsBrief = NewsSchemas["NewsBriefData"];
export type NewsOperatingState = NewsSchemas["NewsStatusData"]["operating_state"];
export type NewsStatus = NewsSchemas["NewsStatusData"];

export type NewsFeedFilters = {
  category: string | null;
  level: NewsLevel | null;
  providerScoreGt: number | null;
  q: string;
  reportingOrigin: string | null;
  sort: "importance" | "latest";
};

export const useNewsFeedWithToken = (token: string, filters: NewsFeedFilters) =>
  useInfiniteQuery({
    enabled: Boolean(token),
    queryKey: queryKeys.newsFeed(
      filters.q,
      filters.category,
      filters.level,
      filters.reportingOrigin,
      filters.providerScoreGt,
      filters.sort,
    ),
    queryFn: async ({ pageParam }) =>
      (
        await getApi<NewsFeed>("/api/news/feed", {
          etagKey: `news-feed:${JSON.stringify([
            filters.q,
            filters.category,
            filters.level,
            filters.reportingOrigin,
            filters.providerScoreGt,
            filters.sort,
            pageParam ?? "first",
          ])}`,
          params: {
            category: filters.category,
            cursor: pageParam,
            level: filters.level,
            limit: 25,
            provider_score_gt: filters.providerScoreGt,
            q: filters.q,
            reporting_origin: filters.reportingOrigin,
            sort: filters.sort,
          },
          token,
        })
      ).data,
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    refetchInterval: 60_000,
    staleTime: 30_000,
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
          token,
        })
      ).data,
    refetchInterval: 60_000,
    staleTime: 30_000,
  });
