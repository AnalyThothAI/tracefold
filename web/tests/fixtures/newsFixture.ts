import type {
  NewsDelivery,
  NewsEvent,
  NewsEventDetail,
  NewsEventMember,
  NewsFeed,
  NewsFeedEvent,
  NewsOutcome,
  NewsStatus,
  NewsTimelineStep,
  NewsVerdict,
  NewsTriageSummary,
} from "@features/news/useNewsPage";

export const NEWS_NOW_MS = 1_779_000_000_000;

export function newsOutcomeFixture(overrides: Partial<NewsOutcome> = {}): NewsOutcome {
  return {
    group: "pushed",
    kind: "delivered",
    reason_zh: "模型判断值得推送",
    text_zh: "已推送",
    ...overrides,
  };
}

export function newsFeedEventFixture(overrides: Partial<NewsFeedEvent> = {}): NewsFeedEvent {
  return {
    admission: "candidate",
    asset_class: "crypto",
    context_line: "BTC · 首次出现 · 同 storyline 24h 内 1 条",
    delivery: { error_code: null, settled_at_ms: NEWS_NOW_MS - 20_000, state: "sent" },
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
    outcome: newsOutcomeFixture(),
    priority: "high",
    provenance: ["opennews:1018"],
    provider_score_max: 88,
    published_at_ms: NEWS_NOW_MS - 25_000,
    reporting_origin: "Reuters World",
    storyline_key: "asset:BTC",
    title_zh: "央行应对新的全球政策冲击",
    triage: newsTriageFixture(),
    watchlist_hits: ["BTC"],
    ...overrides,
  };
}

export function newsTriageFixture(overrides: Partial<NewsTriageSummary> = {}): NewsTriageSummary {
  return {
    actionable: true,
    assets: [
      { role: "primary", symbol: "BTC" },
      { role: "mentioned", symbol: "ETH" },
    ],
    audience: "macro",
    audience_zh: "宏观",
    confidence: 0.78,
    decision_zh: "推送",
    degraded: false,
    direction: "bearish",
    direction_zh: "利空",
    error_code: null,
    event_type: "macro",
    event_type_zh: "宏观",
    final_decision: "push",
    headline_zh: "央行政策转向，风险资产承压",
    magnitude: 2,
    magnitude_zh: "影响明显",
    model_decision: "push",
    model_decision_zh: "推送",
    novelty: "new_fact",
    novelty_zh: "新事实",
    override_rule: "model_push_actionable",
    scope: "macro",
    scope_zh: "宏观",
    throttled_by: null,
    title_zh: "央行应对新的全球政策冲击",
    why_zh: "利率指引与市场预期背离，风险资产定价需要重估",
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
      outcome: null,
      priority: null,
      q: null,
      hours: null,
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
      title_zh: "央行应对新的全球政策冲击",
      magnitude: 2,
      why_zh: "利率指引与市场预期背离，风险资产定价需要重估",
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

export function newsTimelineFixture(): NewsTimelineStep[] {
  return [
    {
      at_ms: NEWS_NOW_MS - 120_000,
      facts: {
        member_count: 4,
        origins: ["Bloomberg", "Reuters World"],
        reporting_origin: "Reuters World",
      },
      stage: "received",
      summary_zh: "来源 Reuters World · 归并 4 条同类报道（2 个来源）",
      title_zh: "收到",
    },
    {
      at_ms: NEWS_NOW_MS - 120_000,
      facts: {
        admission: "candidate",
        grounded_assets: ["BTC", "ETH"],
        priority: "high",
        storyline_key: "asset:BTC",
      },
      stage: "gate",
      summary_zh: "已送审 · 高优先级 · 关联 BTC ETH · 命中关注列表",
      title_zh: "门禁",
    },
    {
      at_ms: NEWS_NOW_MS - 90_000,
      facts: { direction: "bearish", event_type: "macro", magnitude: 2, model: "triage-model-v1" },
      stage: "triage",
      summary_zh: "央行政策转向，风险资产承压 · 利空 / 影响明显 / 宏观 · 模型建议：推送",
      title_zh: "审稿",
    },
    {
      at_ms: NEWS_NOW_MS - 90_000,
      facts: {
        final_decision: "push",
        override_rule: "model_push_actionable",
        storyline_key: "asset:BTC",
      },
      stage: "decide",
      summary_zh: "推送 · 模型判断值得推送",
      title_zh: "决策",
    },
    {
      at_ms: NEWS_NOW_MS - 20_000,
      facts: { kind: "first", state: "sent" },
      stage: "delivery",
      summary_zh: "已推送到飞书",
      title_zh: "推送",
    },
  ];
}

export function newsEventDetailFixture(overrides: Partial<NewsEventDetail> = {}): NewsEventDetail {
  return {
    deliveries: [newsDeliveryFixture()],
    event: newsEventFixture(),
    outcome: newsOutcomeFixture(),
    triage: newsTriageFixture(),
    timeline: newsTimelineFixture(),
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
    labels: [
      {
        created_at_ms: NEWS_NOW_MS - 5_000,
        label: { label: "good", note: "" },
        label_version: "news_label_v1",
        source: "human",
      },
    ],
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
        "news.raw": { consumers: 1, messages: 0 },
        "news.triage": { consumers: 4, messages: 3 },
        "news.deliver": { consumers: 1, messages: 0 },
      },
    },
    funnel_24h: {
      candidates: 180,
      decided_push: 40,
      delivered: 41,
      delivered_1h: 2,
      received: 320,
      received_1h: 12,
      triaged: 175,
    },
    health: {
      broker: { detail_zh: "raw 0 · triage 3 · deliver 0", level: "ok", summary_zh: "队列畅通" },
      delivery: { detail_zh: "", level: "ok", summary_zh: "24 小时已推送 41 条，最近 1 小时 2 条" },
      ingest: { detail_zh: "最近一帧 0 分钟前", level: "ok", summary_zh: "已连接，正在收帧" },
      model: { detail_zh: "p95 1.9 秒", level: "ok", summary_zh: "模型正常，24 小时降级 2/175" },
      overall: "ok",
    },
    reasons_24h: [
      { count: 60, key: "noise", label_zh: "模型判定为噪音", stage: "drop" },
      { count: 30, key: "model_push_actionable", label_zh: "模型判断值得推送", stage: "push" },
      { count: 20, key: "below_threshold", label_zh: "影响不够，未达推送标准", stage: "drop" },
      { count: 10, key: "magnitude3", label_zh: "重大事件", stage: "push" },
      {
        count: 9,
        key: "storyline:theme:mideast_energy:cap3",
        label_zh: "「中东与能源」话题 4 小时内已推 3 条",
        stage: "throttle",
      },
      {
        count: 8,
        key: "suppressed_pr_template",
        label_zh: "律所推广模板，规则直接拦截",
        stage: "gate",
      },
      { count: 2, key: "news_triage_timeout", label_zh: "模型超时", stage: "degraded" },
    ],
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
      candidate_share_24h: 0.94,
      candidates_24h: 180,
      decided_push_24h: 40,
      dropped_by_rule: { noise: 60, below_threshold: 20 },
      events_1h: 12,
      events_24h: 320,
      labeled_missed_24h: 1,
      pushed_by_rule: { model_push_actionable: 30, magnitude3: 10 },
      reasked_24h: 1,
      novelty_defaulted_24h: 0,
      suppressed_by_reason: { suppressed_pr_template: 8 },
      throttled_24h: 9,
      throttled_by_key: { "storyline:theme:mideast_energy:cap3": 9 },
      triage_24h: 175,
      triage_degraded_24h: 2,
      triage_degraded_by_code_24h: { news_triage_timeout: 2 },
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
