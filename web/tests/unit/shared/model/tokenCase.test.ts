import type { TokenFlowItem } from "@lib/types";
import { buildTokenCaseView } from "@shared/model/tokenCase";
import { describe, expect, it } from "vitest";

describe("buildTokenCaseView", () => {
  it("keeps official, community, market, and decision facts in one token case", () => {
    const view = buildTokenCaseView(tokenFixture());

    expect(view.key).toBe("asset:dex:eth:0x1111111111111111111111111111111111111111");
    expect(view.identity.label).toBe("Identity");
    expect(view.identity.value).toBe("$ALPHA");
    expect(view.identity.detail).toBe("ETH · 0x111111...111111");
    expect(view.official.value).toBe("Alpha Protocol");
    expect(view.official.detail).toContain("alpha.io");
    expect(view.official.source).toBe("official");
    expect(view.community.value).toBe("4 posts · 3 authors");
    expect(view.community.detail).toContain("top share");
    expect(view.market.value).toBe("$51M");
    expect(view.market.detail).toContain("+13%");
    expect(view.decision.value).toBe("driver");
    expect(view.decision.tone).toBe("opportunity");
    expect(view.actions.searchLabel).toBe("Search Intel");
    expect(view.evidence).toEqual(["resolved direct evidence", "independent expansion"]);
    expect(view).not.toHaveProperty("narrative");
  });

  it("labels missing official facts as unavailable without invented prose", () => {
    const view = buildTokenCaseView({
      ...tokenFixture(),
      profile: { status: "missing", source: { provider: "gmgn" } },
    });

    expect(view.official.value).toBe("Official profile unavailable");
    expect(view.official.detail).toBe("profile missing · gmgn");
    expect(view.official.source).toBe("official");
  });
});

function tokenFixture(): TokenFlowItem {
  return {
    identity: {
      identity_key: "asset:dex:eth:0x1111111111111111111111111111111111111111",
      identity_status: "EXACT",
      target_type: "Asset",
      target_id: "asset:dex:eth:0x1111111111111111111111111111111111111111",
      asset_id: "asset:dex:eth:0x1111111111111111111111111111111111111111",
      asset_type: "Asset",
      venue_type: "dex",
      exchange: "gmgn",
      chain: "eth",
      address: "0x1111111111111111111111111111111111111111",
      symbol: "ALPHA",
      resolution_reasons: [],
    },
    profile: {
      status: "ready",
      provider: "gmgn",
      identity: {
        symbol: "ALPHA",
        name: "Alpha Protocol",
        description: "Official token profile from the persisted profile read model.",
      },
      links: {
        website_url: "https://alpha.io",
        twitter_username: "alpha",
      },
      source: {
        provider: "gmgn",
        raw_available: true,
      },
    },
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
      price: 0.104,
      price_status: "fresh",
      market_cap: 51_000_000,
      market_cap_status: "fresh",
      liquidity: 3_000_000,
      liquidity_status: "fresh",
      pool_status: "ready",
      price_change_since_social_pct: 0.13,
      price_change_status: "ready",
    },
    flow: {
      window: "1h",
      mentions: 4,
      previous_mentions: 0,
      mention_delta: 4,
      stream_dominance: 0.004,
      baseline_status: "ready",
      baseline_sample_count: 21,
    },
    social_heat: {
      score_version: "token_factor_snapshot_v5_provider_neutral:social_heat",
      score: 86,
      reasons: ["z_score_above_3"],
      risks: [],
      contributions: [],
      risk_caps: [],
      window: "1h",
      mentions: 4,
      mentions_5m: 1,
      mentions_1h: 4,
      mentions_4h: 4,
      mentions_24h: 4,
      weighted_mentions: 4,
      previous_mentions: 0,
      mention_delta: 4,
      stream_share: 0.004,
      status: "new_burst",
    },
    discussion_quality: {
      score_version: "token_factor_snapshot_v5_provider_neutral:discussion_quality",
      score: 78,
      reasons: ["informative_discussion"],
      risks: [],
      contributions: [],
      risk_caps: [],
      evidence_specificity: 0.92,
      avg_post_quality: 78,
      avg_attribution_confidence: 0.91,
      duplicate_text_share: 0,
      informative_post_count: 3,
    },
    propagation: {
      score_version: "token_factor_snapshot_v5_provider_neutral:propagation",
      score: 74,
      reasons: ["independent_expansion"],
      risks: [],
      contributions: [],
      risk_caps: [],
      independent_authors: 3,
      effective_authors: 3,
      new_authors: 3,
      top_author_share: 0.33,
      duplicate_text_share: 0,
      author_entropy: 0.8,
      phase: "expansion",
      top_authors: [
        { handle: "alpha_founder", posts: 1 },
        { handle: "traderpow", posts: 1 },
      ],
    },
    tradeability: {
      score_version: "token_factor_snapshot_v5_provider_neutral:gates",
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
      score_version: "token_factor_snapshot_v5_provider_neutral:timing",
      score: 88,
      status: "neutral",
      chase_risk: false,
      reasons: ["fresh_market"],
      risks: [],
      price_change_since_social_pct: 0.13,
      market_observation_status: "ready",
    },
    opportunity: {
      score_version: "token_factor_snapshot_v5_provider_neutral:composite",
      score: 83,
      decision: "driver",
      reasons: ["resolved_direct_evidence", "independent_expansion"],
      risks: [],
      contributions: [],
      risk_caps: [],
      components: { heat: 86, propagation: 74, timing: 88 },
    },
    evidence_total_count: 4,
    posts_query: {
      target_type: "Asset",
      target_id: "asset:dex:eth:0x1111111111111111111111111111111111111111",
      window: "1h",
      range: "current_window",
    },
    timeline_query: {
      target_type: "Asset",
      target_id: "asset:dex:eth:0x1111111111111111111111111111111111111111",
      window: "1h",
    },
  };
}
