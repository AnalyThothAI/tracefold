import type { Page, Route } from "@playwright/test";
import {
  macroModuleFixture,
  macroOverviewFixture,
} from "@tests/fixtures/macroFixture";
import { marketContextFixture, marketObservationFixture } from "@tests/fixtures/marketFixtures";
import {
  newsFeedFixture,
  newsGlobalBriefFixture,
  newsStoryDetailFixture,
  newsStoryFixture,
} from "@tests/fixtures/newsFixture";
import { tokenCaseFixture, tokenCasePostsFixture } from "@tests/fixtures/tokenCaseFixture";

const NOW = 1_777_746_300_000;
const ADDRESS = "0x6982508145454Ce325dDbE47a25d4ec3d2311933";
const TARGET_ID = `asset:dex:eth:${ADDRESS.toLowerCase()}`;
const unhandledApiRequests = new WeakMap<Page, string[]>();

export type MockApiOptions = {
  delayNonBootstrapMs?: number;
  failNonBootstrap?: boolean;
};

export async function installMockApi(page: Page, options: MockApiOptions = {}) {
  unhandledApiRequests.set(page, []);

  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;

    if (path !== "/api/bootstrap") {
      if (options.delayNonBootstrapMs) {
        await new Promise((resolve) => setTimeout(resolve, options.delayNonBootstrapMs));
      }
      if (options.failNonBootstrap) {
        return route.abort("failed");
      }
    }

    if (path === "/api/bootstrap") {
      return fulfill(route, {
        ws_token: "secret",
        replay_limit: 25,
      });
    }
    if (path === "/api/status") return fulfill(route, statusData());
    if (path === "/api/token-radar") return fulfill(route, tokenRadarData(url));
    if (path === "/api/token-case") return fulfill(route, tokenCaseData(url));
    if (path.startsWith("/api/token-images/")) return fulfillTokenImage(route);
    if (path === "/api/search/inspect") return fulfill(route, searchInspectData(url));
    if (path === "/api/events/by-ids") return fulfill(route, socialEventsByIds(url));
    if (path === "/api/target-social-timeline") return fulfill(route, timelineData());
    if (path === "/api/target-posts") return fulfill(route, targetPostsData(url));
    if (path === "/api/news/feed") return fulfill(route, newsFeedData());
    if (path.startsWith("/api/news/stories/")) return fulfill(route, newsStoryDetailData(path));
    if (path === "/api/news/brief") return fulfill(route, newsGlobalBriefFixture());
    if (path === "/api/stocks-radar") return fulfill(route, stocksRadarData(url));
    if (path === "/api/macro/overview") return fulfill(route, macroOverviewFixture());
    if (path === "/api/macro/rates-fed") return fulfill(route, macroModuleFixture("rates_fed"));
    if (path === "/api/macro/economy-inflation") {
      return fulfill(route, macroModuleFixture("economy_inflation"));
    }
    if (path === "/api/macro/liquidity-funding") {
      return fulfill(route, macroModuleFixture("liquidity_funding"));
    }
    if (path === "/api/macro/credit") return fulfill(route, macroModuleFixture("credit"));
    if (path === "/api/macro/volatility") {
      return fulfill(route, macroModuleFixture("volatility"));
    }
    if (path === "/api/macro/cross-asset") {
      return fulfill(route, macroModuleFixture("cross_asset"));
    }
    recordUnhandledApiRequest(page, url);
    return route.fulfill({
      status: 404,
      contentType: "application/json",
      body: JSON.stringify({ ok: false, error: `unhandled ${path}` }),
    });
  });
}

export function getUnhandledApiRequests(page: Page): string[] {
  return [...(unhandledApiRequests.get(page) ?? [])];
}

function recordUnhandledApiRequest(page: Page, url: URL) {
  const requests = unhandledApiRequests.get(page) ?? [];
  requests.push(`${url.pathname}${url.search}`);
  unhandledApiRequests.set(page, requests);
}

function newsFeedData() {
  const story = newsStoryFixture({
    story_id: "story-global-policy",
    title: "Macro desk flags liquidity rotation",
    description: "Liquidity rotation is visible across crypto beta and rates-sensitive assets.",
  });
  return { ...newsFeedFixture(), stories: [story] };
}

function newsStoryDetailData(path: string) {
  const storyId = decodeURIComponent(path.split("/").pop() ?? "story-global-policy");
  return newsStoryDetailFixture({
    story_id: storyId,
    title: "Macro desk flags liquidity rotation",
    description: "Liquidity rotation is visible across crypto beta and rates-sensitive assets.",
  });
}

async function fulfill(route: Route, data: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({ ok: true, data }),
  });
}

async function fulfillTokenImage(route: Route) {
  return route.fulfill({
    status: 200,
    contentType: "image/svg+xml",
    body: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40"><rect width="40" height="40" rx="8" fill="#121713"/><circle cx="20" cy="20" r="12" fill="#d99a28"/></svg>`,
  });
}

function statusData() {
  return {
    ok: true,
    reasons: [],
    measured_at_ms: NOW,
    db: {
      ok: true,
      schema_ok: true,
      current_revision: "20260731_0233",
      expected_revision: "20260731_0233",
      error_code: null,
    },
    workers_runtime: {
      runtime_id: "1d36ca48-c41d-4d7b-a26d-86c2429a3e10",
      runtime_version: "a521557",
      state: "running",
      started_at_ms: NOW,
      heartbeat_at_ms: NOW,
      heartbeat_stale_after_ms: 15_000,
      fatal_code: null,
      unavailable_reason: null,
    },
  };
}

function tokenRadarData(url: URL) {
  const targets = shouldReturnLongMobileRadarList(url)
    ? Array.from({ length: 8 }, () => assetFlowRow())
    : [assetFlowRow()];
  return {
    window: url.searchParams.get("window") ?? "1h",
    venue: url.searchParams.get("venue") ?? "all",
    targets,
    attention: [],
    projection: {
      status: "fresh",
      version: "e2e-token-radar",
      source: "token_radar_current_rows",
      venue: url.searchParams.get("venue") ?? "all",
      reason: null,
      latest_attempt_status: "ready",
      row_count: targets.length,
      source_rows: targets.length,
      source_max_received_at_ms: NOW,
      source_frontier_ms: NOW,
      computed_at_ms: NOW,
      error: null,
      anchor_coverage: {
        status: "fresh",
        ready: targets.length,
        missing: 0,
        total: targets.length,
      },
      quality_status: "ready",
      degraded_reasons: [],
      unresolved: {
        identity_missing_count: 0,
        nil_count: 0,
        ambiguous_count: 0,
        sample_symbols: [],
      },
    },
  };
}

function shouldReturnLongMobileRadarList(url: URL) {
  return url.searchParams.get("window") === "24h";
}

function assetFlowRow() {
  const attention = {
    mentions_5m: 2,
    mentions_1h: 4,
    mentions_4h: 4,
    mentions_24h: 4,
    mentions_window: 4,
    unique_authors: 3,
    latest_seen_ms: NOW,
    previous_mentions: 0,
    mention_delta: 4,
    mention_delta_pct: null,
    z_score: null,
    new_burst_score: 80,
    stream_share: 0,
    baseline_status: "insufficient_history",
    baseline_sample_count: 0,
  };
  const market = marketContextFixture({
    event_anchor: marketObservationFixture({
      target_type: "Asset",
      target_id: TARGET_ID,
      source: "event_anchor",
      provider: "gmgn_dex_quote",
      price_usd: 0.001,
      market_cap_usd: 60_490,
      liquidity_usd: 250_000,
      observed_at_ms: NOW - 60_000,
      received_at_ms: NOW - 60_000,
    }),
    decision_latest: marketObservationFixture({
      target_type: "Asset",
      target_id: TARGET_ID,
      source: "decision_latest",
      provider: "okx_dex_price",
      price_usd: 0.00112,
      market_cap_usd: 66_000,
      liquidity_usd: 250_000,
      observed_at_ms: NOW,
      received_at_ms: NOW,
    }),
  });
  return {
    intent: {
      intent_id: `intent:${TARGET_ID}`,
      event_id: "event-upeg-1",
      display_symbol: "UPEG",
      display_name: null,
      evidence: [],
    },
    radar: {
      lane: "resolved",
      rank: 1,
      listed_at_ms: NOW - 60_000,
      computed_at_ms: NOW,
      source_max_received_at_ms: NOW,
    },
    resolution: {
      status: "EXACT",
      target_type: "Asset",
      target_id: TARGET_ID,
      pricefeed_id: null,
      reason_codes: ["CHAIN_ADDRESS_EXACT"],
      candidate_ids: [TARGET_ID],
      lookup_keys: [],
      discovery: [],
    },
    factor_snapshot: factorSnapshot({ attention, market }),
    quality: { status: "ready", degraded_reasons: [] },
  };
}

function factorSnapshot({ attention, market }: { attention: any; market: any }) {
  return {
    schema_version: "token_factor_snapshot_v5_provider_neutral",
    subject: {
      target_type: "Asset",
      target_id: TARGET_ID,
      symbol: "UPEG",
      chain: "eip155:1",
      address: ADDRESS,
      target_market_type: "dex",
      pricefeed_id: null,
    },
    market,
    gates: {
      eligible_for_high_alert: true,
      max_decision: "high_alert",
      blocked_reasons: [],
      risk_reasons: [],
    },
    data_health: { identity: "ready", market: "ready", social: "ready", alpha: "ready" },
    families: {
      social_heat: family(86, 0.55, {
        mentions_5m: attention.mentions_5m,
        mentions_1h: attention.mentions_1h,
        mentions_4h: attention.mentions_4h,
        mentions_24h: attention.mentions_24h,
        unique_authors: attention.unique_authors,
        latest_seen_ms: attention.latest_seen_ms,
        previous_mentions: attention.previous_mentions,
        mention_delta: attention.mention_delta,
        mention_delta_pct: attention.mention_delta_pct,
        z_score: attention.z_score,
        new_burst_score: attention.new_burst_score,
        stream_share: attention.stream_share,
        baseline_status: attention.baseline_status,
        baseline_sample_count: attention.baseline_sample_count,
        status: "rising",
      }),
      social_propagation: family(72, 0.45, {
        mentions: attention.mentions_window,
        independent_authors: attention.unique_authors,
        duplicate_text_share: 0,
        informative_post_count: attention.mentions_window,
      }),
      timing_risk: family(50, 0, {
        social_signal_start_ms: NOW - 60_000,
        price_change_since_social_pct: 0.12,
        price_change_before_social_pct: null,
      }),
    },
    normalization: {
      status: "ready",
      cohort_status: "ready",
      cohort: { window: "1h" },
      factor_ranks: {
        social_heat: 0.86,
        social_propagation: 0.72,
        timing_risk: 0.5,
      },
      alpha_rank: 4,
    },
    composite: {
      raw_alpha_score: 79,
      rank_score: 79,
      recommended_decision: "high_alert",
      family_scores: {
        social_heat: 86,
        social_propagation: 72,
        timing_risk: 50,
      },
    },
    provenance: { source_event_ids: ["event-upeg-1", "event-upeg-2"], computed_at_ms: NOW },
  };
}

function family(score: number, weight: number, facts: Record<string, unknown>) {
  return {
    raw_score: score,
    score,
    weight,
    facts,
    factors: {
      primary: {
        family: "e2e",
        key: "primary",
        raw_value: score,
        score,
        confidence: 0.95,
        data_health: "ready",
        source_refs: [],
        risk_flags: [],
      },
    },
    data_health: "ready",
  };
}

function tokenCaseData(url: URL) {
  const dossier = tokenCaseFixture();
  const targetType = url.searchParams.get("target_type") ?? dossier.target.target_type;
  const targetId = url.searchParams.get("target_id") ?? dossier.target.target_id;
  const window = url.searchParams.get("window") ?? dossier.timeline.query.window;
  return {
    ...dossier,
    target: { ...dossier.target, target_type: targetType, target_id: targetId },
    timeline: {
      ...dossier.timeline,
      query: {
        ...dossier.timeline.query,
        target_type: targetType,
        target_id: targetId,
        window,
      },
    },
    posts: {
      ...dossier.posts,
      query: {
        ...dossier.posts.query,
        target_type: targetType,
        target_id: targetId,
        window,
      },
    },
    market_live: {
      ...dossier.market_live,
      target_type: targetType,
      target_id: targetId,
    },
  };
}

function targetPostsData(url: URL) {
  const targetId = url.searchParams.get("target_id") ?? "";
  if (targetId.includes("FhoxjfsuStvRQKRXSuB9ZDB7WRGjqhUPxa3NztWspump")) {
    return tokenCasePostsData(url);
  }
  return postsData();
}

function tokenCasePostsData(url: URL) {
  const posts = tokenCasePostsFixture();
  const cursor = url.searchParams.get("cursor");
  const targetType = url.searchParams.get("target_type") ?? posts.query.target_type;
  const targetId = url.searchParams.get("target_id") ?? posts.query.target_id;
  const window = url.searchParams.get("window") ?? posts.query.window;
  const nextItem = {
    ...posts.items[0],
    event_id: "event-hansa-4",
    tweet_id: "tweet-hansa-4",
    handle: "marketdesk",
    author_handle: "marketdesk",
    text: "Follow-up page adds fresh HANSA context after the first dossier page.",
    url: "https://x.com/marketdesk/status/event-hansa-4",
  };
  const items = cursor ? [nextItem] : posts.items;
  return {
    ...posts,
    query: { ...posts.query, target_type: targetType, target_id: targetId, window },
    returned_count: items.length,
    total_count: posts.total_count + 1,
    has_more: false,
    next_cursor: null,
    items,
  };
}

function searchInspectData(url: URL) {
  const query = url.searchParams.get("q") ?? "";
  if (query.toLowerCase().includes("hansa")) {
    const dossier = tokenCaseData(url);
    return {
      query: {
        q: query,
        normalized_q: query.toLowerCase(),
        window: url.searchParams.get("window") ?? "24h",
        result_kind: "token_result",
      },
      resolver: {
        target_candidates: [dossier.target],
        selected_target: dossier.target,
        reasons: ["e2e_token_case_fixture"],
      },
      token_result: dossier,
      ambiguous_result: null,
      topic_result: null,
    };
  }
  return {
    query: {
      q: query,
      normalized_q: query.toLowerCase(),
      window: "24h",
      result_kind: "topic_result",
    },
    resolver: { target_candidates: [], selected_target: null, reasons: ["e2e"] },
    token_result: null,
    ambiguous_result: null,
    topic_result: null,
  };
}

function socialEventsByIds(url: URL) {
  const ids = (url.searchParams.get("ids") ?? "")
    .split(",")
    .map((id) => id.trim())
    .filter(Boolean);
  const byId = new Map(postsData().items.map((item) => [item.event_id, sourceEvent(item)]));
  return {
    events: ids.map((id) => byId.get(id)).filter(Boolean),
    not_found: ids.filter((id) => !byId.has(id)),
  };
}

function sourceEvent(item: ReturnType<typeof post>) {
  return {
    event_id: item.event_id,
    timestamp_ms: item.received_at_ms,
    source_provider: "gmgn",
    channel: "twitter_monitor_basic",
    action: item.reference?.type ?? "tweet",
    author_handle: item.author_handle,
    author_name: item.author_handle,
    author_followers: item.post_quality.score >= 80 ? 168_905 : 220,
    text_clean: item.text,
    canonical_url: item.url,
  };
}

function timelineData() {
  const posts = postsData().items.map((item, index) => ({
    ...item,
    bucket_start_ms: index < 2 ? NOW - 300_000 : NOW,
  }));
  return {
    query: { target_type: "Asset", target_id: TARGET_ID, window: "1h", bucket: "5m" },
    summary: {
      posts: 3,
      authors: 2,
      effective_authors: 1.8,
      first_seen_ms: NOW - 300_000,
      latest_seen_ms: NOW,
      phase: "expansion",
      top_author_share: 0.5,
      duplicate_text_share: 0,
      peak_posts_per_bucket: 2,
      peak_new_authors_per_bucket: 1,
      reproduction_rate: 1.5,
    },
    buckets: [
      {
        start_ms: NOW - 300_000,
        end_ms: NOW - 60_000,
        posts: 2,
        authors: 1,
        new_authors: 1,
        duplicate_text_share: 0,
        price: {
          status: "ready",
          provider: "okx_dex_price",
          pricefeed_id: `pricefeed:dex-token:gmgn_payload:eip155:1:${ADDRESS.toLowerCase()}`,
          price_usd: 0.00112,
          observed_at_ms: NOW - 60_000,
        },
        price_change_from_start_pct: 0.12,
      },
      {
        start_ms: NOW - 60_000,
        end_ms: NOW,
        posts: 1,
        authors: 1,
        new_authors: 1,
        duplicate_text_share: 0,
        price: {
          status: "ready",
          provider: "okx_dex_price",
          pricefeed_id: `pricefeed:dex-token:gmgn_payload:eip155:1:${ADDRESS.toLowerCase()}`,
          price_usd: 0.00114,
          observed_at_ms: NOW,
        },
        price_change_from_start_pct: 0.14,
      },
    ],
    market_candles: {
      target_type: "Asset",
      target_id: TARGET_ID,
      chain_id: "eip155:1",
      address: ADDRESS,
      symbol: "UPEG",
      pricefeed_id: `pricefeed:dex-token:gmgn_payload:eip155:1:${ADDRESS.toLowerCase()}`,
    },
    stages: [
      {
        stage_id: "seed:1777746000000:1",
        phase: "seed",
        start_ms: NOW - 300_000,
        end_ms: NOW - 300_000,
        duration_ms: 0,
        trigger_reason: "first_token_evidence",
        confidence: 0.61,
        people: {
          posts: 1,
          authors: 1,
          new_authors: 1,
          top_author_share: 1,
        },
        representative_event_ids: ["event-upeg-1"],
        price: {
          status: "ready",
          start_price: 0.001,
          end_price: 0.00112,
          delta_pct: 0.12,
          observation_ids: ["observation-upeg-1"],
          max_observation_lag_ms: 60_000,
        },
        risks: [],
      },
    ],
    authors: [
      {
        handle: "traderpow",
        first_seen_ms: NOW - 300_000,
        latest_seen_ms: NOW - 300_000,
        posts: 1,
        followers: 168_905,
        role: "seed",
        quality_score: 86,
      },
      {
        handle: "alien19710628",
        first_seen_ms: NOW - 60_000,
        latest_seen_ms: NOW,
        posts: 2,
        followers: 220,
        role: "amplifier",
        quality_score: 74,
      },
    ],
    posts,
    cascade: {
      edges: [
        {
          event_id: "event-upeg-2",
          parent_event_id: "event-upeg-1",
          parent_tweet_id: "tweet-upeg-1",
          edge_type: "quote",
          parent_author_handle: "traderpow",
          resolved: true,
        },
      ],
      unresolved_parents: [],
    },
    returned_count: posts.length,
    has_more: false,
    next_cursor: null,
  };
}

function postsData() {
  return {
    items: [
      post("event-upeg-1", "traderpow", "$UPEG source evidence", 86),
      post("event-upeg-2", "alien19710628", "$UPEG public follow-through", 74),
      post("event-upeg-3", "alien19710628", "$UPEG another public post", 68),
    ],
    returned_count: 3,
    total_count: 3,
    has_more: false,
    next_cursor: null,
    score_window: { window: "1h" },
    query: {
      target_type: "Asset",
      target_id: TARGET_ID,
      window: "1h",
      range: "current_window",
    },
  };
}

function post(eventId: string, handle: string, text: string, score: number) {
  const phase = eventId.endsWith("-1") ? "seed" : "ignition";
  return {
    event_id: eventId,
    tweet_id: eventId.replace("event", "tweet"),
    handle,
    author_handle: handle,
    received_at_ms: NOW,
    text,
    url: `https://x.com/${handle}/status/${eventId}`,
    mention_source: "gmgn_token_payload",
    target_type: "Asset",
    target_id: TARGET_ID,
    attribution_status: "direct",
    attribution_confidence: 1,
    attribution_weight: 1,
    event_type: phase === "seed" ? "seed_post" : "public_followup",
    reference:
      eventId === "event-upeg-2"
        ? { tweet_id: "tweet-upeg-1", author_handle: "traderpow", type: "quote" }
        : null,
    price: {
      status: "ready",
      provider: "okx_dex_price",
      pricefeed_id: `pricefeed:dex-token:gmgn_payload:eip155:1:${ADDRESS.toLowerCase()}`,
      price_usd: 0.00112,
      observed_at_ms: NOW,
    },
    stage_id: phase === "seed" ? "seed:1777746000000:1" : "ignition:1777746240000:1",
    stage_phase: phase,
    author_role: phase === "seed" ? "seed" : "early_amplifier",
    is_stage_representative: phase === "seed",
    price_delta_from_previous_post_pct: null,
    post_quality: {
      score_version: "post_quality_v1",
      score,
      reasons: ["structured_token_payload"],
      risks: [],
      contributions: [
        { feature: "source_specificity", value: 18, reason: "structured_token_payload" },
      ],
      risk_caps: [],
    },
  };
}

function stocksRadarData(url: URL) {
  return {
    window: url.searchParams.get("window") ?? "1h",
    query: {
      window: "1h",
      limit: 48,
      window_start_ms: NOW - 3_600_000,
      window_end_ms: NOW,
    },
    rows: [
      {
        target: {
          target_type: "MarketInstrument",
          target_id: "market_instrument:us_equity:AAPL",
          symbol: "AAPL",
          market: "us_equity",
          exchange: "NASDAQ",
          instrument_type: "equity",
          name: "Apple Inc.",
        },
        attention: { mentions: 3, unique_authors: 2, latest_seen_ms: NOW },
        latest_event: {
          event_id: "event-aapl",
          author_handle: "toly",
          text: "$AAPL breakout",
          received_at_ms: NOW,
        },
        quote: {
          status: "ready",
          price: 291.87,
          reference_close_price: 293.25,
          change_pct: -0.004,
          asof: "2026-05-12T08:45:45+00:00",
          provider: "yahoo",
          provider_symbol: "AAPL",
          latency_class: "delayed_15m",
          freshness_class: "delayed_15m",
          error: null,
        },
        source_event_ids: ["event-aapl"],
        row_health: [],
      },
    ],
    health: { returned_count: 1, quote_ready_count: 1, quote_unavailable_count: 0 },
  };
}
