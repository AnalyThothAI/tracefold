import { tradingOiCellCopy, tradingOiTraceEntries, type TradingOiLookup } from "@features/trading";
import {
  tradingCaseFixture,
  tradingGateDecisionFixture,
  tradingOrderFixture,
} from "@tests/fixtures/tradingFixture";
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

  it("keeps the two batches' completeness apart", () => {
    /*
     * 未评估 is a claim about the frame and only the admission batch can support it; the `case` line is
     * the order batch's. Reading one for the other blamed a truncated order page for a gap in a ledger
     * that had answered in full.
     */
    const gateTruncated = { ...lookup(), gateComplete: false };
    expect(tradingOiCellCopy(gateTruncated)).toEqual({
      primary: "未确认",
      title: "准入台账批次已截断",
    });
    expect(tradingOiTraceEntries(gateTruncated)).toContainEqual([
      "gate",
      "准入台账批次已截断，本帧可能在未列出的部分",
    ]);
    expect(tradingOiTraceEntries(gateTruncated)).toContainEqual(["case", "未成案"]);

    // A truncated order batch says so on the case line and leaves the admission answer alone.
    const ordersTruncated = { ...lookup(), complete: false };
    expect(tradingOiCellCopy(ordersTruncated).primary).toBe("未评估");
    expect(tradingOiTraceEntries(ordersTruncated)).toContainEqual([
      "case",
      "未确认（交易账本批次已截断）",
    ]);
  });

  it("separates a frame the gate refused from one it has never evaluated", () => {
    /*
     * #269. These two were the same cell, and they are different operational facts: one says the lane
     * looked and named a rule, the other says no configuration of the gate has ever seen this frame.
     */
    expect(tradingOiCellCopy(lookup()).primary).toBe("未评估");
    expect(tradingOiCellCopy(gated()).primary).toBe("已拒绝 · 持仓额低于流动性地板");
    expect(tradingOiCellCopy(gated({ gate_status: "DEFERRED" })).primary).toBe(
      "待重试 · 持仓额低于流动性地板",
    );
  });

  it("names the instrument a stale routing refusal was waiting for", () => {
    // The production row #268 fixed: closed by the clock, still saying what it was waiting on.
    const copy = tradingOiCellCopy(
      gated({ gate_reason: "no_native_perp", gate_stage: "routing", gate_status: "EXPIRED" }),
    );

    expect(copy.primary).toBe("已过期 · 该场所无原生永续");
    expect(copy.secondary).toBe("routing");
  });

  it("puts the durable admission row in the trace, with the threshold it compared against", () => {
    const entries = tradingOiTraceEntries(gated());

    expect(entries).toContainEqual(["gate_reason", "oi_value_below_floor"]);
    expect(entries).toContainEqual(["gate_status", "REJECTED"]);
    // Re-reading one source 30 times is one row; the count says "seen", not "retried".
    expect(entries).toContainEqual(["attempt_count", "30"]);
    expect(entries).toContainEqual(["evidence.floor", "5000000"]);
  });

  it("says nothing was evaluated rather than inventing a refusal", () => {
    expect(tradingOiTraceEntries(lookup())).toContainEqual([
      "gate",
      "本帧在任何 gate 版本下都没有落库的准入判定",
    ]);
  });

  it("does not report a failed admission read as a frame nobody evaluated", () => {
    /*
     * The admission ledger is its own request and fails on its own. 未评估 is a claim about the frame;
     * "we could not ask" is a claim about this page, and only one of them is true when the read 5xx'd.
     */
    const unread = { ...lookup(), gateAnswered: false };

    expect(tradingOiCellCopy(unread)).toEqual({ primary: "未确认", title: "准入台账未读到" });
    expect(tradingOiTraceEntries(unread)).toContainEqual([
      "gate",
      "准入台账这一轮没有读到，无法回答为什么没有案例",
    ]);
    // And a row the order batch *did* answer is still answered exactly.
    expect(
      tradingOiCellCopy({
        ...unread,
        entry: { kind: "case", value: tradingCaseFixture({ policy_reason: "oi_context_missing" }) },
      }).primary,
    ).toBe("拒 · OI 上下文缺失");
  });

  it("does not overwrite a case's own state with the admission that opened it", () => {
    /*
     * A frame that produced a case has an admission row too (`freeze:case_created`). Reading the gate
     * first would print 已开案 where the strategy's own decision belongs.
     */
    const copy = tradingOiCellCopy({
      ...gated({ gate_reason: "case_created", gate_stage: "freeze", gate_status: "CASE_CREATED" }),
      entry: { kind: "case", value: tradingCaseFixture({ policy_reason: "oi_context_missing" }) },
    });

    expect(copy.primary).toBe("拒 · OI 上下文缺失");
  });
});

function lookup(entry?: TradingOiLookup["entry"]): TradingOiLookup {
  return {
    complete: true,
    entry,
    eventId: "evt-oi-wif",
    gate: undefined,
    gateAnswered: true,
    gateComplete: true,
    loadFailed: false,
    loaded: true,
  };
}

function gated(overrides: Partial<NonNullable<TradingOiLookup["gate"]>> = {}): TradingOiLookup {
  return { ...lookup(), gate: tradingGateDecisionFixture(overrides) };
}
