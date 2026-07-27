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
      reporting_origin_count: 4,
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
    story_id: "story-global-policy",
    title: "Central banks respond to a new global policy shock",
    url: "https://www.reuters.com/world/story",
    ...overrides,
  };
}

export function newsFeedFixture(): NewsFeed {
  return {
    categories: [{ category: "economic", stories: [newsStoryFixture()] }],
    per_category_cap_count: 0,
    sort: "importance",
    story_count: 1,
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
        importance_score: 83,
        item_id: "news-item-reuters",
        lang: "en",
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
    status: "published",
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
    history: [publication],
    last_error: null,
    last_failure_at_ms: null,
    last_known_good_published_at_ms: publication.published_at_ms,
    pending_fingerprint: null,
    publication,
    state: "fresh",
    update_started_at_ms: null,
    ...overrides,
  };
}

export function newsSourcesFixture(): NewsSources {
  return {
    items: [
      {
        category_hint: "politics",
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
        latest_fetch_finished_at_ms: NEWS_NOW_MS,
        latest_fetch_status: "success",
        latest_items_inserted: 2,
        latest_items_updated: 0,
        latest_observations_inserted: 5,
        latest_rejection_counts: { duplicate: 3 },
        name: "Reuters World",
        next_fetch_at_ms: NEWS_NOW_MS + 120_000,
        refresh_interval_seconds: 120,
        reporting_origin: "reuters",
        source_id: "wm-politics-reuters",
        tier: 1,
      },
    ],
  };
}
