import { commandProgress, signalProgress } from "@features/trading/model/executionProgress";
import {
  tradingCommandFixture,
  tradingObservationFixture,
  tradingSignalFixture,
} from "@tests/fixtures/tradingFixture";
import { describe, expect, it } from "vitest";

describe("executionProgress", () => {
  it("keeps a newly persisted command at the persistence boundary", () => {
    const progress = commandProgress(tradingCommandFixture(), []);

    expect(progress.label).toBe("PERSISTED");
    expect(progress.steps.map((step) => step.tone)).toEqual([
      "complete",
      "current",
      "pending",
      "pending",
    ]);
  });

  it("renders each observed flatten stage without promoting it to a later fact", () => {
    const command = tradingCommandFixture({ action: "flatten" });
    const runtime = tradingObservationFixture({
      command_id: command.command_id,
      normalized_kind: "readiness",
      summary: { control_stage: "runtime_accepted" },
    });
    const order = tradingObservationFixture({
      command_id: command.command_id,
      event_id: "1".repeat(64),
      normalized_kind: "order",
      summary: { status: "accepted" },
    });
    const fill = tradingObservationFixture({
      command_id: command.command_id,
      event_id: "2".repeat(64),
      normalized_kind: "fill",
    });

    expect(commandProgress(command, [runtime]).label).toBe("RUNTIME ACCEPTED");
    expect(commandProgress(command, [runtime, order]).label).toBe("ORDER ACCEPTED");
    expect(commandProgress(command, [runtime, order, fill]).label).toBe("FILL OBSERVED");
    expect(
      commandProgress(
        {
          ...command,
          disposition: "completed",
          disposition_reason: "binance_account_flat",
        },
        [runtime, order, fill],
      ).label,
    ).toBe("ACCOUNT FLAT · PROVEN");
  });

  it("keeps manual entry in the venue lifecycle through position opened", () => {
    const command = tradingCommandFixture({ action: "manual_entry", disposition: "accepted" });
    const order = tradingObservationFixture({
      command_id: command.command_id,
      normalized_kind: "order",
      summary: { leg: "entry", status: "accepted" },
    });
    const fill = tradingObservationFixture({
      command_id: command.command_id,
      event_id: "6".repeat(64),
      normalized_kind: "fill",
      summary: { leg: "entry" },
    });
    const position = tradingObservationFixture({
      command_id: command.command_id,
      event_id: "7".repeat(64),
      normalized_kind: "position",
      summary: { status: "opened" },
    });

    expect(commandProgress(command, []).label).toBe("RUNTIME ACCEPTED");
    expect(commandProgress(command, [order]).label).toBe("ORDER ACCEPTED");
    expect(commandProgress(command, [order, fill]).label).toBe("FILL OBSERVED");
    expect(commandProgress(command, [order, fill, position]).label).toBe("POSITION OPENED");
    expect(
      commandProgress({ ...command, disposition_reason: "unknown_query_first" }, []).label,
    ).toBe("RUNTIME AMBIGUOUS");
  });

  it("keeps rejected and expired commands distinct", () => {
    expect(commandProgress(tradingCommandFixture({ disposition: "rejected" }), []).label).toBe(
      "RUNTIME REJECTED",
    );
    expect(
      commandProgress(
        tradingCommandFixture({ disposition: "rejected", disposition_reason: "expired" }),
        [],
      ).label,
    ).toBe("EXPIRED");
    expect(commandProgress(tradingCommandFixture({ expired: true }), []).label).toBe("EXPIRED");
  });

  it("renders signal progress only from correlated observations", () => {
    const signal = tradingSignalFixture();
    const accepted = tradingObservationFixture({
      normalized_kind: "signal_disposition",
      signal_id: signal.signal_id,
      summary: { disposition: "accepted" },
    });
    const order = tradingObservationFixture({
      event_id: "3".repeat(64),
      normalized_kind: "order",
      signal_id: signal.signal_id,
      summary: { status: "accepted" },
    });
    const fill = tradingObservationFixture({
      event_id: "4".repeat(64),
      normalized_kind: "fill",
      signal_id: signal.signal_id,
    });
    const position = tradingObservationFixture({
      event_id: "5".repeat(64),
      normalized_kind: "position",
      signal_id: signal.signal_id,
      summary: { status: "opened" },
    });

    expect(signalProgress(signal, []).label).toBe("PERSISTED");
    expect(signalProgress(signal, [accepted]).label).toBe("RUNTIME ACCEPTED");
    expect(signalProgress(signal, [accepted, order]).label).toBe("ORDER ACCEPTED");
    expect(signalProgress(signal, [accepted, order, fill]).label).toBe("FILL OBSERVED");
    expect(signalProgress(signal, [accepted, order, fill, position]).label).toBe("POSITION OPENED");
  });

  it("keeps rejected and expired signals distinct", () => {
    const signal = tradingSignalFixture();
    const rejected = tradingObservationFixture({
      normalized_kind: "signal_disposition",
      signal_id: signal.signal_id,
      summary: { disposition: "rejected" },
    });

    expect(signalProgress(signal, [rejected]).label).toBe("RUNTIME REJECTED");
    expect(
      signalProgress(signal, [
        tradingObservationFixture({
          normalized_kind: "signal_disposition",
          signal_id: signal.signal_id,
          summary: { disposition: "expired" },
        }),
      ]).label,
    ).toBe("EXPIRED");
    expect(signalProgress({ ...signal, expired: true }, []).label).toBe("EXPIRED");
  });

  it.each(["unknown_query_first", "replayed_query_first"])(
    "keeps %s signal dispositions ambiguous instead of manufacturing a rejection",
    (runtimeDisposition) => {
      const signal = tradingSignalFixture();
      const ambiguous = tradingObservationFixture({
        normalized_kind: "signal_disposition",
        signal_id: signal.signal_id,
        summary: { disposition: runtimeDisposition },
      });

      const progress = signalProgress(signal, [ambiguous]);

      expect(progress.label).toBe("RUNTIME AMBIGUOUS");
      expect(progress.steps[1]).toEqual({ label: "Runtime 结果不确定", tone: "ambiguous" });
      expect(progress.steps[3].tone).toBe("ambiguous");
    },
  );
});
