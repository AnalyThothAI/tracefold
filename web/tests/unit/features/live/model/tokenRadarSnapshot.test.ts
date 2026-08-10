import {
  parseTokenRadarSnapshot,
  TOKEN_RADAR_SNAPSHOT_SCHEMA,
} from "@features/live/model/tokenRadarSnapshot";
import { describe, expect, it } from "vitest";

describe("parseTokenRadarSnapshot", () => {
  it("accepts the exact v2 snapshot and preserves all fifty server-prioritized rows", () => {
    const value = snapshot(50);
    expect(parseTokenRadarSnapshot(value)).toEqual(value);
    expect(parseTokenRadarSnapshot(value).items.map((item) => item.target.symbol)).toEqual(
      Array.from({ length: 50 }, (_, index) => `TOKEN${index + 1}`),
    );
  });

  it.each([
    ["wrong schema", () => ({ ...snapshot(), schema_version: "legacy" })],
    ["extra root field", () => ({ ...snapshot(), legacy: true })],
    [
      "more than fifty rows",
      () => ({ ...snapshot(), items: Array.from({ length: 51 }, () => item("EXTRA", "extra")) }),
    ],
    [
      "remote logo URL",
      () => {
        const value = snapshot();
        value.items[0].target.logo_url = "https://images.example/token.png";
        return value;
      },
    ],
    [
      "malformed local logo path",
      () => {
        const value = snapshot();
        value.items[0].target.logo_url = "/api/token-images/not-a-content-hash";
        return value;
      },
    ],
    [
      "nonpositive current price",
      () => {
        const value = snapshot();
        value.items[0].market.price_usd = 0;
        return value;
      },
    ],
    [
      "market metrics without observation time",
      () => {
        const value = snapshot();
        (value.items[0].market as { observed_at_ms: number | null }).observed_at_ms = null;
        return value;
      },
    ],
    [
      "market observation after the snapshot evidence clock",
      () => {
        const value = snapshot();
        value.items[0].market.observed_at_ms = value.evidence_as_of_ms + 1;
        return value;
      },
    ],
    [
      "invalid duplicate share",
      () => {
        const value = snapshot();
        value.items[0].evidence.duplicate_share = 1.1;
        return value;
      },
    ],
    [
      "inconsistent mention delta",
      () => {
        const value = snapshot();
        value.items[0].why_now.mention_delta = 4;
        return value;
      },
    ],
    [
      "confirmed market without a change",
      () => {
        const value = snapshot();
        (
          value.items[0].market as {
            status: "confirmed";
            price_change_since_signal: number | null;
          }
        ).price_change_since_signal = null;
        return value;
      },
    ],
    [
      "unknown counter evidence",
      () => {
        const value = snapshot();
        (value.items[0] as { counter_evidence: unknown }).counter_evidence = "generic_fallback";
        return value;
      },
    ],
  ])("rejects %s", (_name, makeValue) => {
    expect(() => parseTokenRadarSnapshot(makeValue())).toThrow(/token_radar_snapshot_contract/);
  });
});

function snapshot(count = 2) {
  return {
    schema_version: TOKEN_RADAR_SNAPSHOT_SCHEMA,
    evidence_as_of_ms: 1_778_426_440_000,
    eligible_total: count,
    items: Array.from({ length: count }, (_, index) =>
      item(`TOKEN${index + 1}`, `token-${index + 1}`),
    ),
  };
}

function item(symbol: string, id: string) {
  return {
    target: {
      target_type: "Asset" as const,
      target_id: `asset:solana:token:${id}`,
      symbol,
      name: `${symbol} Network`,
      logo_url: `/api/token-images/${"a".repeat(64)}`,
      chain: "solana",
      exchange: null,
      address: id,
    },
    trigger_event_id: `event-${id}`,
    triggered_at_ms: 1_778_426_430_000,
    why_now: { current_mentions: 7, prior_mentions: 2, mention_delta: 5 },
    evidence: {
      new_independent_author_count: 4,
      independent_text_count: 5,
      time_to_nth_author_ms: 90_000,
      duplicate_share: 0.1,
    },
    market: {
      status: "confirmed" as const,
      price_usd: 0.00003281,
      price_change_since_signal: 0.12,
      market_cap_usd: 1_250_000,
      observed_at_ms: 1_778_426_435_000,
    },
    counter_evidence: null,
  };
}
