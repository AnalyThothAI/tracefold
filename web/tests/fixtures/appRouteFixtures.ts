import type { TokenRadarSnapshot } from "@features/live";
import type {
  OpenApiStatusData,
  SearchInspectData,
  TokenPostsData,
  TokenSocialTimelineData,
} from "@lib/types";

const NOW = 1_777_770_000_000;
const RADAR_ADDRESS = "0x6982508145454Ce325dDbE47a25d4ec3d2311933";
const RADAR_TARGET_ID = `asset:dex:eth:${RADAR_ADDRESS.toLowerCase()}`;

export function appStatusFixture(overrides: Partial<OpenApiStatusData> = {}): OpenApiStatusData {
  return {
    measured_at_ms: NOW,
    runtime: {
      ok: true,
      reasons: [],
      db: {
        ok: true,
        schema_ok: true,
        current_revision: "20260811_0254",
        expected_revision: "20260811_0254",
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

export function tokenRadarFixture(overrides: Partial<TokenRadarSnapshot> = {}): TokenRadarSnapshot {
  return {
    schema_version: "token_radar_snapshot_v3",
    state: "current",
    stale_reason: null,
    state_changed_at_ms: NOW - 120_000,
    social_evidence_as_of_ms: NOW,
    eligible_total: 1,
    items: [tokenRadarItemFixture()],
    ...overrides,
  };
}

export function tokenRadarItemFixture() {
  return {
    target: {
      target_type: "Asset" as const,
      target_id: RADAR_TARGET_ID,
      symbol: "UPEG",
      name: "Unpegged Token",
      logo_url: `/api/token-images/${"a".repeat(64)}`,
      chain: "eip155:1",
      exchange: null,
      address: RADAR_ADDRESS,
    },
    trigger_event_id: "event-upeg-1",
    trigger_source_event_at_ms: NOW - 60_000,
    qualified_at_ms: NOW - 45_000,
    why_now: { current_mentions: 7, prior_mentions: 2, mention_delta: 5 },
    evidence: {
      independent_author_count: 4,
      independent_text_count: 5,
      time_to_nth_author_ms: 90_000,
      duplicate_share: 0.08,
    },
    market: {
      price_usd: 0.042,
      price_observed_at_ms: NOW - 30_000,
      price_change_since_signal: 0.12,
      market_cap_usd: 42_000_000,
      market_cap_observed_at_ms: NOW - 40_000,
    },
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
