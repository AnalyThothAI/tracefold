import {
  compactNumber,
  formatPercentShare,
  formatRelativeTime,
  formatRisk,
  formatSignedPercent,
  formatTokenPriceUsd,
  formatUsdCompact,
} from "@lib/format";
import { describe, expect, it } from "vitest";

describe("format helpers", () => {
  it("compacts large numbers for dense cockpit cells", () => {
    expect(compactNumber(1250)).toBe("1.3K");
    expect(compactNumber(1_250_000)).toBe("1.3M");
  });

  it("formats relative milliseconds without locale noise", () => {
    expect(formatRelativeTime(1_000, 31_000)).toBe("30s");
    expect(formatRelativeTime(1_000, 181_000)).toBe("3m");
  });

  it("formats normalized mindshare as a compact percent", () => {
    expect(formatPercentShare(0.5)).toBe("50%");
    expect(formatPercentShare(0.0123)).toBe("1.2%");
  });

  it("formats market cap and signed price changes for radar cells", () => {
    expect(formatUsdCompact(15_200)).toBe("$15K");
    expect(formatSignedPercent(0.124)).toBe("+12%");
    expect(formatSignedPercent(-0.084)).toBe("-8.4%");
    expect(formatSignedPercent(null)).toBe("-");
  });

  it("formats token prices without rounding away tradable decimals", () => {
    expect(formatTokenPriceUsd(2.753)).toBe("$2.75");
    expect(formatTokenPriceUsd(0.24668459858168806)).toBe("$0.2467");
    expect(formatTokenPriceUsd(0.00001360704303591779)).toBe("$0.00001361");
    expect(formatTokenPriceUsd(0.0000000006522)).toBe("$6.52e-10");
    expect(formatTokenPriceUsd(null)).toBe("-");
  });

  it("formats stock health codes", () => {
    expect(formatRisk("quote_unavailable")).toBe("quote unavailable");
  });
});
