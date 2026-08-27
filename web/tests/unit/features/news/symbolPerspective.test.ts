import { symbolPerspective } from "@features/news/model/symbolPerspective";
import { tradingLedgerEntries } from "@features/trading";
import { newsOiFrameFixture } from "@tests/fixtures/newsFixture";
import { tradingCaseFixture, tradingOrdersFixture } from "@tests/fixtures/tradingFixture";
import { describe, expect, it } from "vitest";

const FROZEN = {
  allow_short: "False",
  max_price_move_bps: "1000",
  measurement_window_ms: "300000",
  min_oi_change_bps: "500",
  min_price_move_bps: "0",
  min_whale_long_profit_bps: "0",
  min_whale_oi_ratio_bps: "5000",
};

function entryOf(caseOverrides = {}) {
  const ledger = tradingLedgerEntries(
    tradingOrdersFixture({
      cases_without_orders: [tradingCaseFixture({ strategy_config: FROZEN, ...caseOverrides })],
      orders: [],
    }),
  );
  return [...ledger.values()][0];
}

function build(caseOverrides = {}, frame = newsOiFrameFixture()) {
  const perspective = symbolPerspective(frame, entryOf(caseOverrides));
  if (perspective == null) throw new Error("the fixture always opens a case");
  return perspective;
}

/** The same case, over a frame that carries no OI block — a news Event, or a frame off this page. */
function unmeasured(caseOverrides = {}) {
  return build(caseOverrides, { ...newsOiFrameFixture(), oi: null });
}

describe("symbolPerspective", () => {
  it("has no reading at all when the lane never opened a case", () => {
    // Not four grey quadrants and an unmarked band: "never asked" and "asked and found nothing" are
    // different answers, and the panel may only render the second when it is true.
    expect(symbolPerspective(newsOiFrameFixture(), undefined)).toBeNull();
  });

  it("reads the band off the case's frozen config, not off a hard-coded research band", () => {
    /*
     * The artifact draws 1–6% over an hour. Those were the thresholds and the window of the strategy
     * running when it was drawn; today's are 0–10% over five minutes, and a panel that hard-coded either
     * would explain a case against numbers it was never measured by.
     */
    const perspective = build({ pre_move_bps: 187 });

    expect(perspective?.band?.caption).toBe("帧前 5 分钟已走行情");
    expect(perspective?.band?.placement).toBe("in");
    expect(perspective?.band?.measured).toBe("+1.87%");
    expect(perspective?.band?.ticks.map((tick) => tick.label)).toContain("10.00%");
  });

  it("tells a move short of the floor from one past the ceiling", () => {
    // One sentence for both said 追涨 over a frame whose price had gone *down*.
    expect(build({ pre_move_bps: -73 })?.band?.placement).toBe("below");
    expect(build({ pre_move_bps: 1200 })?.band?.placement).toBe("above");
    expect(build({ pre_move_bps: null })?.band?.placement).toBe("unmeasured");
  });

  it("drops a domain tick that would print on top of a threshold", () => {
    // A −0.73% move pulls the domain start to five percent of the bar away from the 0.00% floor; both
    // labels drawn is two numbers stacked on each other, which says less than one of them alone.
    const labels = build({ pre_move_bps: -73 }).band?.ticks.map((tick) => tick.label) ?? [];
    expect(labels).not.toContain("−0.73%");
    expect(labels[0]).toBe("0.00%");
  });

  it("says which regime the lane assigned when it is not one of the four quadrants", () => {
    const perspective = build({ regime: "unclear" });

    expect(perspective?.quadrants.every((cell) => !cell.active)).toBe(true);
    expect(perspective?.quadrantNote).toContain("四个象限都不成立");
  });

  it("measures the frame against the floors the case froze, and calls an absent floor unfrozen", () => {
    const frame = newsOiFrameFixture();
    const perspective = build({}, { ...frame, oi: { ...frame.oi!, whale_oi_ratio_bps: 3869 } });
    const ratio = perspective.floors.find((floor) => floor.key === "ratio");
    // 38.69% against the 50% the case was frozen at — not against whatever is configured today.
    expect(ratio?.measured).toBe("38.69%");
    expect(ratio?.floor).toBe("≥ 50.00%");
    expect(ratio?.verdict).toBe("fail");
    // The size floor moved to the Candidate Gate with its own digest (#264) and this strategy freezes
    // none. `unset`, never a pass: an unread threshold and an absent one are different facts.
    expect(perspective?.floors.find((floor) => floor.key === "value")?.verdict).toBe("unset");
  });

  it("tells a floor the case never froze from one the frame never measured", () => {
    /*
     * Both were `unset`, and `unset` renders 未冻结 — so the card printed 未冻结 in the same row as the
     * `≥ 50.00%` the case had frozen, contradicting itself about the one fact it exists to carry.
     */
    const ratio = unmeasured().floors.find((floor) => floor.key === "ratio");

    expect(ratio?.verdict).toBe("unmeasured");
    expect(ratio?.floor).toBe("≥ 50.00%");
    // The other half stays 未冻结: this strategy freezes no size floor at all (#264).
    expect(unmeasured().floors.find((floor) => floor.key === "value")?.verdict).toBe("unset");
  });

  it("counts the closing sentence off the rows rather than asserting it", () => {
    const frame = newsOiFrameFixture();

    // All three frozen floors cleared by the fixture frame.
    expect(build().floorsNote).toContain("3 条全过了");
    // 38.69% against the 50% floor, the other two still clear.
    expect(
      build({}, { ...frame, oi: { ...frame.oi!, whale_oi_ratio_bps: 3869 } }).floorsNote,
    ).toContain("只过了 2 条");
  });

  it("says why there is nothing to compare instead of calling it a refusal", () => {
    // 「一条都没过」 is a measurement. The card asserted it under four rows that had never been read.
    expect(unmeasured().floorsNote).not.toContain("一条都没过");
    expect(unmeasured().floorsNote).toContain("地板比不了");
    // And a case whose frame is not on this page is a third answer again — not an unmeasured frame.
    expect(symbolPerspective(undefined, entryOf())?.floorsNote).toContain("不在下面这段窗口里");
  });

  it("has no band when the case froze no price thresholds", () => {
    // A case written before #273 froze no `strategy_config`; drawing a band would invent its edges.
    expect(build({ strategy_config: {} }).band).toBeNull();
  });
});
