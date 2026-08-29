import { parseFeedFilters } from "@features/news/model/feedFilters";
import { describe, expect, it } from "vitest";

describe("News feed URL filters", () => {
  it("preserves a symbol beyond the HTTP limit so the server rejects it instead of truncating it", () => {
    const symbol = "A".repeat(33);

    expect(parseFeedFilters(new URLSearchParams({ symbol })).symbol).toBe(symbol);
  });
});
