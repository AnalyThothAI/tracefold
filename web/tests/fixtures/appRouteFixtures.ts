import type {
  AssetFlowData,
  AssetFlowRow,
  OpenApiStatusData,
  SearchInspectData,
  TokenPostsData,
  TokenSocialTimelineData,
} from "@lib/types";

import { marketContextFixture, marketObservationFixture } from "./marketFixtures";

const NOW = 1_777_770_000_000;
const RADAR_ADDRESS = "0x6982508145454Ce325dDbE47a25d4ec3d2311933";
const RADAR_TARGET_ID = `asset:dex:eth:${RADAR_ADDRESS.toLowerCase()}`;

export function appStatusFixture(overrides: Partial<OpenApiStatusData> = {}): OpenApiStatusData {
  return {
    ok: true,
    reasons: [],
    store: "postgresql",
    snapshot_gate: {},
    db: { ok: true },
    news: {
      status: "ready",
      reasons: [],
      layers: {
        ingest: {
          status: "ready",
          enabled_sources: 118,
          recent_success_sources: 118,
          failing_sources: 0,
          last_success_at_ms: NOW,
        },
        story: {
          status: "ready",
          active_stories: 120,
          last_projected_at_ms: NOW,
          identity_version: "worldmonitor-story-identity-f73de5b7",
          classifier_version: "worldmonitor-threat-classifier-f73de5b7",
          importance_version: "worldmonitor-importance-f73de5b7",
        },
        brief: {
          status: "ready",
          public_state: "ready",
          publication_id: "news_brief_fixture",
          last_error: null,
        },
      },
      measured_at_ms: NOW,
    },
    provider_states: {},
    workers: {
      collector: workerStatusFixture({
        enabled: true,
        running: true,
      }),
      token_radar_projection: workerStatusFixture({
        enabled: true,
        running: true,
        last_started_at_ms: NOW,
        last_finished_at_ms: NOW,
        last_result: {
          processed: 0,
          failed: 0,
          dead: 0,
          skipped: 0,
          notes: { rows_written: 0, source_rows: 0 },
        },
      }),
      market_tick_stream: workerStatusFixture({ enabled: false, running: false }),
      market_tick_poll: workerStatusFixture({
        enabled: true,
        running: true,
        last_started_at_ms: NOW,
      }),
      asset_profile_refresh: workerStatusFixture(),
      resolution_refresh: workerStatusFixture(),
    },
    ...overrides,
  };
}

function workerStatusFixture(overrides: Partial<OpenApiStatusData["workers"][string]> = {}) {
  const enabled = overrides.enabled ?? false;
  const running = overrides.running ?? false;
  return {
    enabled,
    running,
    effective_status:
      overrides.effective_status ?? (!enabled ? "disabled" : running ? "running" : "stopped"),
    unavailable_reason: null,
    last_started_at_ms: null,
    last_finished_at_ms: null,
    last_result: null,
    last_error: null,
    iteration_duration_p99_ms: null,
    ...overrides,
  };
}

export function tokenRadarFixture(overrides: Partial<AssetFlowData> = {}): AssetFlowData {
  return {
    window: "1h",
    venue: "all",
    targets: [],
    attention: [],
    projection: {
      status: "fresh",
      version: "token-radar-route-fixture",
      source: "token_radar_current_rows",
      venue: "all",
      reason: null,
      latest_attempt_status: "ready",
      row_count: 0,
      source_rows: 0,
      source_max_received_at_ms: 0,
      source_frontier_ms: null,
      computed_at_ms: NOW,
      error: null,
      anchor_coverage: { status: "fresh", ready: 0, missing: 0, total: 0 },
      quality_status: "ready",
      degraded_reasons: [],
      unresolved: {
        identity_missing_count: 0,
        nil_count: 0,
        ambiguous_count: 0,
        sample_symbols: [],
      },
    },
    ...overrides,
  };
}

export function tokenRadarRowFixture(): AssetFlowRow {
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
      target_id: RADAR_TARGET_ID,
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
      target_id: RADAR_TARGET_ID,
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
      intent_id: `intent:${RADAR_TARGET_ID}`,
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
      target_id: RADAR_TARGET_ID,
      pricefeed_id: null,
      reason_codes: ["CHAIN_ADDRESS_EXACT"],
      candidate_ids: [RADAR_TARGET_ID],
      lookup_keys: [],
      discovery: [],
    },
    factor_snapshot: radarFactorSnapshot(attention, market),
    quality: { status: "ready", degraded_reasons: [] },
  };
}

function radarFactorSnapshot(
  attention: Record<string, number | string | null>,
  market: ReturnType<typeof marketContextFixture>,
): AssetFlowRow["factor_snapshot"] {
  return {
    schema_version: "token_factor_snapshot_v5_provider_neutral",
    subject: {
      target_type: "Asset",
      target_id: RADAR_TARGET_ID,
      symbol: "UPEG",
      chain: "eip155:1",
      address: RADAR_ADDRESS,
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
      social_heat: radarFactorFamily(86, 0.55, {
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
      social_propagation: radarFactorFamily(72, 0.45, {
        mentions: attention.mentions_window,
        independent_authors: attention.unique_authors,
        duplicate_text_share: 0,
        informative_post_count: attention.mentions_window,
      }),
      timing_risk: radarFactorFamily(50, 0, {
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
  } as AssetFlowRow["factor_snapshot"];
}

function radarFactorFamily(score: number, weight: number, facts: Record<string, unknown>) {
  return {
    raw_score: score,
    score,
    weight,
    facts,
    factors: {
      primary: {
        family: "route_fixture",
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
