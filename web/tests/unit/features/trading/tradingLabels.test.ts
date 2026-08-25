import { holdCeiling, stopVerified } from "@features/trading/model/tradingLabels";
import { tradingOrderFixture } from "@tests/fixtures/tradingFixture";
import { describe, expect, it } from "vitest";

describe("holdCeiling", () => {
  it("states the shipped default in a unit that can express it", () => {
    /*
     * `max_holding_seconds: 1800` is what this deployment is configured with, and `Math.round(ms / 3.6e6)`
     * rendered it as `0 h` — which reads as "no ceiling", the exact opposite of a thirty-minute cap. A page
     * that states a risk control has to state it in a unit that survives the value.
     */
    expect(holdCeiling(1_800_000)).toBe("30 分钟");
    expect(holdCeiling(1_800_000)).not.toContain("0 h");
  });

  it("keeps whole hours whole and fractional hours legible", () => {
    expect(holdCeiling(4 * 3_600_000)).toBe("4 h");
    expect(holdCeiling(90 * 60_000)).toBe("1.5 h");
    expect(holdCeiling(60 * 60_000)).toBe("1 h");
  });

  it("says nothing rather than zero when there is no ceiling to state", () => {
    expect(holdCeiling(0)).toBe("—");
    expect(holdCeiling(Number.NaN)).toBe("—");
  });
});

describe("stopVerified", () => {
  it("is the OPEN state, never the presence of a stop price", () => {
    // Every prepared order carries a stop price; only OPEN has a read proving the venue holds one (#185).
    expect(stopVerified(tradingOrderFixture({ state: "OPEN" }))).toBe(true);
    for (const state of ["PREPARED", "ACKNOWLEDGED", "UNPROTECTED", "AMBIGUOUS"]) {
      expect(stopVerified(tradingOrderFixture({ state }))).toBe(false);
    }
  });
});
