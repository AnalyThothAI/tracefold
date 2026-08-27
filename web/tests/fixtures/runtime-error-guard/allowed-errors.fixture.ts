import { it } from "vitest";

import { allowConsoleError, allowUnhandledRejection } from "../../support/runtimeErrorGuard";

it("allows documented runtime errors in this case", () => {
  allowConsoleError({
    match: "fixture permitted console error",
    reason: "This fixture proves the console escape hatch is explicit.",
  });
  allowUnhandledRejection({
    match: "fixture permitted rejection",
    reason: "This fixture proves the rejection escape hatch is explicit.",
  });

  console.error("fixture permitted console error");
  void Promise.reject(new Error("fixture permitted rejection"));
});
