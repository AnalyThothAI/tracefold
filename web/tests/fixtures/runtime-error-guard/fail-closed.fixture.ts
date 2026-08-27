import { it } from "vitest";

import { allowConsoleError } from "../../support/runtimeErrorGuard";

it("allows the documented console error only in this case", () => {
  allowConsoleError({
    match: "case-local console error",
    reason: "This first case deliberately exercises an error path.",
  });
  console.error("case-local console error");
});

it("does not inherit the prior case allowance", () => {
  console.error("case-local console error");
});

it("fails on an unhandled rejection", () => {
  void Promise.reject(new Error("fixture rejection failure"));
});

it("rejects an undocumented allowance", () => {
  allowConsoleError({ match: "never emitted", reason: "  " });
});
