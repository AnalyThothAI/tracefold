import type { NewsFeedOi, NewsOiTradeFloors } from "@features/news/api/newsQueries";
import {
  oiBuckets,
  oiChangeLabel,
  oiPercent,
  oiRankLabel,
  oiTabCount,
  oiValueZh,
  oiWindowLabel,
  parseOiTab,
} from "@features/news/model/oiSignals";
import { describe, expect, it } from "vitest";

/**
 * Display helpers for #137's deterministic OI lane (#207). Nothing here judges — every value is a server
 * field turned into characters — so what these tests pin is the two places where turning it into characters
 * could still tell the reader something untrue.
 */
describe("oiSignals", () => {
  describe("oiValueZh", () => {
    /*
     * `_usd_zh` on the server writes the open interest into the reader card that was pushed. The monitor
     * shows the same frame, so the two must not disagree about it — and the obvious JS spelling does
     * disagree: `Math.round` is half-up where Python's `:.0f` is half-to-even.
     */
    it.each([
      [32_170_000, "3217 万"],
      // The half cases, which are exactly where a naive `Math.round` diverges from the card.
      [32_165_000, "3216 万"],
      [32_155_000, "3216 万"],
      [25_000, "2 万"],
      [35_000, "4 万"],
      // 亿 keeps two decimals through the server's own integer arithmetic.
      [892_310_000, "8.92 亿"],
      [100_000_000, "1.00 亿"],
      // Below 万 the exact figure is short enough to print.
      [9_999, "9999"],
      [0, "0"],
    ])("renders %i as the card does: %s", (usd, expected) => {
      expect(oiValueZh(usd)).toBe(expected);
    });

    it("says nothing rather than zero when there is no measurement", () => {
      expect(oiValueZh(null)).toBe("—");
      expect(oiValueZh(undefined)).toBe("—");
    });
  });

  describe("oiRankLabel", () => {
    it("prints the slot a qualifying frame spent", () => {
      expect(oiRankLabel(frame({ eligible_rank_in_window: 2, max_rank_in_window: 2 }))).toBe(
        "2 / 2",
      );
    });

    it.each(["whale_ratio_below_threshold", "oi_change_below_threshold"])(
      "prints nothing for a frame %s rejected, which spent no slot",
      (rule) => {
        // `evaluate_oi` ranks every frame and the trace records it, so the number is present and means
        // "the rank it would have taken". Printing it would say the window is fuller than it is.
        expect(oiRankLabel(frame({ eligible_rank_in_window: 1, rule }))).toBe("—");
      },
    );

    it("prints nothing when the frame never parsed", () => {
      expect(oiRankLabel(null)).toBe("—");
      expect(oiRankLabel(frame({ eligible_rank_in_window: null, parsed: false }))).toBe("—");
    });
  });

  describe("oiTabCount", () => {
    const byRule = {
      whale_ratio_below_threshold: 106,
      beyond_window_rank: 30,
      opening_move_with_whale_concentration: 3,
      oi_parse_failed: 1,
    };

    it("counts the three judged tabs from the gate that decided them", () => {
      expect(oiTabCount("pushed", byRule, 141)).toBe(3);
      expect(oiTabCount("withheld", byRule, 141)).toBe(136);
      expect(oiTabCount("parse_failed", byRule, 141)).toBe(1);
    });

    it("counts 全部 as Events, which is neither the intake nor the sum of the judged", () => {
      /*
       * The three buckets hold judged verdicts and sum to 140; a frame still awaiting one renders as a row
       * with no `oi` block and belongs to none of them, so the sum would advertise fewer than the tab can
       * reach. `telemetry_received_24h` (142) is the other wrong answer — it counts provider items before
       * the Gate, so it names frames no row can ever show.
       */
      expect(oiTabCount("all", byRule, 141)).toBe(141);
      expect(oiTabCount("all", byRule, 141)).not.toBe(140);
    });

    it("has no number at all until the aggregate arrives", () => {
      for (const tab of ["all", "pushed", "withheld", "parse_failed"] as const) {
        expect(oiTabCount(tab, undefined, undefined)).toBeNull();
      }
    });
  });

  describe("oiBuckets", () => {
    const floors: NewsOiTradeFloors = {
      allow_short: false,
      enabled: false,
      execution_environment: "BINANCE_USDM_DEMO",
      max_price_move_bps: 600,
      min_oi_value_usd: 20_000_000,
      min_price_move_bps: 100,
      min_whale_long_profit_bps: 9_500,
      pre_move_lookback_ms: 3_600_000,
    };

    it("marks the measured buckets a frame falls in", () => {
      const best = oiBuckets(
        frame({ oi_value_usd: 240_000_000, whale_long_profit_bps: 9_600 }),
        floors,
      );
      expect(best.map((bucket) => bucket.label)).toEqual(["盈利正桶", "持仓最优桶"]);

      const worst = oiBuckets(
        frame({ oi_value_usd: 11_030_000, whale_long_profit_bps: 8_840 }),
        floors,
      );
      expect(worst.map((bucket) => bucket.label)).toEqual(["持仓最差桶"]);
    });

    it("marks nothing when the capital lane sent no floor", () => {
      // A zero floor is not a floor: `profit >= 0` would stamp "研究里唯一均值为正的分桶" on every frame.
      const none = oiBuckets(frame({ whale_long_profit_bps: 10 }), {
        ...floors,
        min_whale_long_profit_bps: 0,
      });
      expect(none.map((bucket) => bucket.label)).not.toContain("盈利正桶");
    });

    it("marks nothing on a frame that never parsed", () => {
      expect(oiBuckets(frame({ parsed: false }), floors)).toEqual([]);
      expect(oiBuckets(null, floors)).toEqual([]);
    });
  });

  describe("the remaining formatters", () => {
    it("spells the OI change's own sign, which is open interest and never price", () => {
      expect(oiChangeLabel(frame({ oi_change_bps: 671 }))).toBe("+6.71%");
      expect(oiChangeLabel(frame({ oi_change_bps: -312 }))).toBe("−3.12%");
      expect(oiChangeLabel(frame({ oi_change_bps: null, parsed: false }))).toBe("—");
    });

    it("renders magnitudes unsigned and missing ones as nothing", () => {
      expect(oiPercent(14_390)).toBe("143.90%");
      expect(oiPercent(8_000, 1)).toBe("80.0%");
      expect(oiPercent(null)).toBe("—");
    });

    it("spells the rank window exactly, including the sub-hour ones config admits", () => {
      // `news.oi.window_ms` is a duration and its bounds start at five minutes. Rounding to whole hours
      // printed `1 小时` beside a live 30-minute threshold on both the gate row and the occupancy card.
      expect(oiWindowLabel(14_400_000)).toBe("4 小时");
      expect(oiWindowLabel(1_800_000)).toBe("30 分钟");
      expect(oiWindowLabel(5_400_000)).toBe("1.5 小时");
      expect(oiWindowLabel(300_000)).toBe("5 分钟");
      // No policy yet: the caller drops the label rather than printing a zero.
      expect(oiWindowLabel(null)).toBe("");
    });

    it("falls back to the whole lane for an unknown tab in the URL", () => {
      expect(parseOiTab("withheld")).toBe("withheld");
      expect(parseOiTab("whale_ratio_below_threshold")).toBe("all");
      expect(parseOiTab(null)).toBe("all");
    });
  });
});

function frame(overrides: Partial<NewsFeedOi> = {}): NewsFeedOi {
  return {
    eligible_rank_in_window: 1,
    failure_stage: null,
    max_rank_in_window: 2,
    oi_change_at_least_bps: 0,
    oi_change_bps: 671,
    oi_value_usd: 11_030_000,
    parsed: true,
    parser_version: null,
    rank_semantics: "eligible_rank_v1",
    rule: "opening_move_with_whale_concentration",
    title_sha256: null,
    whale_long_profit_bps: 8_840,
    whale_oi_ratio_above_bps: 8_000,
    whale_oi_ratio_bps: 14_390,
    window_ms: 14_400_000,
    ...overrides,
  };
}
