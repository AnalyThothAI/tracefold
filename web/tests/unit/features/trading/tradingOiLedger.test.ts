import { tradingOiCellCopy, tradingOiTraceEntries, type TradingOiLookup } from "@features/trading";
import { tradingCaseFixture, tradingOrderFixture } from "@tests/fixtures/tradingFixture";
import { describe, expect, it } from "vitest";

describe("OI Trading ledger presentation", () => {
  it("keeps regime, order outcome and realised basis points in Trading vocabulary", () => {
    const order = tradingOrderFixture({ realized_bps: -34 });
    const copy = tradingOiCellCopy(lookup({ kind: "order", value: order }));

    expect(copy.secondary).toBe("增仓 · 价升");
    expect(copy.primary).toBe("多 · OPEN −34bps");
    expect(tradingOiTraceEntries(lookup({ kind: "order", value: order }))).toContainEqual([
      "case_state",
      "ORDER_PREPARED",
    ]);
  });

  it("translates the stored rejection reason without rebuilding it from current thresholds", () => {
    const copy = tradingOiCellCopy(
      lookup({
        kind: "case",
        value: tradingCaseFixture({ policy_reason: "whale_long_profit_below_floor" }),
      }),
    );

    expect(copy.primary).toBe("拒 · 鲸盈利未达地板");
  });

  it("distinguishes measured OI refusal reasons and preserves an unknown ledger key", () => {
    const rejected = (reason: string) =>
      tradingOiCellCopy(
        lookup({ kind: "case", value: tradingCaseFixture({ policy_reason: reason }) }),
      ).primary;

    expect(rejected("move_above_band_chasing")).toBe("拒 · 追高（走势带外）");
    expect(rejected("regime_no_entry:deleveraging_down")).toBe("拒 · 减仓无可跟");
    expect(rejected("future_policy_reason")).toBe("拒 · future_policy_reason");
  });

  it("does not call an absent record a non-case when the batch was truncated", () => {
    const copy = tradingOiCellCopy({ ...lookup(), complete: false });

    expect(copy.primary).toBe("未确认");
    expect(copy.title).toBe("交易账本批次已截断");
  });

  it("calls complete absence a non-case without a truncation warning", () => {
    expect(tradingOiCellCopy(lookup())).toEqual({ primary: "未成案" });
  });
});

function lookup(entry?: TradingOiLookup["entry"]): TradingOiLookup {
  return {
    complete: true,
    entry,
    eventId: "evt-oi-wif",
    loadFailed: false,
    loaded: true,
  };
}
