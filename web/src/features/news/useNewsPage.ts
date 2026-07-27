import { getApi } from "@lib/api/client";
import type { components } from "@lib/types";
import { queryKeys } from "@shared/query/queryKeys";
import { useQuery } from "@tanstack/react-query";

export const NEWS_PAGE_SIZE = 50;

export type NewsStoryQueryParams = {
  cursor?: string | null;
  enabled?: boolean;
  evidencePosture?: string | null;
  limit?: number;
  q?: string | null;
  source?: string | null;
  view?: "latest" | "priority";
};

type StoryList = components["schemas"]["NewsStoryListData"];
type StoryDetail = components["schemas"]["NewsStoryDetailData"];
type GlobalBrief = components["schemas"]["NewsGlobalBriefData"];
type BriefHistory = components["schemas"]["NewsGlobalBriefHistoryData"];

export const useNewsStoriesWithToken = (
  token: string,
  {
    cursor = null,
    enabled = true,
    evidencePosture = null,
    limit = NEWS_PAGE_SIZE,
    q = null,
    source = null,
    view = "latest",
  }: NewsStoryQueryParams = {},
) =>
  useQuery({
    enabled: Boolean(token) && enabled,
    queryKey: queryKeys.newsStories({ cursor, evidencePosture, limit, q, source, view }),
    queryFn: async () =>
      (
        await getApi<StoryList>("/api/news/stories", {
          params: {
            cursor,
            evidence_posture: evidencePosture,
            limit,
            q,
            source,
            view,
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
        await getApi<StoryDetail>(`/api/news/stories/${encodeURIComponent(storyId ?? "")}`, {
          token,
        })
      ).data,
    refetchInterval: 30_000,
    staleTime: 15_000,
  });

export const useNewsBriefWithToken = (token: string) =>
  useQuery({
    enabled: Boolean(token),
    queryKey: queryKeys.newsBrief(),
    queryFn: async () => (await getApi<GlobalBrief>("/api/news/brief", { token })).data,
    refetchInterval: 30_000,
    staleTime: 15_000,
  });

export const useNewsBriefHistoryWithToken = (token: string) =>
  useQuery({
    enabled: Boolean(token),
    queryKey: queryKeys.newsBriefHistory(),
    queryFn: async () =>
      (await getApi<BriefHistory>("/api/news/brief/history", { params: { limit: 20 }, token }))
        .data,
    staleTime: 30_000,
  });
