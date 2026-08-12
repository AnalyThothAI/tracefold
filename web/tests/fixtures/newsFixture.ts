import type {
  BriefPublication,
  BriefTopStory,
  NewsBrief,
  NewsFeed,
  NewsSource,
  NewsSources,
  NewsStatus,
  NewsStory,
  NewsStoryDetail,
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
    notification: {
      delivery_state: "sent",
      eligible: false,
      ineligible_reason: "stale",
    },
    provider_evidence: {
      item_id: "news-item-reuters",
      provider_metadata: {
        score: 88,
        source: "jin10",
        signal: "long",
        grade: "A",
        assets: [{ symbol: "BTC", market_type: "spot", match: "Bitcoin" }],
      },
      url: "https://www.reuters.com/world/story",
    },
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
      page: {
        categories_has_more: false,
        levels_has_more: false,
        reporting_origins_has_more: false,
        sources_has_more: false,
      },
      reporting_origins: [{ count: 1, label: "Reuters", value: "reuters" }],
      sources: [{ count: 1, label: "Reuters World", value: "wm-politics-reuters" }],
    },
    filters: {
      category: null,
      level: null,
      provider_score_gt: null,
      q: null,
      reporting_origin: null,
      source_id: null,
    },
    has_more: false,
    next_cursor: null,
    sort: "importance",
    stories: [newsStoryFixture()],
  };
}

export function newsStoryDetailFixture(overrides: Partial<NewsStoryDetail> = {}): NewsStoryDetail {
  return {
    ...newsStoryFixture(),
    canonical_title: "Central banks respond to a new global policy shock",
    members: [
      {
        category: "economic",
        description: "Central banks respond as the policy outlook changes.",
        importance_factors: newsStoryFixture().importance_factors,
        importance_score: 83,
        item_id: "news-item-reuters",
        provider_record_id: "wire-reuters-1",
        provider_metadata: {
          score: 88,
          source: "jin10",
          signal: "long",
          grade: "A",
          assets: [{ symbol: "BTC", market_type: "spot", match: "Bitcoin" }],
        },
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
    members_page: { has_more: false, next_cursor: null, returned_count: 1 },
    ...overrides,
  };
}

export function newsBriefPublicationFixture(
  overrides: Partial<BriefPublication> = {},
): BriefPublication {
  const topStories = [
    newsBriefTopStoryFixture(),
    newsBriefTopStoryFixture({
      category: "natural_disaster",
      effective_importance_score: 404,
      importance_score: 418,
      is_alert: false,
      last_updated_ms: NEWS_NOW_MS - 1_800_000,
      member_titles: [
        "Typhoon makes landfall near a major port",
        "Coastal communities prepare for severe winds",
      ],
      primary_link: null,
      primary_published_at_ms: NEWS_NOW_MS - 2_400_000,
      primary_source: "NHK",
      primary_title: "Typhoon makes landfall near a major port",
      source_count: 2,
      sources: ["NHK"],
      story_id: "story-typhoon",
      threat_level: "elevated",
      unique_source_count: 1,
      upstream_importance_score: 179,
    }),
  ];
  return {
    brief_kind: "l1",
    brief_story_lines: [
      { n: 1, text: "Ceasefire talks resume as delegations return [1]" },
      { n: 2, text: "A typhoon makes landfall near a major port [2]" },
    ],
    composer_version: "news-public-insights-composer-v1",
    identity_version: "news-story-identity-v2",
    locale: "en",
    model: "llama3.1:8b",
    prompt_version: "news-public-insights-prompt-v1",
    provider: "ollama",
    provenance: {
      projection_revision: "projection-revision",
      selection_stats: {
        admissibility_dropped: 2,
        brief_eligible_considered: 1,
        brief_eligible_promoted: false,
        considered: 4,
        overflow_dropped: 0,
        source_cap_dropped: 0,
      },
      selector_evaluated_at_ms: NEWS_NOW_MS - 3_600_000,
    },
    publication_id: "a".repeat(64),
    published_at_ms: NEWS_NOW_MS,
    quality: "ok",
    schema_version: "news-public-insights-v1",
    selection_fingerprint: "b".repeat(64),
    selector_version: "news-public-selector-v1",
    slot_at_ms: NEWS_NOW_MS - (NEWS_NOW_MS % 1_800_000),
    source_age_range: {
      newest_ms: NEWS_NOW_MS - 900_000,
      oldest_ms: NEWS_NOW_MS - 2_400_000,
    },
    sources: [
      {
        published_at_ms: NEWS_NOW_MS - 1_200_000,
        source: "Reuters",
        title: "Ceasefire talks resume as delegations return",
        url: "https://www.reuters.com/world/ceasefire",
      },
      {
        published_at_ms: NEWS_NOW_MS - 2_400_000,
        source: "NHK",
        title: "Typhoon makes landfall near a major port",
        url: "",
      },
    ],
    top_stories: topStories,
    validation: {
      failure_code: null,
      stripped_citations: 0,
      line_fallbacks: [],
    },
    workflow_version: "news-public-insights-workflow-v1",
    world_brief: "Ceasefire talks and severe weather lead the public news agenda [1][2].",
    ...overrides,
  };
}

export function newsBriefTopStoryFixture(overrides: Partial<BriefTopStory> = {}): BriefTopStory {
  return {
    category: "geopolitical",
    corroboration_source_count: 2,
    effective_importance_score: 132,
    entity_corroboration: true,
    importance_score: 146,
    is_alert: true,
    last_updated_ms: NEWS_NOW_MS - 900_000,
    member_titles: [
      "Ceasefire talks resume as delegations return",
      "Delegations return to the negotiating table",
      "Regional leaders welcome renewed talks",
    ],
    primary_link: "https://www.reuters.com/world/ceasefire",
    primary_published_at_ms: NEWS_NOW_MS - 1_200_000,
    primary_source: "Reuters",
    primary_title: "Ceasefire talks resume as delegations return",
    source_count: 3,
    source_tier: 1,
    sources: ["Reuters", "Associated Press"],
    story_id: "story-ceasefire",
    threat_level: "high",
    unique_source_count: 2,
    upstream_importance_score: 91,
    ...overrides,
  };
}

export function newsGlobalBriefFixture(overrides: Partial<NewsBrief> = {}): NewsBrief {
  const publication = newsBriefPublicationFixture();
  return {
    latest_run: {
      attempt_count: 1,
      completed_at_ms: NEWS_NOW_MS,
      failure_count: 0,
      last_attempt_at_ms: NEWS_NOW_MS - 5_000,
      last_error_code: null,
      lease_expires_at_ms: null,
      model_outcome: "ok",
      next_due_at_ms: publication.slot_at_ms + 1_800_000,
      pointer_action: "advance_ok",
      slot_at_ms: publication.slot_at_ms,
      status: "completed",
      updated_at_ms: NEWS_NOW_MS,
    },
    next_due_at_ms: publication.slot_at_ms + 1_800_000,
    publication,
    slot_at_ms: publication.slot_at_ms,
    state: "current",
    ...overrides,
  };
}

export function newsStatusFixture(overrides: Partial<NewsStatus> = {}): NewsStatus {
  const status: NewsStatus = {
    last_success_at_ms: NEWS_NOW_MS - 60_000,
    layers: {
      brief: {
        latest_run: null,
        next_due_at_ms: NEWS_NOW_MS + 1_800_000,
        public_state: "current",
        publication_id: "brief-publication",
        reasons: [],
        slot_at_ms: NEWS_NOW_MS - (NEWS_NOW_MS % 1_800_000),
        status: "ready",
      },
      ingest: {
        opennews: {
          consecutive_failures: 0,
          live_connected: true,
          last_live_at_ms: NEWS_NOW_MS,
          last_recovery_at_ms: NEWS_NOW_MS - 60_000,
          last_error: null,
          last_http_status: 200,
          last_items_accepted: 5,
          last_items_seen: 5,
          last_outcome: "recovery_complete",
          last_rejection_counts: {},
          last_success_at_ms: NEWS_NOW_MS - 60_000,
          name: "OpenNews",
          source_id: "news-opennews",
        },
        reasons: [],
        rss: {
          claimed_source_count: 0,
          enabled: true,
          failed_source_count: 0,
          latest_success_at_ms: NEWS_NOW_MS - 30_000,
          next_due_at_ms: NEWS_NOW_MS + 60_000,
          source_count: 179,
          successful_source_count: 179,
        },
        status: "ready",
      },
      push: {
        baseline_at_ms: NEWS_NOW_MS - 3_600_000,
        delivery_24h: {
          completed: 1,
          latency_p95_ms: 30_000,
          over_120s: 0,
          slo_met: true,
        },
        enabled: true,
        feishu_signing_secret_configured: false,
        feishu_webhook_url_configured: true,
        initialized: true,
        latest_error: null,
        latest_error_at_ms: null,
        latest_sent_at_ms: NEWS_NOW_MS - 60_000,
        measured_at_ms: NEWS_NOW_MS,
        oldest_due_at_ms: null,
        pending_count: 0,
        reasons: [],
        retry_count: 0,
        sent_count: 1,
        status: "ready",
        suppressed_count: 0,
        terminal_count: 0,
        total_count: 1,
        translation_24h: {
          attempted: 1,
          failure_counts: {},
          latency_p95_ms: 1_500,
          slo_met: true,
          succeeded: 1,
          success_ratio: 1,
        },
      },
      story: {
        active_items: 4,
        active_stories: 1,
        classifier_version: "news-classifier-v1",
        identity_version: "news-story-identity-v1",
        importance_version: "news-importance-v1",
        invalid_owner_count: 0,
        invalid_story_aggregate_count: 0,
        invariant_error_count: 0,
        last_attempt_at_ms: NEWS_NOW_MS,
        last_error: null,
        last_material_change_at_ms: NEWS_NOW_MS - 60_000,
        last_success_at_ms: NEWS_NOW_MS - 60_000,
        newest_item_at_ms: NEWS_NOW_MS - 30_000,
        newest_story_at_ms: NEWS_NOW_MS - 30_000,
        reasons: [],
        status: "ready",
      },
    },
    measured_at_ms: NEWS_NOW_MS,
    operating_state: "live",
    reasons: [],
    status: "ready",
    ...overrides,
  };
  return status;
}

export function newsSourceFixture(overrides: Partial<NewsSource> = {}): NewsSource {
  return {
    claim_lease_expires_at_ms: null,
    consecutive_failures: 0,
    enabled: true,
    feed_url: "https://feeds.example.com/world.xml",
    last_error: null,
    last_fetch_finished_at_ms: NEWS_NOW_MS - 30_000,
    last_fetch_started_at_ms: NEWS_NOW_MS - 31_000,
    last_http_status: 200,
    last_items_accepted: 5,
    last_items_seen: 24,
    last_live_at_ms: null,
    last_outcome: "success",
    last_recovery_at_ms: null,
    last_rejection_counts: { item_cap: 19 },
    last_success_at_ms: NEWS_NOW_MS - 30_000,
    live_connected: false,
    name: "Example World",
    next_fetch_at_ms: NEWS_NOW_MS + 60_000,
    refresh_interval_seconds: 300,
    source_id: "wm-world-example",
    source_kind: "rss",
    tier: 1,
    ...overrides,
  };
}

export function newsSourcesFixture(overrides: Partial<NewsSources> = {}): NewsSources {
  return {
    items: [
      newsSourceFixture(),
      newsSourceFixture({
        feed_url: null,
        last_fetch_finished_at_ms: null,
        last_fetch_started_at_ms: null,
        last_live_at_ms: NEWS_NOW_MS - 5_000,
        last_outcome: "live_item",
        last_recovery_at_ms: NEWS_NOW_MS - 60_000,
        last_rejection_counts: {},
        live_connected: true,
        name: "OpenNews",
        next_fetch_at_ms: null,
        refresh_interval_seconds: null,
        source_id: "news-opennews",
        source_kind: "opennews",
      }),
    ],
    page: { has_more: false, next_cursor: null, returned_count: 2 },
    ...overrides,
  };
}
