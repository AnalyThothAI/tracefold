import {
  shouldHandleLiveWindowHotkey,
  shouldRouteTopbarSearchToNews,
} from "@routes/shellChromeData";
import { describe, expect, it } from "vitest";

describe("shellChromeData", () => {
  it("keeps live window hotkeys scoped to routes that own shell window controls", () => {
    expect(shouldHandleLiveWindowHotkey("/", "1")).toBe(true);
    expect(shouldHandleLiveWindowHotkey("/stocks", "4")).toBe(true);
    expect(shouldHandleLiveWindowHotkey("/stocks?scope=matched", "2")).toBe(true);

    expect(shouldHandleLiveWindowHotkey("/macro", "1")).toBe(false);
    expect(shouldHandleLiveWindowHotkey("/token/canonical/solana:abc", "3")).toBe(false);
    expect(shouldHandleLiveWindowHotkey("/search", "4")).toBe(false);
    expect(shouldHandleLiveWindowHotkey("/", "/")).toBe(false);
  });

  it("scopes topbar search to news on news routes only", () => {
    expect(shouldRouteTopbarSearchToNews("/news")).toBe(true);
    expect(shouldRouteTopbarSearchToNews("/news/stories/story-1")).toBe(true);

    expect(shouldRouteTopbarSearchToNews("/")).toBe(false);
    expect(shouldRouteTopbarSearchToNews("/search")).toBe(false);
    expect(shouldRouteTopbarSearchToNews("/macro/news-cycle")).toBe(false);
  });
});
