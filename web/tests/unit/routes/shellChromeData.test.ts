import {
  shouldHandleStocksWindowHotkey,
  shouldRouteTopbarSearchToNews,
} from "@routes/shellChromeData";
import { describe, expect, it } from "vitest";

describe("shellChromeData", () => {
  it("keeps window hotkeys scoped to Stocks", () => {
    expect(shouldHandleStocksWindowHotkey("/", "1")).toBe(false);
    expect(shouldHandleStocksWindowHotkey("/stocks", "4")).toBe(true);
    expect(shouldHandleStocksWindowHotkey("/stocks?window=24h", "2")).toBe(true);

    expect(shouldHandleStocksWindowHotkey("/macro", "1")).toBe(false);
    expect(shouldHandleStocksWindowHotkey("/token/canonical/solana:abc", "3")).toBe(false);
    expect(shouldHandleStocksWindowHotkey("/search", "4")).toBe(false);
    expect(shouldHandleStocksWindowHotkey("/stocks", "/")).toBe(false);
  });

  it("scopes topbar search to news on news routes only", () => {
    expect(shouldRouteTopbarSearchToNews("/news")).toBe(true);
    expect(shouldRouteTopbarSearchToNews("/news/stories/story-1")).toBe(true);

    expect(shouldRouteTopbarSearchToNews("/")).toBe(false);
    expect(shouldRouteTopbarSearchToNews("/search")).toBe(false);
    expect(shouldRouteTopbarSearchToNews("/macro/news-cycle")).toBe(false);
  });
});
