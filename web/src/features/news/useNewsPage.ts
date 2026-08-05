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
export type WorldBrief = NewsSchemas["NewsBriefData"];
export type NewsOperatingState = NewsSchemas["NewsStatusData"]["operating_state"];
export type NewsStatus = NewsSchemas["NewsStatusData"];

export const useNewsFeedWithToken = (
  token: string,
  category?: string | null,
  sort: "importance" | "latest" = "importance",
) =>
  useInfiniteQuery({
    enabled: Boolean(token),
    queryKey: queryKeys.newsFeed(category ?? null, sort),
    queryFn: async ({ pageParam }) =>
      (
        await getApi<NewsFeed>("/api/news/feed", {
          etagKey: `news-feed:${category ?? "all"}:${sort}:${pageParam ?? "first"}`,
          params: { category, cursor: pageParam, limit: 50, sort },
          token,
        })
      ).data,
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    refetchInterval: 60_000,
    staleTime: 30_000,
  });

export const useNewsStoryWithToken = (token: string, storyId?: string | null) =>
  useQuery({
    enabled: Boolean(token && storyId),
    queryKey: queryKeys.newsStory(storyId ?? ""),
    queryFn: async () =>
      (
        await getApi<NewsStoryDetail>(`/api/news/stories/${encodeURIComponent(storyId ?? "")}`, {
          token,
        })
      ).data,
    refetchInterval: 60_000,
    staleTime: 30_000,
  });

export const useNewsBriefWithToken = (token: string) =>
  useQuery({
    enabled: Boolean(token),
    queryKey: queryKeys.newsBrief(),
    queryFn: async () =>
      (
        await getApi<WorldBrief>("/api/news/brief", {
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
