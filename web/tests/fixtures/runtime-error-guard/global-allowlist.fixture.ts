import { it } from "vitest";

import { allowConsoleError } from "../../support/runtimeErrorGuard";

allowConsoleError({
  match: "never emitted",
  reason: "This fixture proves file-global allowlists are rejected.",
});

it("cannot inherit a file-global allowlist", () => undefined);
