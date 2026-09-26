import { researchReturnPath, withResearchReturn } from "@shared/routing/researchContext";
import { describe, expect, it } from "vitest";

describe("research return navigation", () => {
  it("preserves the selected observation and anchored research scope across reloads", () => {
    const origin = "/news/market?asset=WIF&hours=24&to_ms=1779000000000&item=observation-1";
    const link = withResearchReturn("/trading?tab=executions&execution_case=case-1", origin);
    const params = new URL(link, "https://tracefold.invalid").searchParams;
    expect(params.get("execution_case")).toBe("case-1");
    expect(researchReturnPath(params.get("research_from"))).toBe(origin);
    expect(researchReturnPath("/news/market/observation-1")).toBe("/news/market/observation-1");
  });

  it.each([
    null,
    "https://evil.invalid/news/market",
    "//evil.invalid/news/market",
    "javascript:alert(1)",
    "/news/marketplace",
    "/news/market/../wallets",
    "/news/market/a/b",
  ])("rejects non-research return locations: %s", (path) => {
    expect(researchReturnPath(path)).toBeNull();
    expect(withResearchReturn("/trading?tab=decisions", path)).toBe("/trading?tab=decisions");
  });
});
