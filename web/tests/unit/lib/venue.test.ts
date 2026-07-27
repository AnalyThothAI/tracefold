import type { TokenFlowItem } from "@lib/types";
import { tokenVenueAction } from "@lib/venue";
import { tokenMarketBlockFixture } from "@tests/fixtures/marketFixtures";
import { describe, expect, it } from "vitest";

describe("venue links", () => {
  it("opens OKX spot instruments for CEX assets", () => {
    expect(
      tokenVenueAction(
        token({ venueType: "cex", exchange: "okx", instId: "BTC-USDT", instType: "SPOT" }),
      ),
    ).toEqual({
      label: "OKX",
      url: "https://www.okx.com/trade-spot/btc-usdt",
    });
  });

  it("opens OKX swap instruments for CEX perpetuals", () => {
    expect(
      tokenVenueAction(
        token({ venueType: "cex", exchange: "okx", instId: "TAO-USDT-SWAP", instType: "SWAP" }),
      ),
    ).toEqual({
      label: "OKX",
      url: "https://www.okx.com/trade-swap/tao-usdt-swap",
    });
  });

  it("opens OKX CEX actions from pricefeed ids and provider aliases", () => {
    expect(
      tokenVenueAction(
        token({
          venueType: "cex",
          exchange: "okx_cex",
          instId: "pricefeed:cex:okx:spot:BTC-USDT",
          instType: "cex_spot",
        }),
      ),
    ).toEqual({
      label: "OKX",
      url: "https://www.okx.com/trade-spot/btc-usdt",
    });
  });

  it("keeps GMGN links for DEX assets", () => {
    expect(
      tokenVenueAction(
        token({
          venueType: "dex",
          chain: "solana",
          address: "So11111111111111111111111111111111111111112",
        }),
      ),
    ).toEqual({
      label: "GMGN",
      url: "https://gmgn.ai/sol/token/So11111111111111111111111111111111111111112",
    });
  });
});

function token(options: {
  venueType: "cex" | "dex";
  exchange?: string | null;
  instId?: string | null;
  instType?: string | null;
  chain?: string | null;
  address?: string | null;
}): TokenFlowItem {
  return {
    identity: {
      identity_key: "asset:test",
      identity_status: "resolved",
      venue_type: options.venueType,
      exchange: options.exchange ?? null,
      inst_id: options.instId ?? null,
      inst_type: options.instType ?? null,
      chain: options.chain ?? null,
      address: options.address ?? null,
      symbol: "BTC",
    },
    market: tokenMarketBlockFixture({
      event_anchor: null,
      decision_latest: null,
      market_status: "missing",
      price_change_status: "missing_market",
    }),
    flow: {
      window: "1h",
      mentions: 1,
      previous_mentions: 0,
      mention_delta: 1,
      baseline_status: "insufficient_history",
      baseline_sample_count: 0,
    },
    social_heat: scoreBlock(),
    discussion_quality: scoreBlock(),
    propagation: {
      ...scoreBlock(),
      independent_authors: 1,
      effective_authors: 1,
      new_authors: 1,
      top_author_share: 1,
      duplicate_text_share: 0,
      author_entropy: 0,
      reproduction_rate: null,
      phase: "seed",
      top_authors: [],
    },
    tradeability: {
      ...scoreBlock(),
      identity_tradeable: true,
      market_fresh: false,
      market_cap_present: false,
      liquidity_present: false,
      pool_present: options.venueType === "dex",
      hard_risks: [],
    },
    timing: {
      ...scoreBlock(),
      status: "market_unavailable",
      social_signal_start_ms: null,
      price_change_since_social_pct: null,
      price_change_before_social_pct: null,
      market_observation_status: "provider_not_found",
      chase_risk: false,
    },
    opportunity: {
      ...scoreBlock(),
      decision: "watch",
      decision_priority: 2,
      hard_risks: [],
      components: { heat: 0, propagation: 0, timing: 0 },
    },
    watch: { status: "public_only", direct_mentions: 0, direct_authors: 0 },
    evidence_total_count: 0,
    posts_query: { window: "1h", range: "current_window" },
    timeline_query: { window: "1h" },
  } as unknown as TokenFlowItem;
}

function scoreBlock() {
  return {
    score: 0,
    score_version: "test",
    reasons: [],
    risks: [],
    contributions: [],
    risk_caps: [],
  };
}
