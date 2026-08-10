import {
  parseTokenCaseRouteState,
  serializeTokenCaseRouteState,
} from "@features/token-case/state/tokenCaseRouteState";
import { tokenTargetPath } from "@shared/routing/paths";
import { describe, expect, it } from "vitest";

describe("token case route state", () => {
  it("defaults token case detail routes to the 24h window", () => {
    expect(parseTokenCaseRouteState(new URLSearchParams()).window).toBe("24h");
    expect(
      serializeTokenCaseRouteState({ window: "24h", focus: null, triggerEventId: null }).toString(),
    ).toBe("");
    expect(
      tokenTargetPath({
        targetType: "Asset",
        targetId: "asset:eip155:1:erc20:0x0fb006edd8d6c128b83d2461dbfe74b318952886",
      }),
    ).toBe("/token/Asset/asset%3Aeip155%3A1%3Aerc20%3A0x0fb006edd8d6c128b83d2461dbfe74b318952886");
  });
});
