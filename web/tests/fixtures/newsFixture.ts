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
  NewsEventReaction,
  NewsQuote,
  NewsReaction,
  NewsReview,
} from "@features/news/api/newsQueries";

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
    // #87: the same two tags the Gate grounded on, resolved against the instrument universe. A test that
    // needs the other case — a tag that names nothing — overrides one entry with `listed: false`.
    assets: [
      { base_symbol: "BTC", listed: true, symbol: "BTC", venue: "binance.perp" },
      { base_symbol: "ETH", listed: true, symbol: "ETH", venue: "binance.perp" },
    ],
    context_line: "BTC · 首次出现 · 同 storyline 24h 内 1 条",
    delivery: { error_code: null, settled_at_ms: NEWS_NOW_MS - 20_000, state: "sent" },
    engine_type: "news",
    event_id: "evt-global-policy",
    family: "general",
    focus_fact_context: "Central banks respond as the policy outlook changes.",
    focus_fact_id: "fact-global-policy",
    focus_fact_method: "single",
    focus_fact_text: "Central banks respond to a new global policy shock",
    focus_span_end: 53,
    focus_span_start: 0,
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
    // The server's split of the current filter across the three outcome groups; `total` is their sum.
    counts: { total: 320, pushed: 41, held: 271, pending: 8 },
    filters: {
      admission: null,
      decision: null,
      family: null,
      limit: 25,
      outcome: null,
      q: null,
      hours: null,
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
    assets: feedEvent.assets,
    context_line: feedEvent.context_line,
    engine_type: feedEvent.engine_type,
    event_id: feedEvent.event_id,
    family: feedEvent.family,
    focus_fact_context: feedEvent.focus_fact_context,
    focus_fact_id: feedEvent.focus_fact_id,
    focus_fact_method: feedEvent.focus_fact_method,
    focus_fact_text: feedEvent.focus_fact_text,
    focus_span_end: feedEvent.focus_span_end,
    focus_span_start: feedEvent.focus_span_start,
    grounded_assets: feedEvent.grounded_assets,
    ingest_mode: feedEvent.ingest_mode,
    last_member_at_ms: feedEvent.last_member_at_ms,
    leader_description: feedEvent.leader_description,
    leader_title: feedEvent.leader_title,
    leader_url: feedEvent.leader_url,
    macro_lexicon: feedEvent.macro_lexicon,
    member_count: feedEvent.member_count,
    opened_at_ms: feedEvent.opened_at_ms,
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
    fact_id: "fact-global-policy",
    fact_text: "Central banks respond to a new global policy shock",
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
        storyline_key: "asset:BTC",
      },
      stage: "gate",
      summary_zh: "已送审 · 关联 BTC ETH · 命中关注列表",
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
    evidence_snapshots: [],
    event: newsEventFixture(),
    // #87: only bases that actually collapse several names get a group, so the block explains a surprise
    // rather than restating a ticker that answers to itself.
    normalization: [{ aliases: ["BTC", "XBT"], base_symbol: "BTC", sources: ["operator"] }],
    outcome: newsOutcomeFixture(),
    reader_receipt: {
      delivery_state: "sent",
      error_code: null,
      received_at_ms: NEWS_NOW_MS - 20_000,
      rendered_card: {},
      state: "received",
    },
    review: { accepted: null, judgment_n: 0, uncertain: false },
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
    verdicts: [newsVerdictFixture()],
    ...overrides,
  };
}

export function newsStatusFixture(overrides: Partial<NewsStatus> = {}): NewsStatus {
  return {
    // #75 universe as the status route reports it. Without this the 标的表快照 card renders its
    // "no snapshot yet" note and no test exercises the figures at all.
    instruments: {
      base_symbols: 1286,
      by_class: { crypto: 1110, equity: 173, unknown: 18 },
      by_venue: { "binance.spot": 789, "binance.perp": 744, "hl.xyz": 101 },
      dangling_aliases: 0,
      delisted: 0,
      last_snapshot_ms: NEWS_NOW_MS - 3_600_000,
      reference_symbols: 13_134,
      trading: 2344,
      venues: 9,
    },
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
      grounded: 168,
      received: 320,
      received_1h: 12,
      tagged: 172,
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
      // #87: a provider tag that names nothing. The tag is its own label — inventing the English word it
      // collided with would be a guess.
      { count: 7, key: "SPOT", label_zh: "SPOT", stage: "ungrounded" },
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
    delivery: {
      delivery_available: true,
      e2e_p95_ms: 3_400,
      last_error_code: null,
      sent_1h: 2,
      sent_24h: 41,
      terminal_24h: 1,
    },
    ingest: {
      connected: true,
      last_error_code: null,
      last_frame_at_ms: NEWS_NOW_MS - 5_000,
      last_publish_at_ms: NEWS_NOW_MS - 6_000,
      open_incidents: [],
      token_configured: true,
    },
    learning_retention: {
      deleted_artifacts: 0,
      deleted_cases: 0,
      deleted_recordings: 0,
      eligible_artifacts: 0,
      eligible_cases: 0,
      eligible_recordings: 0,
      last_error_code: null,
      last_run_at_ms: NEWS_NOW_MS - 60_000,
      oldest_artifact_age_ms: 7 * 86_400_000,
      oldest_case_age_ms: 7 * 86_400_000,
      oldest_recording_age_ms: 7 * 86_400_000,
      updated_at_ms: NEWS_NOW_MS - 60_000,
    },
    measured_at_ms: NEWS_NOW_MS,
    pipeline: {
      candidate_share_24h: 0.94,
      candidates_24h: 180,
      decided_push_24h: 40,
      model_triage_24h: 41,
      telemetry_parse_failed_24h: 1,
      telemetry_parsed_24h: 139,
      telemetry_push_24h: 3,
      // Three numbers that must not agree, because they count three different things and the 全部 tab has
      // to read the right one: `received` counts provider items before the Gate (142), `events` counts the
      // Events those became and is the table's own universe (141 — one of them is still awaiting a
      // verdict), and `oi.by_rule_24h` sums to 140 judged verdicts. A fixture where they matched would let
      // the tab read any of the three and still pass.
      telemetry_received_24h: 142,
      telemetry_events_24h: 141,
      dropped_by_rule: { noise: 60, below_threshold: 20 },
      events_1h: 12,
      events_24h: 320,
      grounded_24h: 168,
      tagged_24h: 172,
      reviewed_external_miss_24h: 0,
      reviewed_should_push_24h: 1,
      pushed_by_rule: { model_push_actionable: 30, magnitude3: 10 },
      reasked_24h: 1,
      novelty_defaulted_24h: 0,
      suppressed_by_reason: { suppressed_pr_template: 8 },
      ungrounded_by_symbol_24h: { SPOT: 7, NEAR: 2 },
      throttled_24h: 9,
      throttled_by_key: { "storyline:theme:mideast_energy:cap3": 9 },
      triage_24h: 175,
      triage_degraded_24h: 2,
      triage_degraded_by_code_24h: { news_triage_timeout: 2 },
      reader_card_dedicated: false,
      reader_card_fallback_dedicated: false,
      reader_card_fallback_model: null,
      reader_card_model: "triage-model-v1",
      triage_fallback_model: null,
      triage_model: "triage-model-v1",
      triage_p50_ms: 640,
      triage_p95_ms: 1_900,
    },
    // #207: the deterministic OI lane. `by_rule_24h` is keyed on the judge's own gate names — those cannot
    // come from `dropped_by_rule`, which groups `override_rule` and reads `telemetry_deterministic` for
    // every OI verdict whether it pushed or withheld.
    oi: {
      by_rule_24h: {
        whale_ratio_below_threshold: 106,
        beyond_window_rank: 30,
        opening_move_with_whale_concentration: 3,
        oi_parse_failed: 1,
      },
      policy: {
        max_rank_in_window: 2,
        oi_change_at_least_bps: 0,
        whale_oi_ratio_above_bps: 8_000,
        window_ms: 14_400_000,
      },
      trade_floors: {
        enabled: false,
        max_price_move_bps: 600,
        min_oi_value_usd: 20_000_000,
        min_price_move_bps: 100,
        min_whale_long_profit_bps: 9_500,
        mode: "paper",
        pre_move_lookback_ms: 3_600_000,
      },
      window_occupancy: [
        { full: true, max_rank_in_window: 2, symbol: "WIF", used: 2 },
        { full: false, max_rank_in_window: 2, symbol: "DOGE", used: 1 },
      ],
    },
    state: "ready",
    watchlist: ["BTC", "ETH", "SOL"],
    workers_state: "running",
    ...overrides,
  };
}

/**
 * One judged telemetry frame, as the feed serves it (#207/#137). The `oi` block is the judge's own trace
 * read back, so a test that wants the withheld or the unparseable shape overrides `rule` and the fields that
 * shape actually carries rather than inventing new ones.
 */
export function newsOiFrameFixture(overrides: Partial<NewsFeedEvent> = {}): NewsFeedEvent {
  return newsFeedEventFixture({
    admission: "telemetry_deterministic",
    /*
     * Both empty, because that is what production sends. The Gate grounds `news_event_assets` from the
     * provider's coin tags at admission and a strategy-1019 frame ships none; the symbol is parsed out of
     * the title later, at Triage. A fixture that filled either of these is what let two successive
     * "fixed the symbol column" changes ship while the column stayed `—` on the live console.
     */
    assets: [],
    delivery: { error_code: null, settled_at_ms: NEWS_NOW_MS - 20_000, state: "sent" },
    event_id: "evt-oi-wif",
    family: "market_telemetry",
    grounded_assets: [],
    leader_title:
      "WIF OI Rise 6.71%, OI Value 11.03M, Whale Long Profit 88.40%, Whale/OI Ratio 143.90%",
    oi: {
      eligible_rank_in_window: 1,
      failure_stage: null,
      max_rank_in_window: 2,
      oi_change_at_least_bps: 0,
      oi_change_bps: 671,
      oi_value_usd: 11_030_000,
      parsed: true,
      parser_version: null,
      rank_semantics: "eligible_rank_v1",
      // The only place the feed carries the frame's subject for this lane.
      symbol: "WIF",
      rule: "opening_move_with_whale_concentration",
      title_sha256: null,
      whale_long_profit_bps: 8_840,
      whale_oi_ratio_above_bps: 8_000,
      whale_oi_ratio_bps: 14_390,
      window_ms: 14_400_000,
    },
    reaction: newsReactionFixture({ return_1h_bps: 203, return_4h_bps: 145, state: "complete" }),
    title_zh: null,
    triage: newsTriageFixture({
      /*
       * Empty, because that is what the feed serves. `_triage_summary` builds the slim shape for a feed
       * row and only the Event detail's `full=True` shape carries `assets`. A fixture that filled it here
       * let `/news/oi` read its SYMBOL column from a field production never sends, and the tests passed.
       */
      assets: [],
      // `evaluate_oi` maps a rising frame to `bullish`. Keeping the fixture self-consistent matters here:
      // the direction word and the OI change are two different measurements and a test that let them
      // disagree would be asserting a screen the pipeline cannot produce.
      direction: "bullish",
      direction_zh: "利多",
      event_type: "oi_spike",
      event_type_zh: "持仓异动",
      headline_zh: "▲ WIF 持仓异动6.71%｜持仓1103万｜鲸鱼占比143.9%｜鲸鱼多头盈利88.4%｜4h内第1次",
      magnitude: 2,
      magnitude_zh: "影响明显",
      override_rule: "telemetry_deterministic",
      scope: "single_name",
      scope_zh: "单一标的",
      why_zh: "",
    }),
    watchlist_hits: [],
    ...overrides,
  });
}

export function newsQuoteFixture(overrides: Partial<NewsQuote> = {}): NewsQuote {
  return {
    age_ms: 2_000,
    base_symbol: "BTC",
    change_basis: "rolling_24h",
    change_basis_zh: "滚动 24H",
    change_pct: 1.52,
    instrument_class: "crypto",
    price: "68123.4",
    price_kind: "last",
    price_kind_zh: "最新成交价",
    quote_asset: "USDT",
    received_at_ms: NEWS_NOW_MS - 2_000,
    requested_symbol: "BTC",
    source_at_ms: NEWS_NOW_MS - 2_100,
    state: "fresh",
    state_zh: "实时",
    symbol: "BTC",
    venue: "binance.perp",
    venue_symbol: "BTCUSDT",
    ...overrides,
  };
}

export function newsReactionFixture(overrides: Partial<NewsReaction> = {}): NewsReaction {
  return {
    asset_n: 1,
    metric_version: "reaction_v1",
    priced_n: 1,
    return_1h_bps: 152,
    return_4h_bps: -87,
    state: "complete",
    state_zh: "已完成",
    unavailable_reason: null,
    unavailable_reason_zh: "",
    ...overrides,
  };
}

export function newsEventReactionFixture(
  overrides: Partial<NewsEventReaction> = {},
): NewsEventReaction {
  return {
    anchor_at_ms: NEWS_NOW_MS - 6 * 3_600_000,
    instrument_class: "crypto",
    is_primary: true,
    metric_version: "reaction_v1",
    p0: "68000.0",
    p0_at_ms: NEWS_NOW_MS - 6 * 3_600_000,
    p1: "69033.6",
    p1_at_ms: NEWS_NOW_MS - 5 * 3_600_000,
    p4: "67408.4",
    p4_at_ms: NEWS_NOW_MS - 2 * 3_600_000,
    return_1h_bps: 152,
    return_4h_bps: -87,
    state: "complete",
    state_zh: "已完成",
    symbol: "BTC",
    unavailable_reason: null,
    unavailable_reason_zh: "",
    updated_at_ms: NEWS_NOW_MS,
    venue: "binance.perp",
    venue_symbol: "BTCUSDT",
    ...overrides,
  };
}

/** One actionable ReviewDesk task; market observations live behind a separate view. */
export function newsReviewFixture(overrides: Partial<NewsReview> = {}): NewsReview {
  return {
    counts: { delivered: 1 },
    mode: "event",
    next_cursor: null,
    reader_contract_version: "reader_contract_v2",
    rubric_version: "news_review_v2",
    status: "ready",
    tasks: [
      {
        accepted_review: null,
        agent_headline: "DRAM 合约价继续上涨",
        agent_why: "价格上涨改善厂商议价能力。",
        cohort: "v9/v6/test-model",
        evidence_ready: true,
        evidence_version: 1,
        event_id: "evt-dram",
        final_decision: "drop",
        final_decision_zh: "不推",
        headline: "South Korea DRAM export unit price continued to rise",
        mode: "event",
        opened_at_ms: NEWS_NOW_MS - 3_600_000,
        reader_receipt: { truth: "not_received", truth_zh: "未送达", state: null },
        review_status: "pending",
        selection: {
          reason: "semantic_or_policy_hold",
          reason_zh: "语义或政策未通过",
          sampling_probability: 0.1,
          selection_version: "news_review_sampler_v1",
          stratum: "model_drop",
          stratum_zh: "模型判定不推",
        },
        task_id: "evt.evt-dram.1.0123456789abcdef",
        task_version: "a".repeat(64),
      },
    ],
    view: "queue",
    ...overrides,
  };
}
