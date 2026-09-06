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
  NewsSymbol,
  NewsMarket,
  NewsMarketGroup,
  NewsMarketItem,
  NewsMarketObservation,
  NewsMarketSource,
  NewsWalletCard,
  NewsWalletCards,
  NewsWalletRosterMember,
  NewsWallets,
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
    event_kind: "news",
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
    // #207 PR-W1: the feed row carries the Event Reaction in its own column. A row whose horizons have not
    // matured overrides `state` — it must read 未到期, never 0.00%.
    reaction: newsReactionFixture(),
    reporting_origin: "Reuters World",
    storyline_key: "asset:BTC",
    triage: newsTriageFixture(),
    watchlist_hits: ["BTC"],
    ...overrides,
  };
}

export function newsTriageFixture(overrides: Partial<NewsTriageSummary> = {}): NewsTriageSummary {
  return {
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
    final_decision: "push",
    headline_zh: "央行政策转向，风险资产承压",
    magnitude: 2,
    magnitude_zh: "影响明显",
    novelty: "new_fact",
    novelty_zh: "新事实",
    override_rule: "trade_relevance_realtime",
    relevance: {
      affected_markets: ["crypto_broad", "rates"],
      channels: ["rates", "liquidity"],
      development_delta: "state_change",
      impact_breadth: "cross_asset",
      reader_value: "realtime",
      surprise: "material_vs_expectation",
      tradability: "direct",
    },
    scope: "macro",
    scope_zh: "宏观",
    throttled_by: null,
    taxonomy: {
      assertion_status: "confirmed",
      assertion_status_zh: "已确认",
      change_state: "updated",
      change_state_zh: "已更新",
      codebook_sha256: "6f978685c1ffeb6615bfb5dc05eecb9004ebb6f7de8732602e2823d09a12daac",
      event_family: "macro_policy_data",
      event_family_zh: "宏观政策与数据",
      source_authority: "reputable_secondary",
      source_authority_zh: "可信二手来源",
      subject_codes: ["medtop:20000379"],
      subject_labels_zh: ["货币政策"],
      taxonomy_version: "news_taxonomy_v1",
    },
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
      assertion_status: null,
      change_state: null,
      event_family: null,
      event_kind: null,
      final_decision: null,
      limit: 25,
      outcome: null,
      q: null,
      hours: null,
      source_authority: null,
      subject_code: null,
      symbol: null,
    },
    next_cursor: null,
    search: null,
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
    event_kind: feedEvent.event_kind,
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
  const triage = newsTriageFixture();
  return {
    created_at_ms: NEWS_NOW_MS - 90_000,
    degraded: false,
    error_code: null,
    evidence_sha256: "2".repeat(64),
    evidence_version: 1,
    final_decision: "push",
    focus_fact_id: "fact-global-policy",
    judgment_contract_version: "news_judgment_v2",
    judgment_origin: "model",
    judgment_sha256: "3".repeat(64),
    model: "triage-model-v1",
    model_editorial: { relevance: triage.relevance!, taxonomy: triage.taxonomy! },
    override_rule: null,
    policy_version: "news_triage_policy_v11",
    program_sha256: "4".repeat(64),
    program_version: "news_semantic_program_v9",
    published_at_ms: NEWS_NOW_MS - 60_000,
    rule_baseline_decision: "escalate",
    stage: "triage",
    throttled_by: null,
    verdict: {
      audience: "macro",
      assets: [{ role: "primary", symbol: "BTC" }],
      confidence: 0.82,
      direction: "bearish",
      headline_zh: "央行政策转向，风险资产承压",
      magnitude: 2,
      novelty: "new_fact",
      restates: -1,
      scope: "macro",
      why_zh: "利率指引与市场预期背离，风险资产定价需要重估",
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
      facts: { direction: "bearish", magnitude: 2, model: "triage-model-v1" },
      stage: "triage",
      summary_zh: "央行政策转向，风险资产承压 · 利空 / 影响明显 / 宏观",
      title_zh: "审稿",
    },
    {
      at_ms: NEWS_NOW_MS - 90_000,
      facts: {
        final_decision: "push",
        override_rule: "trade_relevance_realtime",
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
      summary_zh: "已送达",
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
        "news.raw": {
          consumers: 1,
          messages: 0,
          ready: 0,
          unacked: 0,
          delayed: 0,
          dead_letter_pending: 0,
          message_bytes: 0,
          max_length_bytes: 67108864,
          bytes_used_bps: 0,
          policy_ok: true,
          missing: false,
        },
        "news.triage": {
          consumers: 4,
          messages: 3,
          ready: 2,
          unacked: 1,
          delayed: 1,
          dead_letter_pending: 0,
          message_bytes: 1389,
          max_length_bytes: 4194304,
          bytes_used_bps: 3,
          policy_ok: true,
          missing: false,
        },
        "news.deliver": {
          consumers: 1,
          messages: 0,
          ready: 0,
          unacked: 0,
          delayed: 0,
          dead_letter_pending: 0,
          message_bytes: 0,
          max_length_bytes: 4194304,
          bytes_used_bps: 0,
          policy_ok: true,
          missing: false,
        },
      },
    },
    funnel_24h: {
      admitted: 180,
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
      { count: 30, key: "trade_relevance_realtime", label_zh: "符合实时推送条件", stage: "push" },
      { count: 20, key: "below_threshold", label_zh: "影响不够，未达推送标准", stage: "drop" },
      { count: 10, key: "magnitude3", label_zh: "重大事件", stage: "push" },
      {
        count: 9,
        key: "storyline:conflict:mideast_2026:cap3",
        label_zh: "「美伊冲突」话题 4 小时内已推 3 条",
        stage: "throttle",
      },
      {
        count: 8,
        key: "suppressed_pr_template",
        label_zh: "律所推广模板，规则直接拦截",
        stage: "gate",
      },
      { count: 2, key: "news_program_route_deadline", label_zh: "语义程序超时", stage: "degraded" },
    ],
    delivery: {
      delivery_available: true,
      e2e_p50_ms: 2_100,
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
      admitted_24h: 180,
      candidate_share_24h: 0.94,
      candidates_24h: 180,
      decided_push_24h: 40,
      model_triage_24h: 41,
      source_classifier_version: "opennews_source_classifier_v1",
      // The editorial funnel, and only that (#553 PR-1). Market intake is counted off the stored
      // observations and reported per kind by `/api/news/market`, never here.
      source_contracts_24h: {
        news_v1: { received: 170, parsed: 170, verdict: 100 },
        listing_v1: { received: 8, parsed: 8, verdict: 8 },
      },
      dropped_by_rule: { noise: 60, below_threshold: 20 },
      events_1h: 12,
      events_24h: 320,
      funnel_admitted_24h: 180,
      funnel_delivered_24h: 41,
      funnel_received_24h: 320,
      funnel_triaged_24h: 175,
      grounded_24h: 168,
      tagged_24h: 172,
      reviewed_external_miss_24h: 0,
      reviewed_should_push_24h: 1,
      pushed_by_rule: { trade_relevance_realtime: 30, magnitude3: 10 },
      reasked_24h: 1,
      suppressed_by_reason: { suppressed_pr_template: 8 },
      ungrounded_by_symbol_24h: { SPOT: 7, NEAR: 2 },
      throttled_24h: 9,
      throttled_by_key: { "storyline:conflict:mideast_2026:cap3": 9 },
      triage_24h: 175,
      triage_degraded_24h: 2,
      triage_degraded_by_code_24h: { news_program_route_deadline: 2 },
      reader_card_dedicated: false,
      reader_card_fallback_dedicated: false,
      reader_card_fallback_model: null,
      reader_card_model: "triage-model-v1",
      triage_fallback_model: null,
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

/**
 * One market observation, as `/api/news/market` serves it (#553 PR-1).
 *
 * The default is the parsed OI shape. A test that wants the retained-but-unparsed shape overrides
 * `parse_status`, `parse_error` and drops the measurements, rather than inventing fields the row never
 * carries: `unknown_market` has no parser at all, and that is a kind rather than a failure.
 */
export function newsMarketObservationFixture(
  overrides: Partial<NewsMarketObservation> = {},
): NewsMarketObservation {
  return {
    event_at_ms: NEWS_NOW_MS - 120_000,
    group_key: "oi:WIF",
    historical: false,
    ingest_mode: "live",
    item_id: "mkt-oi-wif-3",
    market_kind: "oi",
    notification_reason: "",
    notification_status: "sent",
    oi_change_bps: 671,
    oi_value_usd: 11_030_000,
    parse_error: null,
    parse_status: "parsed",
    provider: "opennews",
    received_at_ms: NEWS_NOW_MS - 118_000,
    source_venue: "binance",
    symbol: "WIF",
    title: "WIF OI Rise 6.71%, OI Value 11.03M, Whale Long Profit 88.40%, Whale/OI Ratio 143.90%",
    whale_long_profit_bps: 8_840,
    whale_oi_ratio_bps: 14_390,
    ...overrides,
  };
}

/**
 * One run of consecutive observations of a group, collapsed to its newest member.
 *
 * `notification_status` and `notification_reason` are open server strings written by the notification
 * owner, and they are kept apart from `parse_status` here for the same reason the page keeps them apart:
 * a record that parsed cleanly and was not pushed is not a parse failure.
 */
export function newsMarketGroupFixture(overrides: Partial<NewsMarketGroup> = {}): NewsMarketGroup {
  const latest = overrides.latest ?? newsMarketObservationFixture();
  return {
    first_event_at_ms: NEWS_NOW_MS - 900_000,
    group_key: latest.group_key,
    last_event_at_ms: latest.event_at_ms,
    market_kind: latest.market_kind,
    notification_reason: latest.notification_reason,
    notification_status: latest.notification_status,
    observation_count: 3,
    // The run's oldest member: the server's cursor anchors here and so does the page's dedupe, because
    // it holds still while the run grows at the newest end.
    oldest_item_id: `${latest.item_id}-oldest`,
    oldest_received_at_ms: latest.received_at_ms - 120_000,
    ...overrides,
    latest,
  };
}

export function newsMarketSourceFixture(
  overrides: Partial<NewsMarketSource> = {},
): NewsMarketSource {
  return {
    failed: 0,
    groups: 24,
    last_failed_at_ms: null,
    last_received_at_ms: NEWS_NOW_MS - 118_000,
    last_sent_at_ms: NEWS_NOW_MS - 118_000,
    last_unknown_at_ms: null,
    market_kind: "oi",
    merged: 37,
    parsed: 61,
    raw: 0,
    received: 61,
    sent: 24,
    unknown: 0,
    ...overrides,
  };
}

/**
 * The market page as one read: the collapsed groups, and the per-kind intake and receipt summary beside
 * them. There is no page-level "is push wired" flag: every group answers that for itself (#553 PR-2).
 */
export function newsMarketFixture(overrides: Partial<NewsMarket> = {}): NewsMarket {
  return {
    filters: {
      from_ms: NEWS_NOW_MS - 72 * 3_600_000,
      kind: null,
      limit: 50,
      to_ms: NEWS_NOW_MS,
    },
    groups: [newsMarketGroupFixture()],
    next_cursor: null,
    // A page that did not fill its scan. Flip it to see the floor note beside the run counts.
    scan_truncated: false,
    sources: [
      newsMarketSourceFixture(),
      newsMarketSourceFixture({
        groups: 9,
        last_received_at_ms: NEWS_NOW_MS - 240_000,
        market_kind: "liquidation",
        parsed: 12,
        raw: 0,
        received: 12,
      }),
      newsMarketSourceFixture({
        groups: 2,
        last_received_at_ms: NEWS_NOW_MS - 600_000,
        market_kind: "smart_money",
        parsed: 4,
        raw: 0,
        received: 4,
      }),
      // No parser, so every record is retained raw. `raw` is the shape, not a failure count.
      newsMarketSourceFixture({
        groups: 1,
        last_received_at_ms: NEWS_NOW_MS - 1_800_000,
        market_kind: "unknown_market",
        parsed: 0,
        raw: 2,
        received: 2,
      }),
      // #572 PR-2. Derived by the chain tape from the fills of the followed wallets rather than
      // reported by a provider, so it is always `parsed` and never `raw`.
      newsMarketSourceFixture({
        groups: 3,
        last_received_at_ms: NEWS_NOW_MS - 90_000,
        market_kind: "wallet",
        merged: 0,
        parsed: 3,
        raw: 0,
        received: 3,
        sent: 3,
      }),
    ],
    ...overrides,
  };
}

/**
 * The chain wallet tape's own page (#572 PR-3): one roster version, the tape's position, and the two
 * windowed counts the header tiles are built from.
 */
export function newsWalletRosterMemberFixture(
  overrides: Partial<NewsWalletRosterMember> = {},
): NewsWalletRosterMember {
  return {
    closed_trades: 46,
    followers: 123_456,
    handle: "0xVantaa",
    open_cost: 220_000,
    profit_factor: 1.6,
    provider: "robinhoodtrenches",
    rank_quality: 1,
    rank_whale: null,
    realized_pnl: 510_000,
    wallet: "0x69326e48f68500fb6cf3b3a7da640737b9cc347b",
    win_rate: 0.44,
    ...overrides,
  };
}

export function newsWalletsFixture(overrides: Partial<NewsWallets> = {}): NewsWallets {
  return {
    cards: [
      { cards: 4, kind: "exit", last_event_at_ms: NEWS_NOW_MS - 90_000, sent: 3 },
      { cards: 1, kind: "crowding", last_event_at_ms: NEWS_NOW_MS - 600_000, sent: 1 },
      { cards: 6, kind: "digest", last_event_at_ms: NEWS_NOW_MS - 1_200_000, sent: 6 },
    ],
    fills: [
      { fills: 62, kind: "buy", tokens: 31, unpriced: 2, usd: "394120.55", wallets: 18 },
      { fills: 44, kind: "sell", tokens: 22, unpriced: 1, usd: "309002.10", wallets: 14 },
      { fills: 9, kind: "transfer_out", tokens: 6, unpriced: 9, usd: "0", wallets: 5 },
    ],
    roster: {
      members: [
        newsWalletRosterMemberFixture(),
        newsWalletRosterMemberFixture({
          followers: 84_000,
          handle: "smol_intern",
          rank_quality: null,
          rank_whale: 2,
          realized_pnl: -22_100,
          wallet: "0x2222222222222222222222222222222222222222",
        }),
      ],
      provider: "robinhoodtrenches",
      roster_version: 3,
      taken_at_ms: NEWS_NOW_MS - 1_800_000,
    },
    tape: {
      high_water_block: 55_432_960,
      high_water_tx_index: 2_147_483_647,
      ignored_inbound_total: 14,
      last_error: null,
      last_outcome: "success",
      last_success_at_ms: NEWS_NOW_MS - 2_000,
      noise_through_block: 55_432_990,
      noise_through_tx_index: 6,
      roster_version: 3,
      unknown_total: 1,
      updated_at_ms: NEWS_NOW_MS - 2_000,
    },
    window_from_ms: NEWS_NOW_MS - 24 * 3_600_000,
    window_to_ms: NEWS_NOW_MS,
    ...overrides,
  };
}

export function newsWalletCardFixture(overrides: Partial<NewsWalletCard> = {}): NewsWalletCard {
  return {
    basis: "chain_balance",
    closed: true,
    delivery_key: "wallet-delivery-1",
    delivery_state: "sent",
    digest_lines: null,
    digest_model_used: null,
    entry_price: "0.0018",
    event_at_ms: NEWS_NOW_MS - 90_000,
    handle: "0xVantaa",
    item_id: "a".repeat(64),
    kind: "exit",
    mark_price: "0.0025",
    outcome_1h_source: "dexscreener",
    outcome_4h_source: null,
    peer_wallets: 0,
    position_usd: "23531.60",
    premium_bps: null,
    ratio_bps: 10_000,
    return_1h_bps: -512,
    return_4h_bps: null,
    settled_at_ms: NEWS_NOW_MS - 88_000,
    token: "0x8de9018c1bb82884245f06dede9fe2bebabd1e18",
    token_symbol: "FSD",
    tone: "",
    usd: "23531.60",
    wallet: "0x69326e48f68500fb6cf3b3a7da640737b9cc347b",
    window_from_ms: NEWS_NOW_MS - 90_000,
    window_to_ms: NEWS_NOW_MS - 90_000,
    ...overrides,
  };
}

export function newsWalletCardsFixture(overrides: Partial<NewsWalletCards> = {}): NewsWalletCards {
  return {
    cards: [
      newsWalletCardFixture(),
      newsWalletCardFixture({
        basis: null,
        closed: false,
        delivery_state: "sent",
        item_id: "b".repeat(64),
        kind: "crowding",
        peer_wallets: 3,
        premium_bps: 5_400,
        ratio_bps: null,
        return_1h_bps: 1_730,
        return_4h_bps: null,
        token_symbol: "MADETEST",
        tone: "late",
        usd: "5400.00",
      }),
      // The digest carries its own sentences and says who wrote them; every other kind carries neither.
      newsWalletCardFixture({
        basis: null,
        closed: false,
        digest_lines: ["合计买入 62 笔 $394,120.55，卖出 44 笔 $309,002.10", "窗口内退出卡 4 张"],
        digest_model_used: true,
        handle: "",
        item_id: "c".repeat(64),
        kind: "digest",
        outcome_1h_source: "unavailable",
        position_usd: null,
        ratio_bps: null,
        return_1h_bps: null,
        token: "",
        token_symbol: null,
        usd: null,
        wallet: "",
      }),
    ],
    limit: 100,
    window: "24h",
    window_from_ms: NEWS_NOW_MS - 24 * 3_600_000,
    window_to_ms: NEWS_NOW_MS,
    ...overrides,
  };
}

/** A wallet digest observation: no provider line, no subject, and its own sentences (#572 PR-3). */
export function newsMarketDigestObservationFixture(
  overrides: Partial<NewsMarketObservation> = {},
): NewsMarketObservation {
  return newsMarketObservationFixture({
    group_key: "wallet|digest|robinhood_chain|||1779000000000",
    item_id: "mkt-wallet-digest-1",
    market_kind: "wallet",
    oi_change_bps: null,
    oi_value_usd: null,
    provider: "robinhood_chain",
    source_venue: null,
    symbol: null,
    title: "名单钱包 4 小时摘要",
    wallet_digest_lines: [
      "合计买入 62 笔 $394,120.55，卖出 44 笔 $309,002.10",
      "窗口内退出卡 4 张、拥挤卡 1 张，其中已送达 5 张",
    ],
    wallet_kind: "digest",
    whale_long_profit_bps: null,
    whale_oi_ratio_bps: null,
    ...overrides,
  });
}

export function newsMarketItemFixture(overrides: Partial<NewsMarketItem> = {}): NewsMarketItem {
  const observation = overrides.observation ?? newsMarketObservationFixture();
  return {
    description: "Open interest rose over the provider's own trigger window.",
    notification_covered_item_ids: [observation.item_id],
    notification_delivery: {
      attempts: 1,
      card: {
        elements: [
          { content: "上升 6.71% · 14:20\nOI $11.03M · binance · oi_signal_v1", tag: "markdown" },
        ],
        header: { template: "blue", title: { content: "持仓异动 WIF", tag: "plain_text" } },
      },
      covered_count: 2,
      covered_from_ms: observation.received_at_ms - 600_000,
      covered_to_ms: observation.received_at_ms,
      delivery_key: "0".repeat(32),
      error: null,
      first_attempt_at_ms: observation.received_at_ms + 2_000,
      last_attempt_at_ms: observation.received_at_ms + 2_000,
      next_attempt_at_ms: observation.received_at_ms,
      receipt_provider: "feishu",
      settled_at_ms: observation.received_at_ms + 2_400,
      state: "sent",
      trigger_item_id: observation.item_id,
      trigger_reason: "followup",
    },
    notification_reason: observation.notification_reason,
    notification_status: observation.notification_status,
    provider_params: { rule: "oi_rise", window_minutes: 15 },
    raw_first_line: observation.title,
    timeline: [
      observation,
      newsMarketObservationFixture({
        event_at_ms: observation.event_at_ms - 600_000,
        item_id: "mkt-oi-wif-2",
        oi_change_bps: 512,
        title:
          "WIF OI Rise 5.12%, OI Value 10.41M, Whale Long Profit 86.10%, Whale/OI Ratio 139.20%",
      }),
    ],
    ...overrides,
    observation,
  };
}

export function newsQuoteFixture(overrides: Partial<NewsQuote> = {}): NewsQuote {
  return {
    base_symbol: "BTC",
    change_basis: "rolling_24h",
    change_basis_zh: "滚动 24H",
    change_pct: 1.52,
    instrument_class: "crypto",
    effective_age_ms: 2_100,
    freshness_basis: "source_and_received",
    price: "68123.4",
    price_kind: "last",
    price_kind_zh: "最新成交价",
    quote_asset: "USDT",
    received_age_ms: 2_000,
    received_at_ms: NEWS_NOW_MS - 2_000,
    reference_age_ms: 5_000,
    reference_at_ms: NEWS_NOW_MS - 5_000,
    requested_symbol: "BTC",
    source_at_ms: NEWS_NOW_MS - 2_100,
    source_age_ms: 2_100,
    state: "fresh",
    state_zh: "报价正常",
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

/**
 * One `base_symbol`'s identity card (#207 PR-W1).
 *
 * `us.listed` is in the contracts on purpose: it is what makes `known` and `tradeable` two different
 * answers, and a fixture that only ever shipped tradeable venues could not tell them apart. A test that
 * wants the name-nothing-lists case passes `{ contracts: [], known: false, tradeable: false, venues: [] }`.
 */
export function newsSymbolFixture(overrides: Partial<NewsSymbol> = {}): NewsSymbol {
  return {
    base_symbol: "WIF",
    contracts: [
      {
        instrument_class: "crypto",
        quote_asset: "USDT",
        reference_only: false,
        venue: "binance.perp",
        venue_symbol: "WIFUSDT",
      },
      {
        instrument_class: "crypto",
        quote_asset: "USDC",
        reference_only: false,
        venue: "hl.perp",
        venue_symbol: "WIF",
      },
    ],
    known: true,
    normalization: { aliases: ["WIF", "XYZ-WIF"], base_symbol: "WIF", sources: ["seed"] },
    tradeable: true,
    venues: ["binance.perp", "hl.perp"],
    ...overrides,
  };
}
