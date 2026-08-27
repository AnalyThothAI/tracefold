import type { TradingCase, TradingCounts, TradingStatus } from "@features/trading";
import {
  bindingCaseRule,
  caseNumbers,
  evidenceByCase,
  funnelLevels,
  laneCounts,
  refusalOf,
  strategyNumbers,
} from "@features/trading/model/tradingReadout";
import {
  gateEvidence,
  tradingCaseFixture,
  tradingStatusFixture,
} from "@tests/fixtures/tradingFixture";
import { describe, expect, it } from "vitest";

/**
 * The answer layer, which is the whole point of #273: an operator has to be able to read "why is there
 * no order" off the page without opening the code.
 *
 * The load-bearing property in every test below is that a number on screen comes from either the
 * durable ledger or the running strategy config, and never from a literal in the browser. A console
 * that hardcodes "5%" keeps saying 5% for a day after someone edits the config, and the cases beside
 * it were decided by the other number.
 */

const STATUS = tradingStatusFixture({
  strategies: [
    {
      config: {
        allow_short: "False",
        max_price_move_bps: "1000",
        measurement_window_ms: "300000",
        min_oi_change_bps: "500",
        min_price_move_bps: "0",
        min_whale_long_profit_bps: "0",
        min_whale_oi_ratio_bps: "5000",
      },
      config_digest: "a".repeat(64),
      permission: "paper",
      strategy_id: "oi_smart_money_momentum_v1",
      strategy_version: "oi_smart_money_momentum_v1",
      trigger_kinds: ["oi"],
    },
  ],
}) as TradingStatus;

const NUMBERS = strategyNumbers(STATUS);

describe("strategyNumbers", () => {
  it("reads every threshold from the running config rather than from a literal", () => {
    expect(NUMBERS).toEqual({
      maxPriceMoveBps: 1000,
      measurementWindowMs: 300_000,
      minOiChangeBps: 500,
      minWhaleLongProfitBps: 0,
      minWhaleOiRatioBps: 5000,
    });
  });

  it("reports a missing config as absent, never as zero", () => {
    // A zero here would render "未达 0.00%", which is a sentence about a strategy nobody is running.
    const blank = strategyNumbers(tradingStatusFixture({ strategies: [] }) as TradingStatus);
    expect(blank.minOiChangeBps).toBeNull();
    expect(refusalOf("smart_money_oi_change_below_floor", { numbers: blank }).threshold).toBe("—");
  });
});

describe("refusalOf", () => {
  it("states the measured value and the threshold it missed", () => {
    const refusal = refusalOf("smart_money_oi_change_below_floor", {
      evidence: gateEvidence({ oi_change_bps: 313 }),
      numbers: NUMBERS,
    });
    expect(refusal.sentence).toBe("5 分钟持仓增幅 3.13%，未达 5.00% 门槛");
    expect([refusal.measured, refusal.threshold]).toEqual(["3.13%", "5.00%"]);
  });

  it("keeps each rule's own comparison in words, because the two are not the same", () => {
    // `oi_change` refuses *below* its floor; the ratio refuses *at or below* its own. The strategy
    // tests pin 499/500 and 5000/5001 for exactly this reason, and a screen that said "未达" for both
    // would describe 5000 as failing to reach a bar it is in fact sitting exactly on.
    expect(
      refusalOf("smart_money_oi_change_below_floor", {
        evidence: gateEvidence({ oi_change_bps: 499 }),
        numbers: NUMBERS,
      }).sentence,
    ).toContain("未达");
    expect(
      refusalOf("smart_money_ratio_below_or_equal_floor", {
        evidence: gateEvidence({ whale_oi_ratio_bps: 5000 }),
        numbers: NUMBERS,
      }).sentence,
    ).toBe("大户持仓占比 50.00%，未超过 50.00%");
  });

  it("names the chasing ceiling with the pre-move the case froze", () => {
    expect(
      refusalOf("move_above_band_chasing", { numbers: NUMBERS, preMoveBps: 1_240 }).sentence,
    ).toBe("价格已先涨 12.40%，超过 10.00% 追价上限");
  });

  it("separates a missing pre-move from an unconfirmed one", () => {
    // "no evidence" and "the evidence says no" are different operational answers and both refuse.
    expect(refusalOf("price_direction_not_confirmed", { numbers: NUMBERS }).sentence).toContain(
      "没有可比的收盘价",
    );
    expect(
      refusalOf("price_direction_not_confirmed", { numbers: NUMBERS, preMoveBps: -30 }).sentence,
    ).toBe("价格未同步上涨（帧前 -0.30%）");
  });

  it("falls back to the shared vocabulary for a rule it has not learned, and never throws", () => {
    expect(refusalOf("some_future_rule", { numbers: NUMBERS }).sentence).toBe("some_future_rule");
    expect(refusalOf("regime_no_entry:deleveraging", { numbers: NUMBERS }).sentence).toBe(
      "减仓无可跟",
    );
    expect(refusalOf("oi_context_missing", { numbers: NUMBERS }).sentence).toBe(
      "新闻旁没有同标的的持仓数据",
    );
  });

  it("explains a case with the threshold it was decided against, not today's", () => {
    /*
     * The sentence this prevents: a 700 bps frame refused under the old 1000 bps floor, rendered
     * against the running 500, reads "7.00%，未达 5.00%" — a refusal that cannot be. It is not a
     * cosmetic error either; the headline names the day's binding rule from one of these, so a
     * threshold edit would make the whole page describe a strategy no case was decided by.
     */
    const frozen = caseNumbers(
      { measurement_window_ms: "300000", min_oi_change_bps: "1000" },
      NUMBERS,
    );
    expect(
      refusalOf("smart_money_oi_change_below_floor", {
        evidence: gateEvidence({ oi_change_bps: 700 }),
        numbers: frozen,
      }).sentence,
    ).toBe("5 分钟持仓增幅 7.00%，未达 10.00% 门槛");
  });

  it("falls back to the running config only when a case froze none", () => {
    // Cases created before the projection carried the field. Today's numbers are then the best
    // available answer rather than a wrong one, and an empty document must not read as "zero".
    for (const empty of [undefined, {}]) {
      expect(caseNumbers(empty, NUMBERS)).toEqual(NUMBERS);
    }
  });

  it("says the window the frame proved rather than assuming five minutes", () => {
    const hourly = strategyNumbers(
      tradingStatusFixture({
        strategies: [
          {
            config: { measurement_window_ms: "3600000", min_oi_change_bps: "500" },
            config_digest: "b".repeat(64),
            permission: "paper",
            strategy_id: "oi_smart_money_momentum_v1",
            strategy_version: "oi_smart_money_momentum_v1",
            trigger_kinds: ["oi"],
          },
        ],
      }) as TradingStatus,
    );
    expect(
      refusalOf("smart_money_oi_change_below_floor", {
        evidence: gateEvidence({ oi_change_bps: 100 }),
        numbers: hourly,
      }).sentence,
    ).toContain("60 分钟持仓增幅");
  });
});

describe("bindingCaseRule", () => {
  it("picks the rule that refused the most cases", () => {
    const cases = [
      tradingCaseFixture({ case_id: "c1", policy_reason: "smart_money_oi_change_below_floor" }),
      tradingCaseFixture({ case_id: "c2", policy_reason: "smart_money_oi_change_below_floor" }),
      tradingCaseFixture({ case_id: "c3", policy_reason: "move_above_band_chasing" }),
    ] as TradingCase[];
    expect(bindingCaseRule(cases)).toEqual({
      count: 2,
      reason: "smart_money_oi_change_below_floor",
    });
  });

  it("counts only refusals, so an allowed case never becomes the reason there are no orders", () => {
    const cases = [
      tradingCaseFixture({ case_id: "c1", policy_decision: "long", policy_reason: "ok" }),
    ] as TradingCase[];
    expect(bindingCaseRule(cases)).toBeNull();
  });
});

describe("funnelLevels", () => {
  it("reads the rolling twin of every count, never the UTC budget-day one", () => {
    const counts = {
      ...tradingStatusFixture().counts,
      candidate_counts_24h: { CASE_CREATED: 4, REJECTED: 6 },
      cases_by_state: { ORDER_PREPARED: 2, POLICY_REJECTED: 2 },
      orders_by_state: { CLOSED: 1, OPEN: 1 },
      policy_allowed_24h: 3,
      policy_allowed_today: 99,
    } as TradingCounts;
    expect(funnelLevels(counts).map((level) => [level.label, level.value])).toEqual([
      ["上游帧到达", 10],
      ["建成案例", 4],
      ["策略放行", 3],
      ["提交订单", 2],
      ["已了结", 1],
    ]);
  });

  it("explains a drop only where one happened", () => {
    const counts = {
      ...tradingStatusFixture().counts,
      candidate_counts_24h: { CASE_CREATED: 2 },
      candidate_reasons_24h: { "eligibility:rank_above_limit": 5 },
      cases_by_state: { POLICY_REJECTED: 2 },
      policy_allowed_24h: 2,
    } as TradingCounts;
    const levels = funnelLevels(counts);
    // Nothing was refused at admission — every frame became a case — so that level explains nothing.
    expect(levels[1].note).toBeNull();
    // And nothing was refused by the strategy either.
    expect(levels[2].note).toBeNull();
  });

  it("counts 建成案例 off the case table, the same ledger 策略放行 comes from", () => {
    /*
     * The defect this guards: 建成案例 came off the gate ledger's `CASE_CREATED` while 策略放行 came
     * off the case table — the same quantity counted two ways, on two clocks — so the funnel rendered
     * 1 above 3, a shape that disproves itself.
     *
     * Note what is *not* asserted: monotonicity. The three ledgers are each bounded on their own
     * timestamp, so at a window boundary an order can be inside while its case is outside. The fix is
     * one derivation per quantity, not a clamp — a clamp would make this test pass by making the page
     * lie.
     */
    const counts = {
      ...tradingStatusFixture().counts,
      candidate_counts_24h: { CASE_CREATED: 1, REJECTED: 90 },
      cases_by_state: { ORDER_PREPARED: 3, POLICY_REJECTED: 6 },
      policy_allowed_24h: 3,
    } as TradingCounts;
    const levels = funnelLevels(counts);
    expect([levels[1].value, levels[2].value]).toEqual([9, 3]);
    // And both panels derive from one place, so the headline cannot disagree with the ladder.
    expect(laneCounts(counts).cased).toBe(levels[1].value);
  });

  it("says a case nobody has judged yet is unjudged, rather than naming a floor", () => {
    // `PENDING`/`RUNNING` cases carry no policy columns; the shared vocabulary answers a null with
    // 交易地板, which under 离下一单还差什么 reads as the reason this case has no order.
    expect(refusalOf(null, { numbers: NUMBERS }).sentence).toBe("尚未判定");
    expect(refusalOf("", { numbers: NUMBERS }).sentence).toBe("尚未判定");
  });
});

describe("evidenceByCase", () => {
  it("joins the frame's numbers to the case they grounded", () => {
    const byCase = evidenceByCase([
      { case_id: "c1", gate_evidence: { oi_change_bps: 700 } },
      { case_id: null, gate_evidence: { oi_change_bps: 100 } },
    ] as any);
    expect(byCase.get("c1")?.gate_evidence?.oi_change_bps).toBe(700);
    expect(byCase.size).toBe(1);
  });
});
