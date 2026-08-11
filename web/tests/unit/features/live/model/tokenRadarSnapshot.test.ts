import {
  parseTokenRadarSnapshot,
  TOKEN_RADAR_SNAPSHOT_SCHEMA,
} from "@features/live/model/tokenRadarSnapshot";
import { describe, expect, it } from "vitest";

describe("parseTokenRadarSnapshot", () => {
  it("accepts one exact v3 current snapshot with causal and independent market clocks", () => {
    const value = v3Snapshot();

    expect(parseTokenRadarSnapshot(value)).toEqual(value);
  });

  it("enforces the server-owned state and stale-reason combinations", () => {
    const stale = {
      ...v3Snapshot(),
      state: "stale",
      stale_reason: "source_unavailable",
    } as const;
    const currentWithReason = {
      ...v3Snapshot(),
      stale_reason: "projection_failed",
    } as const;

    expect(parseTokenRadarSnapshot(stale)).toEqual(stale);
    expect(() => parseTokenRadarSnapshot(currentWithReason)).toThrow(
      /token_radar_snapshot_contract:snapshot.stale_reason/,
    );
  });

  it("requires the public rows to exactly represent the capped eligible total", () => {
    const underfilled = { ...v3Snapshot(), eligible_total: 2 };

    expect(() => parseTokenRadarSnapshot(underfilled)).toThrow(
      /token_radar_snapshot_contract:snapshot.eligible_total/,
    );
  });

  it("accepts only an empty unavailable snapshot", () => {
    const unavailable = {
      ...v3Snapshot(),
      state: "unavailable",
      state_changed_at_ms: 0,
      social_evidence_as_of_ms: 0,
      eligible_total: 0,
      items: [],
    } as const;
    const unavailableWithLkg = { ...v3Snapshot(), state: "unavailable" } as const;

    expect(parseTokenRadarSnapshot(unavailable)).toEqual(unavailable);
    expect(() => parseTokenRadarSnapshot(unavailableWithLkg)).toThrow(
      /token_radar_snapshot_contract:snapshot.state/,
    );
  });

  it("rejects duplicate or out-of-order server target rows", () => {
    const first = structuredClone(v3Snapshot().items[0]);
    const newer = structuredClone(first);
    newer.target.target_id = "asset:solana:token:token-2";
    newer.target.symbol = "TOKEN2";
    newer.target.name = "TOKEN2 Network";
    newer.target.address = "token-2";
    newer.trigger_event_id = "event-token-2";
    newer.qualified_at_ms += 1_000;
    const outOfOrder = {
      ...v3Snapshot(),
      eligible_total: 2,
      items: [first, newer],
    };
    const duplicate = {
      ...v3Snapshot(),
      eligible_total: 2,
      items: [first, structuredClone(first)],
    };

    expect(() => parseTokenRadarSnapshot(outOfOrder)).toThrow(
      /token_radar_snapshot_contract:snapshot.items/,
    );
    expect(() => parseTokenRadarSnapshot(duplicate)).toThrow(
      /token_radar_snapshot_contract:snapshot.items/,
    );
  });

  it("rejects a qualification that predates its causal trigger", () => {
    const invalid = structuredClone(v3Snapshot());
    invalid.items[0].qualified_at_ms = invalid.items[0].trigger_source_event_at_ms - 1;

    expect(() => parseTokenRadarSnapshot(invalid)).toThrow(
      /token_radar_snapshot_contract:snapshot.items\[0\].qualified_at_ms/,
    );
  });

  it("rejects a qualification newer than the represented social evidence", () => {
    const invalid = structuredClone(v3Snapshot());
    invalid.items[0].qualified_at_ms = invalid.social_evidence_as_of_ms + 1;

    expect(() => parseTokenRadarSnapshot(invalid)).toThrow(
      /token_radar_snapshot_contract:snapshot.items\[0\].qualified_at_ms/,
    );
  });

  it("rejects a since-signal change backed by a pre-trigger price observation", () => {
    const invalid = structuredClone(v3Snapshot());
    invalid.items[0].market.price_observed_at_ms = invalid.items[0].trigger_source_event_at_ms - 1;

    expect(() => parseTokenRadarSnapshot(invalid)).toThrow(
      /token_radar_snapshot_contract:snapshot.items\[0\].market.price_change_since_signal/,
    );
  });

  it("preserves all fifty exact server-prioritized v3 rows", () => {
    const value = snapshot(50);
    expect(parseTokenRadarSnapshot(value)).toEqual(value);
    expect(parseTokenRadarSnapshot(value).items.map((item) => item.target.symbol)).toEqual(
      Array.from({ length: 50 }, (_, index) => `TOKEN${index + 1}`),
    );
  });

  it.each([
    ["wrong schema", () => ({ ...snapshot(), schema_version: "legacy" })],
    [
      "retired v2 snapshot",
      () => ({
        schema_version: "token_radar_snapshot_v2",
        evidence_as_of_ms: 1_778_426_440_000,
        eligible_total: 0,
        items: [],
      }),
    ],
    ["extra root field", () => ({ ...snapshot(), legacy: true })],
    ["more than fifty rows", () => snapshot(51)],
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
        (
          value.items[0].market as {
            price_observed_at_ms: number | null;
          }
        ).price_observed_at_ms = null;
        return value;
      },
    ],
    [
      "price observation without a metric",
      () => {
        const value = snapshot();
        (
          value.items[0].market as {
            price_usd: number | null;
          }
        ).price_usd = null;
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
      "retired market status",
      () => {
        const value = snapshot();
        (value.items[0].market as Record<string, unknown>).status = "confirmed";
        return value;
      },
    ],
    [
      "unknown counter evidence",
      () => {
        const value = snapshot();
        (value.items[0] as Record<string, unknown>).counter_evidence = null;
        return value;
      },
    ],
    [
      "asset with an exchange",
      () => {
        const value = snapshot();
        (value.items[0].target as { exchange: string | null }).exchange = "binance";
        return value;
      },
    ],
    [
      "asset without an address",
      () => {
        const value = snapshot();
        (value.items[0].target as { address: string | null }).address = null;
        return value;
      },
    ],
  ])("rejects %s", (_name, makeValue) => {
    expect(() => parseTokenRadarSnapshot(makeValue())).toThrow(/token_radar_snapshot_contract/);
  });

  it("keeps social, price, and market-cap freshness independent", () => {
    const value = snapshot(1);
    const market = value.items[0].market as {
      market_cap_usd: number | null;
      market_cap_observed_at_ms: number | null;
    };
    market.market_cap_usd = null;
    market.market_cap_observed_at_ms = null;
    value.items[0].market.price_observed_at_ms = value.social_evidence_as_of_ms + 60_000;

    expect(parseTokenRadarSnapshot(value)).toEqual(value);
  });
});

function v3Snapshot() {
  return snapshot(1);
}

function snapshot(count = 2) {
  return {
    schema_version: TOKEN_RADAR_SNAPSHOT_SCHEMA,
    state: "current" as const,
    stale_reason: null,
    state_changed_at_ms: 1_778_426_420_000,
    social_evidence_as_of_ms: 1_778_426_440_000,
    eligible_total: count,
    items: Array.from({ length: count }, (_, index) => item(index)),
  };
}

function item(index: number) {
  const number = index + 1;
  const symbol = `TOKEN${number}`;
  const id = `token-${number}`;
  const qualifiedAtMs = 1_778_426_435_000 - index * 1_000;
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
    trigger_source_event_at_ms: qualifiedAtMs - 5_000,
    qualified_at_ms: qualifiedAtMs,
    why_now: { current_mentions: 7, prior_mentions: 2, mention_delta: 5 },
    evidence: {
      independent_author_count: 4,
      independent_text_count: 5,
      time_to_nth_author_ms: 90_000,
      duplicate_share: 0.1,
    },
    market: {
      price_usd: 0.00003281,
      price_observed_at_ms: 1_778_426_439_000,
      price_change_since_signal: 0.12,
      market_cap_usd: 1_250_000,
      market_cap_observed_at_ms: 1_778_426_438_000,
    },
  };
}
