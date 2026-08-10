import {
  parseTokenCaseRouteState,
  serializeTokenCaseRouteState,
} from "@features/token-case/state/tokenCaseRouteState";
import { describe, expect, it } from "vitest";

describe("tokenCaseRouteState", () => {
  it("uses defaults when params are omitted", () => {
    expect(parseTokenCaseRouteState(new URLSearchParams())).toEqual({
      window: "24h",
      focus: null,
      triggerEventId: null,
    });
  });

  it("accepts the supported window and ignores retired scope and sort params", () => {
    expect(
      parseTokenCaseRouteState(new URLSearchParams("window=24h&scope=watched&postSort=watched")),
    ).toEqual({
      window: "24h",
      focus: null,
      triggerEventId: null,
    });
  });

  it("falls back to defaults for invalid enum params", () => {
    expect(
      parseTokenCaseRouteState(new URLSearchParams("window=7d&scope=private&postSort=quality")),
    ).toEqual({
      window: "24h",
      focus: null,
      triggerEventId: null,
    });
  });

  it("omits defaults when serializing", () => {
    expect(
      serializeTokenCaseRouteState({
        window: "24h",
        focus: null,
        triggerEventId: null,
      }).toString(),
    ).toBe("");
  });

  it("round-trips trigger focus after the Case window", () => {
    expect(
      serializeTokenCaseRouteState({
        window: "1h",
        focus: "trigger",
        triggerEventId: "event-1",
      }).toString(),
    ).toBe("window=1h&focus=trigger&trigger_event_id=event-1");
    expect(
      parseTokenCaseRouteState(
        new URLSearchParams("window=1h&focus=trigger&trigger_event_id=event-1"),
      ),
    ).toEqual({ window: "1h", focus: "trigger", triggerEventId: "event-1" });
  });

  it("ignores incomplete trigger focus", () => {
    expect(parseTokenCaseRouteState(new URLSearchParams("focus=trigger"))).toEqual({
      window: "24h",
      focus: null,
      triggerEventId: null,
    });
  });
});
