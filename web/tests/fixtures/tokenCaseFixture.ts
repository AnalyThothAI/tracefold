import type { TokenCaseDossier, TokenPostsData } from "@lib/types";

const BASE_MS = 1_777_746_300_000;

export function tokenCaseFixture(): TokenCaseDossier {
  return {
    target: {
      target_type: "Asset",
      target_id: "asset:solana:token:FhoxjfsuStvRQKRXSuB9ZDB7WRGjqhUPxa3NztWspump",
      symbol: "HANSA",
      chain_id: "solana",
      address: "FhoxjfsuStvRQKRXSuB9ZDB7WRGjqhUPxa3NztWspump",
      status: "resolved",
      source: "registry_assets",
      reason: "TARGET_ID",
    },
    timeline: {
      query: {
        target_type: "Asset",
        target_id: "asset:solana:token:FhoxjfsuStvRQKRXSuB9ZDB7WRGjqhUPxa3NztWspump",
        window: "1h",
        bucket: "5m",
      },
      summary: {
        posts: 18,
        authors: 9,
        effective_authors: 7,
        first_seen_ms: BASE_MS - 42 * 60_000,
        latest_seen_ms: BASE_MS - 90_000,
        phase: "expansion",
        top_author_share: 0.24,
        duplicate_text_share: 0.08,
        peak_posts_per_bucket: 6,
        peak_new_authors_per_bucket: 4,
        reproduction_rate: 1.7,
      },
      market_candles: {
        target_type: "Asset",
        target_id: "asset:solana:token:FhoxjfsuStvRQKRXSuB9ZDB7WRGjqhUPxa3NztWspump",
        chain_id: "solana",
        address: "FhoxjfsuStvRQKRXSuB9ZDB7WRGjqhUPxa3NztWspump",
        symbol: "HANSA",
        provider: "gmgn",
      },
      stages: [
        stage("stage-seed", "seed", BASE_MS - 42 * 60_000, 3, 2),
        stage("stage-ignition", "ignition", BASE_MS - 24 * 60_000, 7, 4),
        stage("stage-expansion", "expansion", BASE_MS - 9 * 60_000, 8, 5),
      ],
      buckets: [],
      authors: [
        {
          handle: "earlyape",
          first_seen_ms: BASE_MS - 42 * 60_000,
          latest_seen_ms: BASE_MS - 30 * 60_000,
          posts: 3,
          followers: 42_000,
          role: "seed",
          quality_score: 78,
        },
      ],
      posts: [],
      cascade: {
        edges: [],
        unresolved_parents: [],
      },
      returned_count: 3,
      has_more: false,
    },
    posts: tokenCasePostsFixture(),
    market_live: {
      status: "missing",
      target_type: "Asset",
      target_id: "asset:solana:token:FhoxjfsuStvRQKRXSuB9ZDB7WRGjqhUPxa3NztWspump",
      provider: null,
      price_usd: null,
      market_cap_usd: null,
      liquidity_usd: null,
      holders: null,
      observed_at_ms: null,
      error: "live snapshot unavailable in fixture",
    },
  };
}

export function tokenCasePostsFixture(): TokenPostsData {
  return {
    query: {
      target_type: "Asset",
      target_id: "asset:solana:token:FhoxjfsuStvRQKRXSuB9ZDB7WRGjqhUPxa3NztWspump",
      window: "1h",
      range: "current_window",
    },
    score_window: { window: "1h" },
    total_count: 18,
    returned_count: 3,
    has_more: true,
    next_cursor: "cursor-hansa-3",
    items: [
      post(
        "event-hansa-3",
        "solwatch",
        "Expansion leg forming on $HANSA. CA still needs market confirmation.",
        82,
      ),
      post(
        "event-hansa-2",
        "scannerjoe",
        "$HANSA scanner pickup. Watching independent follow-through.",
        74,
      ),
      post("event-hansa-1", "earlyape", "First $HANSA mention with contract evidence.", 91),
    ],
  };
}

function stage(stageId: string, phase: string, startMs: number, posts: number, authors: number) {
  return {
    stage_id: stageId,
    phase,
    start_ms: startMs,
    end_ms: startMs + 10 * 60_000,
    duration_ms: 10 * 60_000,
    trigger_reason: `${phase}_threshold`,
    confidence: 0.82,
    people: {
      posts,
      authors,
      new_authors: authors,
      top_author_share: 0.25,
    },
    representative_event_ids: [`event-hansa-${phase}`],
    price: {
      status: "missing",
      observation_ids: [],
    },
    risks: [],
  };
}

function post(eventId: string, handle: string, text: string, quality: number) {
  return {
    event_id: eventId,
    tweet_id: eventId.replace("event-", "tweet-"),
    author_handle: handle,
    text,
    url: `https://x.com/${handle}/status/${eventId}`,
    received_at_ms: BASE_MS - Number(eventId.slice(-1)) * 5 * 60_000,
    mention_source: "cashtag",
    target_type: "Asset",
    target_id: "asset:solana:token:FhoxjfsuStvRQKRXSuB9ZDB7WRGjqhUPxa3NztWspump",
    symbol: "HANSA",
    attribution_status: "ca_evidence",
    attribution_confidence: 0.91,
    attribution_weight: 1,
    event_type: "tweet",
    stage_phase: eventId.endsWith("3") ? "expansion" : eventId.endsWith("2") ? "ignition" : "seed",
    author_role: eventId.endsWith("3") ? "early_amplifier" : "scanner",
    is_stage_representative: true,
    post_quality: {
      score: quality,
      score_version: "pq_fixture_v1",
      reasons: ["ca_evidence", "specific thesis"],
      risks: [],
      contributions: [
        { feature: "ca_evidence", value: 0.34, reason: "contract address included" },
        { feature: "specificity", value: 0.28, reason: "non-generic token language" },
        { feature: "source_quality", value: 0.22, reason: "account has prior useful calls" },
      ],
      risk_caps: [],
    },
  };
}
