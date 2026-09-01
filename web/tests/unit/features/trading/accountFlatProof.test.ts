import { currentAccountFlatProof } from "@features/trading/model/accountFlatProof";
import { describe, expect, it } from "vitest";

describe("currentAccountFlatProof", () => {
  const measuredAtMs = 1_000_000;

  it("expires the server proof at the private-reconciliation budget", () => {
    expect(
      currentAccountFlatProof({
        accountFlatProven: true,
        measuredAtMs,
        nowMs: measuredAtMs + 10_000,
        queryHealthy: true,
        reconciliationAgeMs: 0,
      }),
    ).toBe(true);
    expect(
      currentAccountFlatProof({
        accountFlatProven: true,
        measuredAtMs,
        nowMs: measuredAtMs + 10_001,
        queryHealthy: true,
        reconciliationAgeMs: 0,
      }),
    ).toBe(false);
  });

  it("does not preserve flat proof across a failed refresh or future server clock", () => {
    expect(
      currentAccountFlatProof({
        accountFlatProven: true,
        measuredAtMs,
        nowMs: measuredAtMs,
        queryHealthy: false,
        reconciliationAgeMs: 0,
      }),
    ).toBe(false);
    expect(
      currentAccountFlatProof({
        accountFlatProven: true,
        measuredAtMs: measuredAtMs + 1,
        nowMs: measuredAtMs,
        queryHealthy: true,
        reconciliationAgeMs: 0,
      }),
    ).toBe(false);
  });

  it("uses the server reconciliation age instead of restarting the proof budget", () => {
    expect(
      currentAccountFlatProof({
        accountFlatProven: true,
        measuredAtMs,
        nowMs: measuredAtMs + 1_000,
        queryHealthy: true,
        reconciliationAgeMs: 9_000,
      }),
    ).toBe(true);
    expect(
      currentAccountFlatProof({
        accountFlatProven: true,
        measuredAtMs,
        nowMs: measuredAtMs + 1_001,
        queryHealthy: true,
        reconciliationAgeMs: 9_000,
      }),
    ).toBe(false);
    expect(
      currentAccountFlatProof({
        accountFlatProven: true,
        measuredAtMs,
        nowMs: measuredAtMs,
        queryHealthy: true,
        reconciliationAgeMs: null,
      }),
    ).toBe(false);
  });
});
