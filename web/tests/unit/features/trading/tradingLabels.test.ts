import {
  bpsPercent,
  isActiveIntent,
  policyReasonLabel,
  stopVerified,
} from "@features/trading/model/tradingLabels";
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

  it("names a system block and a policy rule from the same lookup, and neither invents a synonym", () => {
    // #331: `BLOCKED` reasons and policy rules are two closed vocabularies with one reader. A key with
    // no entry renders as itself — it is the string an operator greps.
    expect(policyReasonLabel("capability_mismatch")).toBe("能力指针已改变，冻结的合约不再权威");
    expect(policyReasonLabel("smart_money_momentum_long")).toBe("聪明钱动量 · 做多");
    expect(policyReasonLabel("a_rule_nobody_translated")).toBe("a_rule_nobody_translated");
    expect(policyReasonLabel(null)).toBe("—");
    // The retired catch-all has no translation because nothing writes it any more.
    expect(policyReasonLabel("intent_admission_blocked")).toBe("intent_admission_blocked");
  });

  it("prints a signed percentage from basis points, and a dash for an unmeasured one", () => {
    expect(bpsPercent(187)).toBe("+1.87%");
    expect(bpsPercent(-312)).toBe("−3.12%");
    expect(bpsPercent(null)).toBe("—");
  });
});
