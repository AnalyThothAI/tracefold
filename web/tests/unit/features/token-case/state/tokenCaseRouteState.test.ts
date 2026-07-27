import {
  parseTokenCaseRouteState,
  serializeTokenCaseRouteState,
} from "@features/token-case/state/tokenCaseRouteState";
import { describe, expect, it } from "vitest";

describe("tokenCaseRouteState", () => {
  it("uses defaults when params are omitted", () => {
    expect(parseTokenCaseRouteState(new URLSearchParams())).toEqual({
      window: "24h",
    });
  });

  it("accepts the supported window and ignores retired scope and sort params", () => {
    expect(
      parseTokenCaseRouteState(new URLSearchParams("window=24h&scope=watched&postSort=watched")),
    ).toEqual({
      window: "24h",
    });
  });

  it("falls back to defaults for invalid enum params", () => {
    expect(
      parseTokenCaseRouteState(new URLSearchParams("window=7d&scope=private&postSort=quality")),
    ).toEqual({
      window: "24h",
    });
  });

  it("omits defaults when serializing", () => {
    expect(
      serializeTokenCaseRouteState({
        window: "24h",
      }).toString(),
    ).toBe("");
  });

  it("serializes only the window in stable order", () => {
    expect(
      serializeTokenCaseRouteState({
        window: "1h",
      }).toString(),
    ).toBe("window=1h");
  });
});
