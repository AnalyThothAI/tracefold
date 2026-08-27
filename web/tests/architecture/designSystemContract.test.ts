import { readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, extname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

import postcss from "postcss";
import { describe, expect, it } from "vitest";

const srcRoot = join(dirname(fileURLToPath(import.meta.url)), "../../src");

describe("Tracefold design-system contract", () => {
  it("defines each semantic color axis once in one global token owner", () => {
    const cssFiles = collectFiles(srcRoot).filter((path) => extname(path) === ".css");
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
      "--signal-neutral",
      "--focus-ring",
    ];
    const owners = new Set<string>();

    for (const token of semanticTokens) {
      const declarations = cssFiles.flatMap((path) => declarationSites(path, token));
      expect(declarations, token).toHaveLength(1);
      expect(declarations[0]?.selector.split(",").map((selector) => selector.trim())).toContain(
        ":root",
      );
      owners.add(declarations[0]?.path ?? "");
    }

    expect([...owners]).toHaveLength(1);
    expect([...owners][0]?.startsWith("styles/")).toBe(true);
  });
});

type DeclarationSite = {
  path: string;
  selector: string;
};

function declarationSites(path: string, property: string): DeclarationSite[] {
  const sites: DeclarationSite[] = [];
  postcss.parse(readFileSync(path, "utf8")).walkDecls(property, (declaration) => {
    sites.push({
      path: relative(srcRoot, path),
      selector: declaration.parent?.type === "rule" ? declaration.parent.selector : "",
    });
  });
  return sites;
}

function collectFiles(root: string): string[] {
  return readdirSync(root).flatMap((entry) => {
    const path = join(root, entry);
    return statSync(path).isDirectory() ? collectFiles(path) : [path];
  });
}
