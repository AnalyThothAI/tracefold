import type {
  NewsDelivery,
  NewsEvent,
  NewsEventDetail,
  NewsEventMember,
  NewsFeed,
  NewsFeedEvent,
  NewsMarketMark,
  NewsStatus,
  NewsVerdict,
} from "@features/news/useNewsPage";

export const NEWS_NOW_MS = 1_779_000_000_000;

export function newsFeedEventFixture(overrides: Partial<NewsFeedEvent> = {}): NewsFeedEvent {
  return {
    admission: "candidate",
    asset_class: "crypto",
    context_line: "BTC · 首次出现 · 同 storyline 24h 内 1 条",
    delivery: { settled_at_ms: NEWS_NOW_MS - 20_000, state: "sent" },
    display_title: "央行应对新的全球政策冲击",
    engine_type: "news",
    event_id: "evt-global-policy",
    family: "general",
    grounded_assets: ["BTC", "ETH"],
    ingest_mode: "live",
    last_member_at_ms: NEWS_NOW_MS - 30_000,
    leader_description: "Central banks respond as the policy outlook changes.",
    leader_title: "Central banks respond to a new global policy shock",
    leader_url: "https://www.reuters.com/world/story",
    macro_lexicon: true,
    member_count: 4,
    opened_at_ms: NEWS_NOW_MS - 120_000,
    presentation_outcome: "translated",
    priority: "high",
    provenance: ["opennews:1018"],
    provider_score_max: 88,
    published_at_ms: NEWS_NOW_MS - 25_000,
    reporting_origin: "Reuters World",
    storyline_key: "asset:BTC",
    triage: {
      degraded: false,
      direction: "bearish",
      event_type: "macro",
      final_decision: "push",
      headline_zh: "央行政策转向，风险资产承压",
      magnitude: 2,
      override_rule: null,
      scope: "macro",
      throttled_by: null,
    },
    watchlist_hits: ["BTC"],
    ...overrides,
  };
}

export function newsFeedFixture(overrides: Partial<NewsFeed> = {}): NewsFeed {
  return {
    events: [newsFeedEventFixture()],
    filters: {
      admission: null,
      decision: null,
      family: null,
      limit: 25,
      priority: null,
      q: null,
      sort: "latest",
      symbol: null,
    },
    next_cursor: null,
    ...overrides,
  };
}

export function newsEventFixture(overrides: Partial<NewsEvent> = {}): NewsEvent {
  const feedEvent = newsFeedEventFixture();
  return {
    admission: feedEvent.admission,
    asset_class: feedEvent.asset_class,
    context_line: feedEvent.context_line,
    engine_type: feedEvent.engine_type,
    event_id: feedEvent.event_id,
    family: feedEvent.family,
    grounded_assets: feedEvent.grounded_assets,
    ingest_mode: feedEvent.ingest_mode,
    last_member_at_ms: feedEvent.last_member_at_ms,
    leader_description: feedEvent.leader_description,
    leader_title: feedEvent.leader_title,
    leader_url: feedEvent.leader_url,
    macro_lexicon: feedEvent.macro_lexicon,
    member_count: feedEvent.member_count,
    opened_at_ms: feedEvent.opened_at_ms,
    priority: feedEvent.priority,
    provenance: feedEvent.provenance,
    provider_score_max: feedEvent.provider_score_max,
    published_at_ms: feedEvent.published_at_ms,
    reporting_origin: feedEvent.reporting_origin,
    storyline_key: feedEvent.storyline_key,
    watchlist_hits: feedEvent.watchlist_hits,
    ...overrides,
  };
}

export function newsEventMemberFixture(overrides: Partial<NewsEventMember> = {}): NewsEventMember {
  return {
    description: "Central banks respond as the policy outlook changes.",
    item_id: "news-item-reuters",
    jaccard_estimate: null,
    joined_at_ms: NEWS_NOW_MS - 120_000,
    match_kind: "leader",
    provenance: ["opennews:1018"],
    published_at_ms: NEWS_NOW_MS - 120_000,
    reporting_origin: "Reuters World",
    title: "Central banks respond to a new global policy shock",
    url: "https://www.reuters.com/world/story",
    ...overrides,
  };
}

export function newsVerdictFixture(overrides: Partial<NewsVerdict> = {}): NewsVerdict {
  return {
    created_at_ms: NEWS_NOW_MS - 90_000,
    degraded: false,
    error_code: null,
    final_decision: "push",
    model: "triage-model-v1",
    model_decision: "push",
    override_rule: null,
    policy_version: "news_triage_policy_v1",
    prompt_version: "news_triage_prompt_v1",
    published_at_ms: NEWS_NOW_MS - 60_000,
    rule_baseline_decision: "escalate",
    stage: "triage",
    throttled_by: null,
    trace: { latency_ms: 812, tokens_in: 640 },
    verdict: {
      actionable: true,
      assets: [{ role: "primary", symbol: "BTC" }],
      confidence: 0.82,
      decision: "push",
      direction: "bearish",
      event_type: "macro",
      headline_zh: "央行政策转向，风险资产承压",
      magnitude: 2,
      rationale: "Rates guidance changed materially against consensus.",
      scope: "macro",
    },
    ...overrides,
  };
}

export function newsDeliveryFixture(overrides: Partial<NewsDelivery> = {}): NewsDelivery {
  return {
    attempted_at_ms: NEWS_NOW_MS - 40_000,
    error_code: null,
    kind: "first",
    receipt: { message_id: "om_123" },
    settled_at_ms: NEWS_NOW_MS - 20_000,
    state: "sent",
    ...overrides,
  };
}

export function newsMarketMarkFixture(overrides: Partial<NewsMarketMark> = {}): NewsMarketMark {
  return {
    captured_at_ms: NEWS_NOW_MS - 10_000,
    event_id: "evt-global-policy",
    mark: "t0",
    market_type: "perp",
    oi_change_pct: null,
    open_interest: 1_250_000_000,
    price: 64_250.5,
    price_change_pct: null,
    symbol: "BTC",
    ...overrides,
  };
}

export function newsEventDetailFixture(overrides: Partial<NewsEventDetail> = {}): NewsEventDetail {
  return {
    deliveries: [newsDeliveryFixture()],
    event: newsEventFixture(),
    marks: [newsMarketMarkFixture()],
    members: [
      newsEventMemberFixture(),
      newsEventMemberFixture({
        item_id: "news-item-bloomberg",
        jaccard_estimate: 0.71,
        match_kind: "jaccard",
        published_at_ms: NEWS_NOW_MS - 90_000,
        reporting_origin: "Bloomberg",
        title: "Central banks scramble after policy shock",
        url: "https://www.bloomberg.com/news/story",
      }),
    ],
    presentation: {
      display_title: "央行应对新的全球政策冲击",
      outcome: "translated",
      provider: "deepl",
    },
    verdicts: [newsVerdictFixture()],
    ...overrides,
  };
}

export function newsStatusFixture(overrides: Partial<NewsStatus> = {}): NewsStatus {
  return {
    broker: {
      configured: true,
      connected: true,
      error_code: null,
      queues: {
        "news.triage": { consumers: 4, messages: 0 },
        "news.deep": { consumers: 1, messages: 0 },
      },
    },
    control: { mutes: [], paused: false },
    delivery: {
      delivery_available: true,
      e2e_p95_ms: 3_400,
      hourly_cap: 12,
      last_error_code: null,
      sent_1h: 2,
      sent_24h: 41,
      terminal_24h: 1,
    },
    ingest: {
      configured_strategy_ids: ["1018", "1019"],
      connected: true,
      last_error_code: null,
      last_frame_at_ms: NEWS_NOW_MS - 5_000,
      last_publish_at_ms: NEWS_NOW_MS - 6_000,
      open_incidents: [],
      provider_enabled_strategy_ids: ["1018", "1019"],
      strategy_warnings: [],
      token_configured: true,
    },
    measured_at_ms: NEWS_NOW_MS,
    pipeline: {
      analyst_model: "analyst-model-v1",
      candidates_24h: 180,
      decided_push_24h: 40,
      deep_24h: 6,
      events_1h: 12,
      events_24h: 320,
      throttled_24h: 9,
      triage_24h: 175,
      triage_degraded_24h: 2,
      triage_model: "triage-model-v1",
      triage_p50_ms: 640,
      triage_p95_ms: 1_900,
    },
    state: "ready",
    watchlist: ["BTC", "ETH", "SOL"],
    workers_state: "running",
    ...overrides,
  };
}
