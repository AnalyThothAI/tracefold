import type {
  BriefPublication,
  NewsFeed,
  NewsSources,
  NewsStory,
  NewsStoryDetail,
  WorldBrief,
} from "@features/news/useNewsPage";

export const NEWS_NOW_MS = 1_779_000_000_000;

export function newsStoryFixture(overrides: Partial<NewsStory> = {}): NewsStory {
  return {
    category: "economic",
    description: "Central banks respond as the policy outlook changes.",
    first_published_at_ms: NEWS_NOW_MS - 120_000,
    importance_factors: {
      corroboration_points: 12,
      diplomacy_flashpoint_boost: 0,
      entity_corroboration_boost: 0,
      recency_points: 9.8,
      physical_source_count: 4,
      scoring_corroboration_count: 4,
      severity_level: "high",
      severity_points: 41.25,
      source_points: 20,
      source_tier: 1,
      total: 83,
    },
    importance_score: 83,
    item_count: 4,
    last_published_at_ms: NEWS_NOW_MS - 30_000,
    level: "high",
    source_count: 4,
    source_id: "wm-politics-reuters",
    source_name: "Reuters World",
    representative_item_id: "news-item-reuters",
    scoring_item_id: "news-item-reuters",
    story_id: "story-global-policy",
    title: "Central banks respond to a new global policy shock",
    url: "https://www.reuters.com/world/story",
    ...overrides,
  };
}

export function newsFeedFixture(): NewsFeed {
  return {
    facets: {
      categories: [{ count: 1, value: "economic" }],
      levels: [{ count: 1, value: "high" }],
      sources: [{ count: 1, label: "Reuters World", value: "wm-politics-reuters" }],
    },
    filters: { category: null, level: null, source_id: null },
    has_more: false,
    next_cursor: null,
    sort: "importance",
    stories: [newsStoryFixture()],
  };
}

export function newsStoryDetailFixture(overrides: Partial<NewsStoryDetail> = {}): NewsStoryDetail {
  return {
    ...newsStoryFixture(),
    active: true,
    canonical_title: "Central banks respond to a new global policy shock",
    members: [
      {
        category: "economic",
        current: true,
        description: "Central banks respond as the policy outlook changes.",
        first_joined_at_ms: NEWS_NOW_MS - 60_000,
        importance_score: 83,
        item_id: "news-item-reuters",
        lang: "en",
        last_confirmed_at_ms: NEWS_NOW_MS,
        last_observed_at_ms: NEWS_NOW_MS,
        level: "high",
        published_at_ms: NEWS_NOW_MS - 60_000,
        reporting_origin: "reuters",
        source_id: "wm-politics-reuters",
        source_name: "Reuters World",
        tier: 1,
        title: "Central banks respond to a new global policy shock",
        url: "https://www.reuters.com/world/story",
      },
    ],
    ...overrides,
  };
}

export function newsBriefPublicationFixture(
  overrides: Partial<BriefPublication> = {},
): BriefPublication {
  return {
    evidence_cutoff_at_ms: NEWS_NOW_MS - 30_000,
    fingerprint: "brief-fingerprint",
    lead: "全球政策冲击正在改变央行预期 [1]",
    lines: ["主要央行回应新的全球政策冲击 [1]"],
    locale: "zh-CN",
    model: "deepseek-v4-flash",
    provider: "openrouter",
    publication_id: "brief-publication",
    published_at_ms: NEWS_NOW_MS,
    selected_story_ids: ["story-global-policy"],
    sources: [
      {
        n: 1,
        source: "Reuters World",
        story_id: "story-global-policy",
        title: "Central banks respond to a new global policy shock",
        url: "https://www.reuters.com/world/story",
      },
    ],
    validation: {
      citation_index_lock: true,
      citation_closure: true,
      final_story_coverage: 1,
      grounding_failures: [],
      lead_fallback: false,
      line_fallbacks: [],
      model_line_coverage: 1,
      no_cross_story_stitching: true,
      proper_noun_grounding: true,
      story_count: 1,
    },
    ...overrides,
  };
}

export function newsGlobalBriefFixture(overrides: Partial<WorldBrief> = {}): WorldBrief {
  const publication = newsBriefPublicationFixture();
  return {
    candidate_source_count: 4,
    candidate_story_count: 1,
    history: [publication],
    latest_run: {
      attempt_count: 1,
      candidate_source_count: 4,
      candidate_story_count: 1,
      completed_at_ms: NEWS_NOW_MS,
      created_at_ms: NEWS_NOW_MS,
      fingerprint: "brief-fingerprint",
      heartbeat_at_ms: NEWS_NOW_MS,
      last_error: null,
      lease_expires_at_ms: null,
      run_id: "brief-run",
      status: "ready",
      updated_at_ms: NEWS_NOW_MS,
    },
    publication,
    state: "ready",
    target_fingerprint: "brief-fingerprint",
    ...overrides,
  };
}

export function newsSourcesFixture(): NewsSources {
  return {
    items: [
      {
        consecutive_failures: 0,
        enabled: true,
        feed_url: "https://example.test/reuters.xml",
        lang: "en",
        last_error: null,
        last_http_status: 200,
        last_success_at_ms: NEWS_NOW_MS,
        latest_entries_seen: 5,
        latest_fetch_duration_ms: 240,
        latest_fetch_error_code: null,
        latest_fetch_path: "direct",
        latest_direct_error_code: null,
        latest_fetch_finished_at_ms: NEWS_NOW_MS,
        latest_fetch_status: "success",
        latest_items_inserted: 2,
        latest_items_updated: 0,
        latest_observations_inserted: 5,
        latest_rejection_counts: { duplicate: 3 },
        name: "Reuters World",
        memberships: ["politics", "intel"],
        next_fetch_at_ms: NEWS_NOW_MS + 120_000,
        refresh_interval_seconds: 120,
        source_id: "wm-politics-reuters",
        tier: 1,
      },
      {
        consecutive_failures: 0,
        enabled: true,
        feed_url:
          "http://rsshub:1200/twitter/user/wallstengine/includeReplies=0&includeRts=0&showRetweetTextInTitle=1&showQuotedInTitle=0",
        lang: "en",
        last_error: null,
        last_http_status: 200,
        last_success_at_ms: NEWS_NOW_MS,
        latest_entries_seen: 3,
        latest_fetch_duration_ms: 180,
        latest_fetch_error_code: null,
        latest_fetch_path: "direct",
        latest_direct_error_code: null,
        latest_fetch_finished_at_ms: NEWS_NOW_MS,
        latest_fetch_status: "success",
        latest_items_inserted: 1,
        latest_items_updated: 0,
        latest_observations_inserted: 3,
        latest_rejection_counts: { duplicate: 2 },
        name: "WallStEngine",
        memberships: ["finance"],
        next_fetch_at_ms: NEWS_NOW_MS + 120_000,
        refresh_interval_seconds: 120,
        source_id: "news-wallstengine-eeeec11d",
        tier: 4,
      },
    ],
  };
}
