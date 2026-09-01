import { currentExecutionHeartbeat } from "@features/trading/model/executionFreshness";
import { describe, expect, it } from "vitest";

describe("currentExecutionHeartbeat", () => {
  const measuredAtMs = 1_000_000;

  it("expires from the server heartbeat age instead of the HTTP response clock", () => {
    expect(
      currentExecutionHeartbeat({
        heartbeatAtNs: (measuredAtMs - 4_000) * 1_000_000,
        measuredAtMs,
        nowMs: measuredAtMs + 1_000,
        queryHealthy: true,
      }),
    ).toBe(true);
    expect(
      currentExecutionHeartbeat({
        heartbeatAtNs: (measuredAtMs - 4_000) * 1_000_000,
        measuredAtMs,
        nowMs: measuredAtMs + 1_001,
        queryHealthy: true,
      }),
    ).toBe(false);
  });

  it("fails closed on refresh failure and invalid clocks", () => {
    expect(
      currentExecutionHeartbeat({
        heartbeatAtNs: measuredAtMs * 1_000_000,
        measuredAtMs,
        nowMs: measuredAtMs,
        queryHealthy: false,
      }),
    ).toBe(false);
    expect(
      currentExecutionHeartbeat({
        heartbeatAtNs: (measuredAtMs + 1) * 1_000_000,
        measuredAtMs,
        nowMs: measuredAtMs,
        queryHealthy: true,
      }),
    ).toBe(false);
  });
});
