import {
  compactNumber,
  eventHandle,
  formatPercentShare,
  formatRelativeAge,
  formatPropagationPhase,
  formatRelativeTime,
  formatRisk,
  formatScoreDelta,
  formatSignedPercent,
  formatTokenPriceUsd,
  formatTimingStatus,
  formatUtcTimestamp,
  formatUsdCompact,
  tokenLabel,
} from "@lib/format";
import type { TokenFlowItem } from "@lib/types";
import { tokenMarketBlockFixture } from "@tests/fixtures/marketFixtures";
import { describe, expect, it } from "vitest";

describe("format helpers", () => {
  it("compacts large numbers for dense cockpit cells", () => {
    expect(compactNumber(1250)).toBe("1.3K");
    expect(compactNumber(1_250_000)).toBe("1.3M");
  });

  it("formats relative milliseconds without locale noise", () => {
    expect(formatRelativeTime(1_000, 31_000)).toBe("30s");
    expect(formatRelativeTime(1_000, 181_000)).toBe("3m");
  });

  it("formats absolute UTC timestamps", () => {
    expect(formatUtcTimestamp(1778726642689)).toBe("2026-05-14 02:44 UTC");
    expect(formatUtcTimestamp(1778726642689, { suffix: false })).toBe("2026-05-14 02:44");
    expect(formatUtcTimestamp(null)).toBe("-");
    expect(formatUtcTimestamp(Number.NaN)).toBe("-");
  });

  it("formats relative age labels", () => {
    const now = 1778726642689;
    expect(formatRelativeAge(now - 59 * 60_000, now)).toBe("(59m ago)");
    expect(formatRelativeAge(now - 2 * 3_600_000, now)).toBe("(2h ago)");
    expect(formatRelativeAge(now - 10_000, now)).toBe("(just now)");
    expect(formatRelativeAge(now + 4 * 60_000, now)).toBe("(in 4m)");
    expect(formatRelativeAge(null, now)).toBe("");
  });

  it("formats normalized mindshare as a compact percent", () => {
    expect(formatPercentShare(0.5)).toBe("50%");
    expect(formatPercentShare(0.0123)).toBe("1.2%");
  });

  it("formats market cap and signed price changes for radar cells", () => {
    expect(formatUsdCompact(15_200)).toBe("$15K");
    expect(formatSignedPercent(0.124)).toBe("+12%");
    expect(formatSignedPercent(-0.084)).toBe("-8.4%");
    expect(formatSignedPercent(null)).toBe("-");
  });

  it("formats token prices without rounding away tradable decimals", () => {
    expect(formatTokenPriceUsd(2.753)).toBe("$2.75");
    expect(formatTokenPriceUsd(0.24668459858168806)).toBe("$0.2467");
    expect(formatTokenPriceUsd(0.00001360704303591779)).toBe("$0.00001361");
    expect(formatTokenPriceUsd(0.0000000006522)).toBe("$6.52e-10");
    expect(formatTokenPriceUsd(null)).toBe("-");
  });

  it("normalizes event handles and token labels", () => {
    expect(eventHandle({ event_id: "1", author: { handle: "@Toly" } })).toBe("toly");
    expect(tokenLabel(sampleToken())).toBe("$PEPE");
  });

  it("formats social heat rebuild labels", () => {
    expect(formatTimingStatus("neutral")).toBe("中性");
    expect(formatTimingStatus("chase_risk")).toBe("追高风险");
    expect(formatPropagationPhase("expansion")).toBe("扩散");
    expect(formatRisk("author_concentration_high")).toBe("作者集中");
    expect(formatScoreDelta(11)).toBe("+11");
  });
});

function sampleToken(): TokenFlowItem {
  return {
    identity: {
      identity_key: "asset:eip155:1:erc20:0x6982508145454ce325ddbe47a25d4ec3d2311933",
      identity_status: "EXACT",
      target_type: "Asset",
      target_id: "asset:eip155:1:erc20:0x6982508145454ce325ddbe47a25d4ec3d2311933",
      asset_id: "asset:eip155:1:erc20:0x6982508145454ce325ddbe47a25d4ec3d2311933",
      chain: "eip155:1",
      address: "0x6982508145454Ce325dDbE47a25d4ec3d2311933",
      symbol: "PEPE",
    },
    market: tokenMarketBlockFixture({
      market_status: "fresh",
      price_change_status: "insufficient_history",
    }),
    flow: {
      window: "5m",
      mentions: 1,
      previous_mentions: 0,
      mention_delta: 1,
      stream_dominance: 1,
      baseline_status: "insufficient_history",
      baseline_sample_count: 0,
    },
    social_heat: {
      score_version: "token_factor_snapshot_v5_provider_neutral:social_heat",
      score: 50,
      reasons: [],
      risks: [],
      contributions: [],
      risk_caps: [],
      window: "5m",
      mentions: 1,
      mentions_5m: 1,
      mentions_1h: 1,
      mentions_4h: 1,
      mentions_24h: 1,
      weighted_mentions: 1,
      previous_mentions: 0,
      mention_delta: 1,
      stream_share: 1,
      status: "new_burst",
    },
    discussion_quality: {
      score_version: "token_factor_snapshot_v5_provider_neutral:discussion_quality",
      score: 50,
      reasons: [],
      risks: [],
      contributions: [],
      risk_caps: [],
      evidence_specificity: 1,
      avg_post_quality: 50,
      avg_attribution_confidence: 1,
      duplicate_text_share: 0,
      informative_post_count: 1,
    },
    propagation: {
      score_version: "token_factor_snapshot_v5_provider_neutral:propagation",
      score: 50,
      reasons: [],
      risks: [],
      contributions: [],
      risk_caps: [],
      independent_authors: 1,
      effective_authors: 1,
      new_authors: 1,
      top_author_share: 1,
      duplicate_text_share: 0,
      author_entropy: 0,
      phase: "seed",
      top_authors: [],
    },
    tradeability: {
      score_version: "token_factor_snapshot_v5_provider_neutral:gates",
      score: 50,
      reasons: [],
      risks: [],
      contributions: [],
      risk_caps: [],
      identity_tradeable: true,
      market_fresh: true,
      market_cap_present: false,
      liquidity_present: false,
      pool_present: false,
    },
    timing: {
      score_version: "token_factor_snapshot_v5_provider_neutral:timing",
      score: 50,
      status: "neutral",
      chase_risk: false,
      reasons: [],
      risks: [],
    },
    opportunity: {
      score_version: "token_factor_snapshot_v5_provider_neutral:composite",
      score: 50,
      decision: "watch",
      reasons: [],
      risks: [],
      contributions: [],
      risk_caps: [],
      components: {
        heat: 50,
        propagation: 50,
        timing: 50,
      },
    },
    evidence_total_count: 0,
    posts_query: {
      target_type: "Asset",
      target_id: "asset:eip155:1:erc20:0x6982508145454ce325ddbe47a25d4ec3d2311933",
      window: "5m",
      range: "current_window",
    },
    timeline_query: {
      target_type: "Asset",
      target_id: "asset:eip155:1:erc20:0x6982508145454ce325ddbe47a25d4ec3d2311933",
      window: "5m",
    },
  };
}
