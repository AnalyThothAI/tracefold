import { getApi } from "@lib/api/client";
import type { TokenCaseDossier, TokenCasePostsData, TokenPostRange, WindowKey } from "@lib/types";
import { queryKeys } from "@shared/query/queryKeys";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";

import type { TargetRef } from "../../../domain/tokenTarget";
import { targetRefKey } from "../../../domain/tokenTarget";
type UseTokenCaseArgs = {
  token: string;
  target: TargetRef | null;
  window: WindowKey;
  postsLimit?: number;
};

type UseTokenCasePostsArgs = UseTokenCaseArgs & {
  range?: TokenPostRange;
  initialPosts?: TokenCasePostsData | null;
};

export function useTriggerTargetPost({
  token,
  target,
  eventId,
  enabled,
}: {
  token: string;
  target: TargetRef | null;
  eventId: string | null;
  enabled: boolean;
}) {
  return useQuery({
    queryKey: queryKeys.triggerTargetPost(target ? targetRefKey(target) : null, eventId),
    queryFn: async () => {
      const response = await getApi<TokenCasePostsData>("/api/target-posts", {
        token,
        params: {
          target_type: target?.target_type,
          target_id: target?.target_id,
          window: "24h",
          range: "all_history",
          event_id: eventId,
          limit: 1,
        },
      });
      return response.data.items.find((post) => post.event_id === eventId) ?? null;
    },
    enabled: Boolean(token && target && eventId && enabled),
    staleTime: Infinity,
    retry: false,
  });
}

export function useTokenCase({ token, target, window, postsLimit = 24 }: UseTokenCaseArgs) {
  return useQuery({
    queryKey: queryKeys.tokenCase(target ? targetRefKey(target) : null, window, postsLimit),
    queryFn: () =>
      getApi<TokenCaseDossier>("/api/token-case", {
        token,
        params: {
          target_type: target?.target_type,
          target_id: target?.target_id,
          window,
          posts_limit: postsLimit,
        },
      }),
    enabled: Boolean(token && target),
    staleTime: 15_000,
  });
}

export function useTokenCasePosts({
  token,
  target,
  window,
  postsLimit = 24,
  range = "current_window",
  initialPosts,
}: UseTokenCasePostsArgs) {
  const seedPosts = canSeedTokenCasePosts({
    initialPosts,
    target,
    window,
    range,
  })
    ? initialPosts
    : null;
  const shouldFetchFirstPage = shouldEnableTokenCasePostsQuery({
    token,
    target,
    initialPosts,
    hasSeedPosts: Boolean(seedPosts),
  });
  const queryKey = queryKeys.targetPosts(
    target ? targetRefKey(target) : null,
    window,
    range,
    postsLimit,
  );

  return useInfiniteQuery({
    queryKey,
    queryFn: async ({ pageParam }) => {
      const response = await getApi<TokenCasePostsData>("/api/target-posts", {
        token,
        params: {
          target_type: target?.target_type,
          target_id: target?.target_id,
          window,
          range,
          limit: postsLimit,
          cursor: pageParam || undefined,
        },
      });
      return response.data;
    },
    initialData: seedPosts
      ? {
          pages: [seedPosts],
          pageParams: [""],
        }
      : undefined,
    initialPageParam: "",
    getNextPageParam: (lastPage) => lastPage.next_cursor || undefined,
    enabled: shouldFetchFirstPage,
    refetchOnMount: false,
    refetchOnReconnect: false,
    refetchOnWindowFocus: false,
    staleTime: 15_000,
  });
}

export function mergeTokenCasePostPages(pages?: TokenCasePostsData[]): TokenCasePostsData | null {
  if (!pages?.length) {
    return null;
  }
  const first = pages[0];
  const last = pages[pages.length - 1];
  return {
    ...first,
    returned_count: pages.reduce((total, page) => total + page.returned_count, 0),
    has_more: last.has_more,
    next_cursor: last.next_cursor,
    items: pages.flatMap((page) => page.items),
  };
}

export function canSeedTokenCasePosts({
  initialPosts,
  target,
  window,
  range,
}: {
  initialPosts?: TokenCasePostsData | null;
  target: TargetRef | null;
  window: WindowKey;
  range: TokenPostRange;
}): boolean {
  if (!initialPosts || !target) {
    return false;
  }
  const query = initialPosts.query;
  return (
    query.target_type === target.target_type &&
    query.target_id === target.target_id &&
    query.window === window &&
    query.range === range
  );
}

export function shouldEnableTokenCasePostsQuery({
  token,
  target,
  initialPosts,
  hasSeedPosts,
}: {
  token: string;
  target: TargetRef | null;
  initialPosts?: TokenCasePostsData | null;
  hasSeedPosts: boolean;
}): boolean {
  if (!token || !target) {
    return false;
  }
  if (hasSeedPosts) {
    return false;
  }
  return initialPosts !== undefined;
}
