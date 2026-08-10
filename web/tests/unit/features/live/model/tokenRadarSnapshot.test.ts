import {
  parseTokenRadarSnapshot,
  TOKEN_RADAR_SNAPSHOT_SCHEMA,
} from "@features/live/model/tokenRadarSnapshot";
import { describe, expect, it } from "vitest";

describe("parseTokenRadarSnapshot", () => {
  it("accepts the exact v1 snapshot without changing server priority", () => {
    const value = snapshot();
    expect(parseTokenRadarSnapshot(value)).toEqual(value);
    expect(parseTokenRadarSnapshot(value).items.map((item) => item.target.symbol)).toEqual([
      "FIRST",
      "SECOND",
    ]);
  });

  it.each([
    ["wrong schema", () => ({ ...snapshot(), schema_version: "legacy" })],
    ["extra root field", () => ({ ...snapshot(), legacy: true })],
    [
      "more than eight rows",
      () => ({ ...snapshot(), items: Array.from({ length: 9 }, () => snapshot().items[0]) }),
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

function snapshot() {
  return {
    schema_version: TOKEN_RADAR_SNAPSHOT_SCHEMA,
    evidence_as_of_ms: 1_778_426_440_000,
    eligible_total: 2,
    items: [item("FIRST", "one"), item("SECOND", "two")],
  };
}

function item(symbol: string, id: string) {
  return {
    target: {
      target_type: "Asset" as const,
      target_id: `asset:solana:token:${id}`,
      symbol,
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
    market: { status: "confirmed" as const, price_change_since_signal: 0.12 },
    counter_evidence: null,
  };
}
