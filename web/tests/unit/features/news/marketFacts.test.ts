import {
  marketObservationTrace,
  marketParseLabel,
  marketSubject,
  nextMarketParams,
  parseMarketKinds,
  toggleMarketKind,
} from "@features/news/model/marketFacts";
import { newsMarketObservationFixture } from "@tests/fixtures/newsFixture";
import { describe, expect, it } from "vitest";

describe("market kind filter state", () => {
  it("reads the server's own comma-separated subset and drops a word it does not serve", () => {
    expect(parseMarketKinds("oi,liquidation")).toEqual(["oi", "liquidation"]);
    // Order is the page's, not the URL's, so two readers who picked the same two kinds share one query key.
    expect(parseMarketKinds("liquidation,oi")).toEqual(["oi", "liquidation"]);
    expect(parseMarketKinds("oi,telemetry_deterministic")).toEqual(["oi"]);
  });

  it("treats a filter that narrows nothing as the absence of one", () => {
    // All four is the default window. Sending it would report `filters.kind` back as a narrowing the
    // reader never made, and would split the cache key from the unfiltered page showing the same rows.
    expect(parseMarketKinds("oi,liquidation,smart_money,unknown_market")).toEqual([]);
    expect(parseMarketKinds("")).toEqual([]);
    expect(parseMarketKinds(null)).toEqual([]);
  });

  it("toggles one kind at a time and writes the URL the list reads back", () => {
    expect(toggleMarketKind([], "liquidation")).toEqual(["liquidation"]);
    expect(toggleMarketKind(["liquidation"], "oi")).toEqual(["oi", "liquidation"]);
    expect(toggleMarketKind(["oi", "liquidation"], "oi")).toEqual(["liquidation"]);
    expect(toggleMarketKind(["oi", "liquidation", "smart_money"], "unknown_market")).toEqual([]);

    expect(nextMarketParams(["oi", "liquidation"]).toString()).toBe("kind=oi%2Cliquidation");
    expect(nextMarketParams([]).toString()).toBe("");
  });
});

describe("market observation display", () => {
  it("names the subject from the parser, then from the provider, and otherwise says nothing", () => {
    expect(marketSubject(newsMarketObservationFixture())).toBe("WIF");
    expect(
      marketSubject(newsMarketObservationFixture({ raw_instrument: "PENGU-PERP", symbol: null })),
    ).toBe("PENGU-PERP");
    // No subject was read out of the record, and the title is not re-parsed to invent one.
    expect(
      marketSubject(
        newsMarketObservationFixture({
          parse_status: "raw",
          raw_instrument: null,
          symbol: null,
          title: "PENGU OI Rise 3.4%",
        }),
      ),
    ).toBe("—");
  });

  it("separates what parsed from what did not, in the closed server vocabulary", () => {
    expect(marketParseLabel("parsed")).toBe("已解析");
    expect(marketParseLabel("raw")).toBe("仅原文");
  });

  it("traces only stored fields, never a placeholder for one the record does not carry", () => {
    const trace = new Map(marketObservationTrace(newsMarketObservationFixture()));

    expect(trace.get("symbol")).toBe("WIF");
    expect(trace.get("oi_change_bps")).toBe("671");
    expect(trace.get("whale_oi_ratio_bps")).toBe("14390");
    // A liquidation field an OI frame never carries is absent rather than an em dash: twenty of those
    // would bury the three the reader came for.
    expect(trace.has("liquidated_position_side")).toBe(false);
    expect(trace.has("pnl_usd")).toBe(false);
    // `historical: false` is the ordinary case and says nothing; `true` is the fact worth printing.
    expect(trace.has("historical")).toBe(false);
    expect(
      new Map(marketObservationTrace(newsMarketObservationFixture({ historical: true }))).get(
        "historical",
      ),
    ).toBe("true");
  });
});
