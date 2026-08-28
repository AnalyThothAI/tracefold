import { tradingOiCellCopy, tradingOiTraceEntries, type TradingOiLookup } from "@features/trading";
import { tradingCaseFixture, tradingIntentFixture } from "@tests/fixtures/tradingFixture";
import { describe, expect, it } from "vitest";

describe("OI Trading intent presentation", () => {
  it("uses native intent and outcome fields", () => {
    const intent = tradingIntentFixture({
      execution_state: "TERMINAL",
      realized_pnl_amount: "-0.34",
      realized_pnl_currency: "USDT",
      terminal_outcome: "CLOSED_FLAT",
    });
    const subject = lookup({ kind: "intent", value: intent });

    expect(tradingOiCellCopy(subject).primary).toBe("多 · TERMINAL −0.34 USDT");
    expect(tradingOiTraceEntries(subject)).toContainEqual(["intent_id", "intent-sol"]);
    expect(tradingOiTraceEntries(subject)).toContainEqual(["terminal_outcome", "CLOSED_FLAT"]);
  });

  it("preserves named policy refusal on a case without intent", () => {
    const copy = tradingOiCellCopy(
      lookup({
        kind: "case",
        value: tradingCaseFixture({ policy_reason: "whale_long_profit_below_floor" }),
      }),
    );
    expect(copy.primary).toBe("拒 · 鲸盈利未达地板");
  });
});

function lookup(entry?: TradingOiLookup["entry"]): TradingOiLookup {
  return {
    complete: true,
    entry,
    eventId: "evt-oi-sol",
    gate: undefined,
    gateAnswered: true,
    gateComplete: true,
    loadFailed: false,
    loaded: true,
  };
}
