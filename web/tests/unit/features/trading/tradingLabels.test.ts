import {
  bpsPercent,
  caseClock,
  entryBlockReasonLabel,
  moneyLabel,
  nsClock,
  policyLabel,
  policyReasonLabel,
  signalDispositionLabel,
} from "@features/trading/model/tradingLabels";
import { describe, expect, it } from "vitest";

describe("Alpha labels", () => {
  it("names the current Alpha identity without falling back to a historical capital label", () => {
    expect(policyLabel("source_native_oi_smart_money_long_v4")).toBe("来源原生 OI × 聪明钱 · 做多");
  });

  it("renders a retired policy identity as itself rather than translating what nothing writes", () => {
    /*
     * #528 PR-2 deleted the seven historical entries. Every surface that calls this reads a rolling 24 h
     * window and no writer has emitted them since V4, so a translation for them was a claim about the
     * ledger the ledger no longer makes. The raw id is what an operator greps anyway.
     */
    expect(policyLabel("binance_oi_smart_money_long_v2")).toBe("binance_oi_smart_money_long_v2");
    expect(policyLabel("oi_momentum_v1")).toBe("oi_momentum_v1");
    expect(policyLabel("source_native_oi_smart_money_long_v3")).toBe(
      "source_native_oi_smart_money_long_v3",
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
  it("translates why entries are blocked, from the projection's words and the Runtime's alike", () => {
    // `execution_status.py` owns the first, `oi_runtime/state.py` the second; both reach the same field.
    expect(entryBlockReasonLabel("entries_paused")).toBe("开仓已暂停");
    expect(entryBlockReasonLabel("startup_reconciliation_unproven")).toBe("启动对账尚未完成");
    expect(entryBlockReasonLabel("a_gate_nobody_translated")).toBe("a_gate_nobody_translated");
    // No reason at all is the armed state, not a missing translation.
    expect(entryBlockReasonLabel(null)).toBe("允许新增 exposure");
  });

  it("translates one Signal's durable disposition without collapsing accept and refuse", () => {
    expect(signalDispositionLabel("accepted")).toBe("已受理");
    expect(signalDispositionLabel("instrument_unmapped")).toBe("运行时目录里没有这个市场");
    expect(signalDispositionLabel("expired")).toBe("Signal 已过期");
    expect(signalDispositionLabel("aggregate_risk_limit")).toBe("总风险已达上限");
    // The entry path forwards the readiness gate's own word, so that vocabulary resolves here too.
    expect(signalDispositionLabel("unexpected_exposure")).toBe("出现无主敞口");
    expect(signalDispositionLabel("a_refusal_nobody_translated")).toBe(
      "a_refusal_nobody_translated",
    );
    // Undisposed is a real state of a persisted Signal, and it is not a refusal.
    expect(signalDispositionLabel(null)).toBe("等待 Runtime");
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
