import {
  requireStatusData,
  requireWorkerStatusData,
} from "@features/cockpit/model/statusCurrentContract";
import { appStatusFixture } from "@tests/fixtures/appRouteFixtures";
import { describe, expect, it } from "vitest";

describe("statusCurrentContract", () => {
  it("accepts the fixed current status payload", () => {
    expect(requireStatusData(appStatusFixture())).toBeTruthy();
  });

  it("rejects unknown top-level status buckets", () => {
    const payload = {
      ...appStatusFixture(),
      retired_bucket: {},
    };
    expect(() => requireStatusData(payload)).toThrowError(
      "status_current_contract:status.retired_bucket",
    );
  });

  it("rejects status without the required provider states", () => {
    const payload = { ...appStatusFixture() } as Record<string, unknown>;
    delete payload.provider_states;

    expect(() => requireStatusData(payload)).toThrowError(
      "status_current_contract:status.provider_states",
    );
  });

  it("rejects missing worker fields and the retired details bucket", () => {
    const worker = { ...appStatusFixture().workers.collector } as Record<string, unknown>;
    delete worker.last_result;
    expect(() => requireWorkerStatusData(worker)).toThrowError(
      "status_current_contract:worker.last_result",
    );

    expect(() =>
      requireWorkerStatusData({
        ...appStatusFixture().workers.collector,
        details: {},
      }),
    ).toThrowError("status_current_contract:worker.details");
  });
});
