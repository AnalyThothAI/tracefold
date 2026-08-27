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
      cases_without_orders: [
        tradingCaseFixture({
          // The lane whose seven frozen keys `FROZEN` is; `_exact_keys` gives each strategy its own set.
          strategy_id: "oi_smart_money_momentum_v1",
          strategy_config: FROZEN,
          ...caseOverrides,
        }),
      ],
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

  it("anchors each tick on its own edge rather than on its place in the list", () => {
    /*
     * `spaced()` returns two to four ticks, so `:first-child` is often a *threshold* — for any pre-move in
     * roughly (−228, 0) bps the domain start is dropped and `0.00%` leads the list. Left-anchoring it put
     * the label 10.7px inside the band rather than on the boundary it names.
     */
    const ticks = build({ pre_move_bps: -73 }).band?.ticks ?? [];

    expect(ticks[0]).toMatchObject({ anchor: "middle", label: "0.00%" });
    expect(ticks.at(-1)?.anchor).toBe("end");
  });

  it("merges the two thresholds into one label when the band is too narrow to name twice", () => {
    // At +200% the 0–10% band is a twentieth of the bar. Neither threshold may be dropped — between them
    // they are the rule — so they become one range at the band's own midpoint instead of overprinting.
    const ticks = build({ pre_move_bps: 20_000 }).band?.ticks ?? [];

    expect(ticks.map((tick) => tick.label)).toContain("0.00%–10.00%");
  });

  it("says which regime the lane assigned when it is not one of the four quadrants", () => {
    const perspective = build({ regime: "unclear" });

    expect(perspective?.quadrants.every((cell) => !cell.active)).toBe(true);
    expect(perspective?.quadrantNote).toContain("四个象限都不成立");
  });

  it("names the frozen regime reason, never the strategy's later answer", () => {
    /*
     * The population the smart-money lane creates routinely: it accepts a move between the shared 600 bps
     * ceiling and its own 1000, so the Case is *traded* — `regime` is `unclear` and `policy_reason` is
     * null — while `contexts.regime.reason` durably holds `move_above_band_chasing`. Reading
     * `policy_reason` here told an operator the ledger had recorded no reason, over a manifest that had.
     */
    const traded = build({
      policy_decision: "long",
      policy_reason: null,
      regime: "unclear",
      regime_reason: "move_above_band_chasing",
    });

    expect(traded.quadrantNote).toContain("追高（走势带外）");
    expect(traded.quadrantNote).not.toContain("账本没有发布");
  });

  it("measures the frame against the floors the case froze, and calls an absent floor unfrozen", () => {
    const frame = newsOiFrameFixture();
    const perspective = build({}, { ...frame, oi: { ...frame.oi!, whale_oi_ratio_bps: 3869 } });
    const ratio = perspective.floors.find((floor) => floor.key === "ratio");
    // 38.69% against the 50% the case was frozen at — not against whatever is configured today.
    expect(ratio?.measured).toBe("38.69%");
    expect(ratio?.floor).toBe("> 50.00%");
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
    expect(ratio?.floor).toBe("> 50.00%");
    // The other half stays 未冻结: this strategy freezes no size floor at all (#264).
    expect(unmeasured().floors.find((floor) => floor.key === "value")?.verdict).toBe("unset");
  });

  it("counts the closing sentence off the rows rather than asserting it", () => {
    const frame = newsOiFrameFixture();

    // All three frozen floors cleared by the fixture frame.
    expect(
      build(
        {},
        {
          ...newsOiFrameFixture(),
          oi: { ...newsOiFrameFixture().oi!, whale_long_profit_bps: 9600 },
        },
      ).floorsNote,
    ).toContain("3 条全过了");
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
    /*
     * A case the deterministic OI trigger did not author publishes no joinable `event_id` at all — the
     * common shape, per `tradingLedgerEntries`. Naming a frame that is "not on this page" would invent one.
     */
    expect(symbolPerspective(undefined, entryOf({ event_id: null }))?.floorsNote).toContain(
      "没有发布可连的 event_id",
    );
  });

  it("compares each floor with the operator its own strategy refuses on", () => {
    /*
     * `oi_smart_money_momentum.py` refuses `whale_oi_ratio_bps <= floor` and `whale_long_profit_bps <=
     * floor` — its docstring calls the inclusivity non-negotiable and spells the boundary out: 「`5001`
     * qualifies and `5000` does not; `1` qualifies and `0` does not」. Reading `>=` stamped 过地板 on
     * exactly those refusals, and the shipped profit floor is 0, so the boundary is the common value.
     */
    const frame = newsOiFrameFixture();
    const atFloor = build(
      { strategy_config: { ...FROZEN, min_whale_long_profit_bps: "0" } },
      { ...frame, oi: { ...frame.oi!, whale_long_profit_bps: 0, whale_oi_ratio_bps: 5000 } },
    );

    expect(atFloor.floors.find((floor) => floor.key === "profit")?.verdict).toBe("fail");
    expect(atFloor.floors.find((floor) => floor.key === "ratio")?.verdict).toBe("fail");
    // And the label is the operator the strategy wrote, not a rounder one.
    expect(atFloor.floors.find((floor) => floor.key === "ratio")?.floor).toBe("> 50.00%");
    // `min_oi_change_bps` really is `>=` (`oi_change_bps < floor` refuses), so it is not swept along.
    expect(
      build({}, { ...frame, oi: { ...frame.oi!, oi_change_bps: 500 } }).floors.find(
        (floor) => floor.key === "change",
      ),
    ).toMatchObject({ floor: "≥ 5.00%", verdict: "pass" });
  });

  it("shows the floors of the case's own strategy, and none when it knows none", () => {
    // `/api/trading/orders?underlying=` filters on the name alone, so the newest case can be any lane's.
    const momentum = build({
      strategy_id: "oi_momentum_v1",
      strategy_config: { allow_short: "False", min_whale_long_profit_bps: "9500" },
    });

    // One floor, compared on `>=` through the shared `oi_gate`; no 大户占比 row it never froze.
    expect(momentum.floors.map((floor) => floor.key)).toEqual(["profit", "value"]);
    expect(momentum.floors.find((floor) => floor.key === "profit")?.floor).toBe("≥ 95.00%");

    const unknown = build({ strategy_id: "some_future_lane_v1", strategy_config: {} });
    expect(unknown.floors).toEqual([]);
    expect(unknown.floorsNote).toContain("不替它猜");
  });

  it("has no band when the case froze no price thresholds", () => {
    // A case written before #273 froze no `strategy_config`; drawing a band would invent its edges.
    expect(build({ strategy_config: {} }).band).toBeNull();
  });
});
