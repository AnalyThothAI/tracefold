import type { NewsFeedOi } from "@features/news/api/newsQueries";
import {
  oiBuckets,
  oiChangeLabel,
  oiPercent,
  oiTabCount,
  oiValueZh,
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

  describe("oiTabCount", () => {
    const byRule = { stored: 139, oi_parse_failed: 1 };

    it("counts 解析失败 from the judged rule that names it", () => {
      expect(oiTabCount("parse_failed", byRule, 141)).toBe(1);
    });

    it("counts 全部 as Events, which is neither the intake nor the sum of the judged", () => {
      /*
       * The two buckets hold judged verdicts and sum to 140; a frame still awaiting one renders as a row
       * with no `oi` block and belongs to neither, so the sum would advertise fewer than the tab can
       * reach. `telemetry_received_24h` (142) is the other wrong answer — it counts provider items before
       * the Gate, so it names frames no row can ever show.
       */
      expect(oiTabCount("all", byRule, 141)).toBe(141);
      expect(oiTabCount("all", byRule, 141)).not.toBe(140);
    });

    it("has no number at all until the aggregate arrives", () => {
      for (const tab of ["all", "parse_failed"] as const) {
        expect(oiTabCount(tab, undefined, undefined)).toBeNull();
      }
    });
  });

  describe("oiBuckets", () => {
    it("marks the measured buckets a frame falls in", () => {
      const best = oiBuckets(frame({ oi_value_usd: 240_000_000, whale_long_profit_bps: 9_600 }));
      expect(best.map((bucket) => bucket.label)).toEqual(["盈利正桶", "持仓最优桶"]);

      const worst = oiBuckets(frame({ oi_value_usd: 11_030_000, whale_long_profit_bps: 8_840 }));
      expect(worst.map((bucket) => bucket.label)).toEqual(["持仓最差桶"]);
    });

    it("reads the research boundary, never a lane threshold", () => {
      /*
       * #331: the 95% boundary is a *measurement* from `oi-agent-design-2026-08-22.md` §1.5, not the
       * capital lane's floor. It used to be read off the lane's republished settings, which made a
       * research fact move whenever an operator edited a threshold — and the lane no longer publishes a
       * whale-profit floor at all, because its policy freezes that number onto each Case.
       */
      expect(oiBuckets(frame({ whale_long_profit_bps: 9_500 })).map((b) => b.label)).toContain(
        "盈利正桶",
      );
      expect(oiBuckets(frame({ whale_long_profit_bps: 9_499 })).map((b) => b.label)).not.toContain(
        "盈利正桶",
      );
    });

    it("marks nothing on a frame that never parsed", () => {
      expect(oiBuckets(frame({ parsed: false }))).toEqual([]);
      expect(oiBuckets(null)).toEqual([]);
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

    it("falls back to the whole lane for an unknown tab in the URL", () => {
      expect(parseOiTab("parse_failed")).toBe("parse_failed");
      // Both a rule name and a tab #458 retired resolve to the whole lane rather than a 4xx.
      expect(parseOiTab("withheld")).toBe("all");
      expect(parseOiTab("whale_ratio_below_threshold")).toBe("all");
      expect(parseOiTab(null)).toBe("all");
    });
  });
});

function frame(overrides: Partial<NewsFeedOi> = {}): NewsFeedOi {
  return {
    failure_stage: null,
    oi_change_bps: 671,
    oi_value_usd: 11_030_000,
    parsed: true,
    parser_version: null,
    rule: "stored",
    symbol: "SKR",
    title_sha256: null,
    whale_long_profit_bps: 8_840,
    whale_oi_ratio_bps: 14_390,
    ...overrides,
  };
}
