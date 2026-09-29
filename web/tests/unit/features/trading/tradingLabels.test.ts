import {
  EXECUTION_STAGE_ZH,
  EXIT_REASON_ZH,
  admissionReasonLabel,
  admissionStatusLabel,
  bpsPercent,
  caseClock,
  entryBlockReasonLabel,
  holdingLabel,
  moneyLabel,
  moneyTone,
  nsClock,
  policyLabel,
  policyReasonLabel,
  signalDispositionLabel,
} from "@features/trading/model/tradingLabels";
import { describe, expect, it } from "vitest";

describe("Alpha labels", () => {
  it("names the current Alpha identity without falling back to a historical capital label", () => {
    expect(policyLabel("source_native_oi_smart_money_long_v5")).toBe("来源原生 OI × 聪明钱 · 做多");
    // A Case whose manifest names no policy renders as a dash, not as a crash: `/cases` reads the
    // identity out of the frozen manifest now, and the manifest is the only writer of it (#537 PR-3).
    expect(policyLabel(null)).toBe("—");
  });

  it("renders a retired policy identity as itself rather than translating what nothing writes", () => {
    /*
     * #528 PR-2 deleted the seven historical entries. Every surface that calls this reads a rolling 24 h
     * window and no writer has emitted them since V4, so a translation for them was a claim about the
     * ledger the ledger no longer makes. The raw id is what an operator greps anyway. V4 joined them
     * when #537 PR-3 deleted the profit threshold from the policy's identity.
     */
    expect(policyLabel("binance_oi_smart_money_long_v2")).toBe("binance_oi_smart_money_long_v2");
    expect(policyLabel("oi_momentum_v1")).toBe("oi_momentum_v1");
    expect(policyLabel("source_native_oi_smart_money_long_v4")).toBe(
      "source_native_oi_smart_money_long_v4",
    );
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

describe("execution labels", () => {
  it("translates the executor's readiness reasons", () => {
    expect(entryBlockReasonLabel("executor_heartbeat_stale")).toBe("执行器心跳已过期");
    expect(entryBlockReasonLabel("account_reconcile_stale")).toBe("账户签名对账已过期");
    expect(entryBlockReasonLabel("entries_paused")).toBe("开仓已暂停");
    expect(entryBlockReasonLabel("unexpected_exposure")).toBe("账户检查发现异常");
    expect(entryBlockReasonLabel("a_gate_nobody_translated")).toBe("a_gate_nobody_translated");
    expect(entryBlockReasonLabel(null)).toBe("允许新增 exposure");
  });

  it("translates one Signal's durable disposition without collapsing accept and refuse", () => {
    // Admission precedes the venue call; the order stage carries its result.
    expect(signalDispositionLabel("accepted")).toBe("执行器已受理");
    expect(signalDispositionLabel("execution_venue_unlisted")).toBe("DEMO 场所未列出该合约");
    expect(signalDispositionLabel("market_lot_or_notional")).toBe(
      "下单数量或名义金额不满足场所规则",
    );
    expect(signalDispositionLabel("expired")).toBe("Signal 已过期");
    expect(signalDispositionLabel("spread")).toBe("点差超限");
    expect(signalDispositionLabel("unexpected_exposure")).toBe("账户检查发现异常");
    expect(signalDispositionLabel("a_refusal_nobody_translated")).toBe(
      "a_refusal_nobody_translated",
    );
    expect(signalDispositionLabel(null)).toBe("等待执行器");
  });

  it("prints a stored decimal as money and a nanosecond clock as the lane's own time", () => {
    expect(moneyLabel("-14.92274518")).toBe("−$14.92");
    expect(moneyLabel("0")).toBe("$0.00");
    expect(moneyLabel(null)).toBe("—");
    // Not a number the ledger can mean anything by; the cell says nothing rather than `$NaN`.
    expect(moneyLabel("unavailable")).toBe("—");
    // The nanosecond clock is the Case clock, truncated — one format for both ledgers.
    const at = Date.parse("2026-08-25T12:00:00Z");
    expect(nsClock(at * 1_000_000)).toBe(caseClock(at));
    expect(nsClock(at * 1_000_000)).toMatch(/^\d{2}-\d{2} \d{2}:\d{2}$/);
    expect(nsClock(null)).toBe("—");
  });
});

describe("desk labels", () => {
  it("carries the executor's admission, uncertain submission and venue stages", () => {
    expect(Object.keys(EXECUTION_STAGE_ZH).sort()).toEqual(
      [
        "accepted",
        "closed",
        "expired",
        "filled",
        "ordered",
        "pending",
        "protected",
        "rejected",
        "submission_unknown",
      ].sort(),
    );
    expect(EXIT_REASON_ZH.external).toBe("外部平仓");
    expect(EXIT_REASON_ZH.entry_rejected).toBe("入场被场所拒绝");
    expect(EXIT_REASON_ZH.protection_failed).toBe("保护失败后平仓");
    for (const reason of [
      "stop_filled",
      "take_profit",
      "time_exit",
      "operator_flatten",
      "external",
    ]) {
      expect(EXIT_REASON_ZH[reason]).toBeTruthy();
    }
  });

  it("names an admission status and its refusal, and never crashes on an unknown key", () => {
    expect(admissionStatusLabel("CASE_CREATED")).toBe("成案");
    expect(admissionStatusLabel("REJECTED")).toBe("准入拒绝");
    expect(admissionStatusLabel("EXPIRED")).toBe("过期");
    expect(admissionStatusLabel("A_STATUS_NOBODY_TRANSLATED")).toBe("A_STATUS_NOBODY_TRANSLATED");
    expect(admissionReasonLabel("oi_value_below_floor")).toBe("持仓价值低于地板");
    expect(admissionReasonLabel("instrument_unmapped")).toBe("无可执行路由");
    expect(admissionReasonLabel("source_not_live")).toBe("来源未上线");
    expect(admissionReasonLabel("a_gate_nobody_translated")).toBe("a_gate_nobody_translated");
    // `CASE_CREATED` carries no reason, and the absence is a dash rather than an invented one.
    expect(admissionReasonLabel(null)).toBe("—");
  });

  it("measures a holding interval between the two clocks the ledger stores, and only those", () => {
    const filled = 1_700_000_000_000_000_000;
    expect(holdingLabel(filled, filled)).toBe("0s");
    expect(holdingLabel(filled, filled + 92_500_000_000)).toBe("1m33s");
    expect(holdingLabel(filled, filled + 4 * 3_600_000_000_000 + 600_000_000_000)).toBe("4h10m");
    // One clock missing means the entry is still open or never filled. It is not measured against `now`.
    expect(holdingLabel(null, filled)).toBe("—");
    expect(holdingLabel(filled, null)).toBe("—");
    expect(holdingLabel(null, null)).toBe("—");
    // A close that precedes its own fill is not an interval; the cell says nothing rather than a negative.
    expect(holdingLabel(filled, filled - 1_000_000_000)).toBe("—");
  });

  it("puts a realized result on the market axis, and leaves an unmeasured one off it", () => {
    // `tokens.css` reads red as bullish; a profit is what a long that worked produced (#604 T4).
    expect(moneyTone("110.33")).toBe("profit");
    expect(moneyTone("-11.04")).toBe("loss");
    expect(moneyTone("0")).toBeUndefined();
    expect(moneyTone(null)).toBeUndefined();
    expect(moneyTone("unavailable")).toBeUndefined();
  });
});
