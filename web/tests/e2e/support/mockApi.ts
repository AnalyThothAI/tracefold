import type { Page, Route } from "@playwright/test";
import { macroModuleFixture, macroOverviewFixture } from "@tests/fixtures/macroFixture";
import {
  newsFeedFixture,
  newsGlobalBriefFixture,
  newsSourcesFixture,
  newsStatusFixture,
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
  radarItemCount?: number;
  radarPresentationStress?: boolean;
  radarRefreshItemCount?: number;
  radarUnsupportedChain?: boolean;
};

export async function installMockApi(page: Page, options: MockApiOptions = {}) {
  unhandledApiRequests.set(page, []);
  let radarRequestCount = 0;

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
    if (path === "/api/token-radar") {
      radarRequestCount += 1;
      const itemCount =
        radarRequestCount > 1 && options.radarRefreshItemCount !== undefined
          ? options.radarRefreshItemCount
          : (options.radarItemCount ?? 1);
      return fulfill(
        route,
        tokenRadarData(
          itemCount,
          options.radarPresentationStress ?? false,
          options.radarUnsupportedChain ?? false,
        ),
      );
    }
    if (path === "/api/token-case") return fulfill(route, tokenCaseData(url));
    if (path.startsWith("/api/token-images/")) return fulfillTokenImage(route);
    if (path === "/api/search/inspect") return fulfill(route, searchInspectData(url));
    if (path === "/api/events/by-ids") return fulfill(route, socialEventsByIds(url));
    if (path === "/api/target-social-timeline") return fulfill(route, timelineData());
    if (path === "/api/target-posts") return fulfill(route, targetPostsData(url));
    if (path === "/api/news/feed") return fulfill(route, newsFeedData());
    if (path === "/api/news/status") return fulfill(route, newsStatusFixture());
    if (path === "/api/news/sources") return fulfill(route, newsSourcesFixture());
    if (path.startsWith("/api/news/stories/")) return fulfill(route, newsStoryDetailData(path));
    if (path === "/api/news/brief") return fulfill(route, newsGlobalBriefFixture());
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
  return {
    ...newsFeedFixture(),
    stories: Array.from({ length: 5 }, (_, index) => ({
      ...story,
      story_id: index === 0 ? story.story_id : `story-global-policy-${index + 1}`,
      title: index === 0 ? story.title : `Global policy update ${index + 1}`,
    })),
  };
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
    measured_at_ms: NOW,
    runtime: {
      ok: true,
      reasons: [],
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
    },
    providers: {
      status: "ok",
      reasons: [],
      items: [],
    },
  };
}

function tokenRadarData(itemCount: number, presentationStress: boolean, unsupportedChain: boolean) {
  return {
    schema_version: "token_radar_snapshot_v2",
    evidence_as_of_ms: NOW,
    eligible_total: itemCount,
    items: Array.from({ length: itemCount }, (_, index) =>
      radarItem(index, presentationStress, unsupportedChain),
    ),
  };
}

function radarItem(index: number, presentationStress: boolean, unsupportedChain: boolean) {
  const suffix = index ? `:${index + 1}` : "";
  const stressFirstItem = presentationStress && index === 0;
  return {
    target: {
      target_type: "Asset",
      target_id: `${TARGET_ID}${suffix}`,
      symbol: index ? `CASE${index + 1}` : "UPEG",
      name: stressFirstItem
        ? "超长中文代币名称用于验证窄屏完整展示"
        : index
          ? `Case ${index + 1}`
          : "Unpegged Token",
      logo_url: `/api/token-images/${String(index + 1).padStart(64, "0")}`,
      chain: unsupportedChain && index === 0 ? "eip155:999999" : "eip155:1",
      exchange: null,
      address: index ? `${ADDRESS}${index + 1}` : ADDRESS,
    },
    trigger_event_id: index ? `event-upeg-${index + 1}` : "event-upeg-1",
    triggered_at_ms: NOW - (index + 1) * 60_000,
    why_now: {
      current_mentions: stressFirstItem ? 12_345 : 7 + index,
      prior_mentions: stressFirstItem ? 1_234 : 2,
      mention_delta: stressFirstItem ? 11_111 : 5 + index,
    },
    evidence: {
      new_independent_author_count: stressFirstItem ? 1_234 : 4,
      independent_text_count: stressFirstItem ? 5_678 : 5,
      time_to_nth_author_ms: 90_000,
      duplicate_share: 0.08,
    },
    market: {
      status: "confirmed",
      price_usd: stressFirstItem ? 0.000000000123456789 : 0.042,
      price_change_since_signal: 0.12,
      market_cap_usd: 42_000_000,
      observed_at_ms: NOW - 30_000,
    },
    counter_evidence: null,
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
  byId.set("event-upeg-1", {
    event_id: "event-upeg-1",
    timestamp_ms: NOW - 60_000,
    source_provider: "gmgn",
    channel: "twitter_monitor_basic",
    action: "tweet",
    author_handle: "radartrigger",
    author_name: "Radar Trigger",
    author_followers: 1_024,
    text_clean: "Independent authors accelerated around $UPEG.",
    canonical_url: "https://x.com/radartrigger/status/event-upeg-1",
  });
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
