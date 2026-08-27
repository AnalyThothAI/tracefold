import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const srcRoot = join(dirname(fileURLToPath(import.meta.url)), "../../src");

describe("Tracefold design-system contract", () => {
  it("defines each semantic color axis once in the token owner", () => {
    const tokens = readFileSync(join(srcRoot, "styles/tokens.css"), "utf8");
    const semanticTokens = [
      "--surface-canvas",
      "--surface-panel",
      "--text-primary",
      "--text-muted",
      "--border-subtle",
      "--accent-primary",
      "--dir-bullish",
      "--dir-bearish",
      "--signal-done",
      "--signal-caution",
      "--signal-alert",
      "--signal-info",
      "--focus-ring",
    ];

    for (const token of semanticTokens) {
      expect([...tokens.matchAll(new RegExp(`${token}:`, "g"))], token).toHaveLength(1);
    }
  });
});
