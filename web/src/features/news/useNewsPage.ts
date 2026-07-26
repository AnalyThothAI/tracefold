import { getApi } from "@lib/api/client";
import type { components } from "@lib/types";
import { queryKeys } from "@shared/query/queryKeys";
import { useQuery } from "@tanstack/react-query";

export const NEWS_PAGE_SIZE = 50;

export type NewsStoryQueryParams = {
  cursor?: string | null;
  enabled?: boolean;
  limit?: number;
  q?: string | null;
  source?: string | null;
  verificationStatus?: string | null;
};

type NewsStoryListData = components["schemas"]["NewsStoryListData"];
type NewsStoryDetailData = components["schemas"]["NewsStoryDetailData"];

export const useNewsStoriesWithToken = (
  token: string,
  {
    cursor = null,
    enabled = true,
    limit = NEWS_PAGE_SIZE,
    q = null,
    source = null,
    verificationStatus = null,
  }: NewsStoryQueryParams = {},
) =>
  useQuery({
    enabled: Boolean(token) && enabled,
    queryKey: queryKeys.newsStories({
      cursor,
      limit,
      q,
      source,
      verificationStatus,
    }),
    queryFn: async () =>
      (
        await getApi<NewsStoryListData>("/api/news/stories", {
          params: {
            cursor,
            limit,
            q,
            source,
            verification_status: verificationStatus,
          },
          token,
        })
      ).data,
    refetchInterval: 15_000,
    staleTime: 0,
  });

export const useNewsStoryWithToken = (token: string, storyId?: string | null) =>
  useQuery({
    enabled: Boolean(token && storyId),
    queryKey: queryKeys.newsStory(storyId ?? ""),
    queryFn: async () =>
      (
        await getApi<NewsStoryDetailData>(
          `/api/news/stories/${encodeURIComponent(storyId ?? "")}`,
          { token },
        )
      ).data,
    refetchInterval: 30_000,
    staleTime: 15_000,
  });
