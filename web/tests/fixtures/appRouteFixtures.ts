import type {
  OpenApiStatusData,
  SearchInspectData,
  TokenPostsData,
  TokenSocialTimelineData,
} from "@lib/types";

const NOW = 1_777_770_000_000;

export function appStatusFixture(overrides: Partial<OpenApiStatusData> = {}): OpenApiStatusData {
  return {
    measured_at_ms: NOW,
    runtime: {
      ok: true,
      reasons: [],
      db: {
        ok: true,
        schema_ok: true,
        current_revision: "20260812_0255",
        expected_revision: "20260812_0255",
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
    providers: { status: "ok", reasons: [], items: [] },
    ...overrides,
  };
}

export function searchInspectFixture(
  overrides: Partial<SearchInspectData> = {},
): SearchInspectData {
  return {
    query: {
      q: "$RKC",
      normalized_q: "rkc",
      window: "24h",
      result_kind: "empty_result",
    },
    resolver: {
      target_candidates: [],
      selected_target: null,
      reasons: ["route_fixture_empty"],
    },
    token_result: null,
    topic_result: null,
    ambiguous_result: null,
    ...overrides,
  };
}

export function targetSocialTimelineFixture(
  overrides: Partial<TokenSocialTimelineData> = {},
): TokenSocialTimelineData {
  return {
    query: { window: "1h", bucket: "5m" },
    summary: {
      posts: 0,
      authors: 0,
      effective_authors: 0,
      phase: "seed",
      top_author_share: 0,
      latest_seen_ms: null,
    },
    market_candles: {
      price_series_type: "anchor_line",
      candle_status: "missing_market_id",
      candle_bar: "1H",
    },
    stages: [],
    buckets: [],
    authors: [],
    posts: [],
    cascade: { edges: [], unresolved_parents: [] },
    ...overrides,
  } as TokenSocialTimelineData;
}

export function targetPostsFixture(overrides: Partial<TokenPostsData> = {}): TokenPostsData {
  return {
    items: [],
    returned_count: 0,
    total_count: 0,
    has_more: false,
    score_window: { window: "1h" },
    query: {
      target_type: null,
      target_id: null,
      window: "1h",
      range: "current_window",
    },
    ...overrides,
  } as TokenPostsData;
}
