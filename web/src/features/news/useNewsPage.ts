import { getApi } from "@lib/api/client";
import { queryKeys } from "@shared/query/queryKeys";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";

export type NewsLevel = "critical" | "high" | "medium" | "low" | "info";

export type NewsStory = {
  category: string;
  description: string;
  first_published_at_ms: number;
  importance_score: number;
  importance_factors: {
    corroboration_points: number;
    diplomacy_flashpoint_boost: number;
    entity_corroboration_boost: number;
    recency_points: number;
    physical_source_count: number;
    scoring_corroboration_count: number;
    severity_level: NewsLevel;
    severity_points: number;
    source_points: number;
    source_tier: number;
    total: number;
  };
  item_count: number;
  last_published_at_ms: number;
  level: NewsLevel;
  source_count: number;
  source_id: string;
  source_name: string;
  representative_item_id: string;
  scoring_item_id: string;
  story_id: string;
  title: string;
  url: string;
};

export type NewsFeed = {
  facets: {
    categories: Array<{ count: number; value: string }>;
    levels: Array<{ count: number; value: string }>;
    sources: Array<{ count: number; label: string; value: string }>;
  };
  filters: {
    category: string | null;
    level: string | null;
    source_id: string | null;
  };
  has_more: boolean;
  next_cursor: string | null;
  sort: "importance" | "latest";
  stories: NewsStory[];
};

export type NewsStoryMember = {
  category: string;
  current: boolean;
  description: string;
  first_joined_at_ms: number;
  importance_score: number;
  item_id: string;
  lang: string;
  last_confirmed_at_ms: number;
  last_observed_at_ms: number;
  level: NewsLevel;
  published_at_ms: number;
  reporting_origin: string;
  source_id: string;
  source_name: string;
  tier: number;
  title: string;
  url: string;
};

export type NewsStoryDetail = NewsStory & {
  active: boolean;
  canonical_title: string;
  members: NewsStoryMember[];
};

export type BriefPublication = {
  evidence_cutoff_at_ms: number;
  fingerprint: string;
  lead: string;
  lines: string[];
  locale: string;
  model: string;
  provider: string;
  publication_id: string;
  published_at_ms: number;
  selected_story_ids: string[];
  sources: Array<{
    n: number;
    source: string;
    story_id: string;
    title: string;
    url: string;
  }>;
  validation: {
    citation_index_lock: boolean;
    citation_closure: boolean;
    final_story_coverage: number;
    grounding_failures: number[];
    lead_fallback: boolean;
    line_fallbacks: number[];
    model_line_coverage: number;
    no_cross_story_stitching: boolean;
    proper_noun_grounding: boolean;
    story_count: number;
  };
};

export type WorldBrief = {
  candidate_source_count: number;
  candidate_story_count: number;
  history: BriefPublication[];
  latest_run: {
    attempt_count: number;
    candidate_source_count: number;
    candidate_story_count: number;
    completed_at_ms: number | null;
    created_at_ms: number;
    fingerprint: string;
    heartbeat_at_ms: number | null;
    last_error: string | null;
    lease_expires_at_ms: number | null;
    run_id: string;
    status: "running" | "ready" | "insufficient_material" | "failed";
    updated_at_ms: number;
  } | null;
  publication: BriefPublication | null;
  state:
    | "unavailable"
    | "insufficient_material"
    | "running"
    | "ready"
    | "stale_fallback"
    | "failed";
  target_fingerprint: string;
};

export type NewsSource = {
  consecutive_failures: number;
  enabled: boolean;
  feed_url: string;
  lang: string;
  last_error: string | null;
  last_http_status: number | null;
  last_success_at_ms: number | null;
  latest_entries_seen: number | null;
  latest_fetch_duration_ms: number | null;
  latest_fetch_error_code: string | null;
  latest_fetch_path: "direct" | "relay" | null;
  latest_direct_error_code: string | null;
  latest_fetch_finished_at_ms: number | null;
  latest_fetch_status: "success" | "not_modified" | "failed" | null;
  latest_items_inserted: number | null;
  latest_items_updated: number | null;
  latest_observations_inserted: number | null;
  latest_rejection_counts: Record<string, number> | null;
  name: string;
  memberships: string[];
  next_fetch_at_ms: number;
  refresh_interval_seconds: number;
  source_id: string;
  tier: number;
};

export type NewsSources = {
  items: NewsSource[];
};

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

export const useNewsSourcesWithToken = (token: string) =>
  useQuery({
    enabled: Boolean(token),
    queryKey: queryKeys.newsSources(),
    queryFn: async () =>
      (
        await getApi<NewsSources>("/api/news/sources", {
          token,
        })
      ).data,
    refetchInterval: 60_000,
    staleTime: 30_000,
  });
