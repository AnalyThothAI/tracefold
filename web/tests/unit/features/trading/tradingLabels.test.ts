import { bpsPercent, policyLabel, policyReasonLabel } from "@features/trading/model/tradingLabels";
import { describe, expect, it } from "vitest";

describe("Alpha labels", () => {
  it("names the current Alpha identity without falling back to a historical capital label", () => {
    expect(policyLabel("source_native_oi_smart_money_long_v4")).toBe("来源原生 OI × 聪明钱 · 做多");
  });

  it("names a system block and a policy rule from the same lookup, and neither invents a synonym", () => {
    // `BLOCKED` reasons and policy rules are two closed vocabularies with one reader. A key with
    // no entry renders as itself — it is the string an operator greps.
    expect(policyReasonLabel("policy_identity_retired")).toBe("该案例的策略身份已退役");
    expect(policyReasonLabel("smart_money_momentum_long")).toBe("聪明钱动量 · 做多");
    expect(policyReasonLabel("a_rule_nobody_translated")).toBe("a_rule_nobody_translated");
    expect(policyReasonLabel(null)).toBe("—");
    // A reason the deleted execution owner used to write has no translation, because nothing writes it.
    expect(policyReasonLabel("capability_mismatch")).toBe("capability_mismatch");
  });

  it("prints a signed percentage from basis points, and a dash for an unmeasured one", () => {
    expect(bpsPercent(187)).toBe("+1.87%");
    expect(bpsPercent(-312)).toBe("−3.12%");
    expect(bpsPercent(null)).toBe("—");
  });
});
