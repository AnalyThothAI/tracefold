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
      }),
    ).toBe(true);
    expect(
      currentAccountFlatProof({
        accountFlatProven: true,
        measuredAtMs,
        nowMs: measuredAtMs + 10_001,
        queryHealthy: true,
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
      }),
    ).toBe(false);
    expect(
      currentAccountFlatProof({
        accountFlatProven: true,
        measuredAtMs: measuredAtMs + 1,
        nowMs: measuredAtMs,
        queryHealthy: true,
      }),
    ).toBe(false);
  });
});
