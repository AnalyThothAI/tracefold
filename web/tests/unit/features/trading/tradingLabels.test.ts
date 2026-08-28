import { holdCeiling, isActiveIntent, stopVerified } from "@features/trading/model/tradingLabels";
import { tradingIntentFixture } from "@tests/fixtures/tradingFixture";
import { describe, expect, it } from "vitest";

describe("intent labels", () => {
  it("recognizes nonterminal intent states", () => {
    expect(isActiveIntent(tradingIntentFixture({ execution_state: "PENDING" }))).toBe(true);
    expect(isActiveIntent(tradingIntentFixture({ execution_state: "TERMINAL" }))).toBe(false);
  });

  it("requires both OPEN_PROTECTED and a protected quantity", () => {
    expect(stopVerified(tradingIntentFixture())).toBe(true);
    expect(stopVerified(tradingIntentFixture({ protected_quantity: null }))).toBe(false);
    expect(stopVerified(tradingIntentFixture({ execution_state: "IN_FLIGHT" }))).toBe(false);
  });

  it("formats a ceiling without rounding above it", () => {
    expect(holdCeiling(61 * 60_000)).toBe("1h 01m");
  });
});
