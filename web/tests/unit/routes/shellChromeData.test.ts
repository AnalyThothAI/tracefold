import { shouldRouteTopbarSearchToNews } from "@routes/shellChromeData";
import { describe, expect, it } from "vitest";

describe("shellChromeData", () => {
  it("scopes topbar search to news on news routes only", () => {
    expect(shouldRouteTopbarSearchToNews("/news")).toBe(true);
    expect(shouldRouteTopbarSearchToNews("/news/stories/story-1")).toBe(true);

    expect(shouldRouteTopbarSearchToNews("/")).toBe(false);
    expect(shouldRouteTopbarSearchToNews("/search")).toBe(false);
    expect(shouldRouteTopbarSearchToNews("/macro/news-cycle")).toBe(false);
  });
});
