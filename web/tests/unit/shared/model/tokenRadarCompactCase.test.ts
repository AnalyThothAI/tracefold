import type { TokenFlowItem } from "@lib/types";
import { buildTokenRadarCompactCase } from "@shared/model/tokenRadarCompactCase";
import { describe, expect, it } from "vitest";

const TOKEN_IMAGE_URL = "/api/token-images/hansa-local";

describe("buildTokenRadarCompactCase", () => {
  it("renders the transparent propagation score and source facts", () => {
    const view = buildTokenRadarCompactCase(tokenFlowFixture());

    expect(view.propagation.value).toBe("74 / 100");
    expect(view.propagation.detail).toBe("12 informative · 8% duplicate");
    expect(view.propagation.tone).toBe("health");
  });

  it("surfaces propagation risk directly", () => {
    const view = buildTokenRadarCompactCase({
      ...tokenFlowFixture(),
      propagation: {
        ...tokenFlowFixture().propagation,
        risks: ["duplicate_text_share_high"],
      },
    });

    expect(view.propagation.tone).toBe("warn");
  });

  it("keeps local mirrored logo URLs", () => {
    const view = buildTokenRadarCompactCase({
      ...tokenFlowFixture(),
      profile: {
        status: "ready",
        identity: {
          logo_url: TOKEN_IMAGE_URL,
        },
      },
    });

    expect(view.logoUrl).toBe(TOKEN_IMAGE_URL);
  });
});

function tokenFlowFixture(): TokenFlowItem {
  return {
    identity: {
      identity_key: "asset:dex:sol:hansa",
      identity_status: "EXACT",
      target_type: "Asset",
      target_id: "asset:dex:sol:hansa",
      asset_type: "Asset",
      venue_type: "dex",
      exchange: "gmgn",
      chain: "solana",
      address: "FhoxjfsuStvRQKRXSuB9ZDB7WRGjqhUPxa3NztWspump",
      symbol: "HANSA",
      resolution_reasons: [],
    },
    profile: { status: "missing" },
    market: {
      event_anchor: null,
      decision_latest: null,
      readiness: {
        anchor_status: "ready",
        latest_status: "live",
        dex_floor_status: "ready",
        missing_fields: [],
        stale_fields: [],
      },
      market_status: "fresh",
      price: 0.001,
      price_status: "fresh",
      market_cap: 5_100_000,
      market_cap_status: "fresh",
      liquidity: 180_000,
      liquidity_status: "fresh",
      holder_count: 12_000,
      holder_count_status: "fresh",
      price_change_since_social_pct: 0.14,
      price_change_status: "ready",
    },
    flow: {
      window: "1h",
      mentions: 21,
      previous_mentions: 4,
      mention_delta: 17,
      stream_dominance: 0.004,
      baseline_status: "ready",
      baseline_sample_count: 21,
    },
    social_heat: {
      score_version: "fixture",
      score: 86,
      reasons: ["z_score_above_3"],
      risks: [],
      contributions: [],
      risk_caps: [],
      window: "1h",
      mentions: 21,
      mentions_5m: 2,
      mentions_1h: 21,
      mentions_4h: 21,
      mentions_24h: 21,
      weighted_mentions: 21,
      previous_mentions: 4,
      mention_delta: 17,
      stream_share: 0.004,
      status: "new_burst",
    },
    discussion_quality: {
      score_version: "fixture",
      score: 72,
      reasons: ["informative_discussion"],
      risks: [],
      contributions: [],
      risk_caps: [],
      evidence_specificity: 0.9,
      avg_post_quality: 72,
      avg_attribution_confidence: 0.91,
      duplicate_text_share: 0.08,
      informative_post_count: 12,
    },
    propagation: {
      score_version: "fixture",
      score: 74,
      reasons: ["independent_expansion"],
      risks: [],
      contributions: [],
      risk_caps: [],
      independent_authors: 9,
      effective_authors: 8,
      new_authors: 7,
      top_author_share: 0.22,
      duplicate_text_share: 0.08,
      author_entropy: 0.8,
      phase: "expansion",
      top_authors: [],
    },
    tradeability: {
      score_version: "fixture",
      score: 100,
      reasons: [],
      risks: [],
      contributions: [],
      risk_caps: [],
      identity_tradeable: true,
      market_fresh: true,
      market_cap_present: true,
      liquidity_present: true,
      pool_present: true,
    },
    timing: {
      score_version: "fixture",
      score: 88,
      status: "neutral",
      chase_risk: false,
      reasons: [],
      risks: [],
      price_change_since_social_pct: 0.14,
      market_observation_status: "ready",
    },
    opportunity: {
      score_version: "fixture",
      score: 83,
      decision: "driver",
      reasons: [],
      risks: [],
      contributions: [],
      risk_caps: [],
      components: { heat: 86, propagation: 74, timing: 88 },
    },
    evidence_total_count: 21,
    posts_query: { window: "1h", range: "current_window" },
    timeline_query: { window: "1h" },
  };
}
