import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, extname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const webRoot = join(dirname(fileURLToPath(import.meta.url)), "../..");
const srcRoot = join(webRoot, "src");
const sourceExtensions = new Set([".ts", ".tsx"]);

describe("shared primitive imports", () => {
  it("flags direct Radix Tabs package imports outside the shared wrapper", () => {
    expect(
      disallowedTabsImportMessages(
        "src/features/news/BadTabsImport.tsx",
        'import * as Tabs from "@radix-ui/react-tabs";',
      ),
    ).toEqual([
      'src/features/news/BadTabsImport.tsx: import * as Tabs from "@radix-ui/react-tabs"',
    ]);
  });

  it("flags named Tabs imports from aggregate Radix outside the shared wrapper", () => {
    expect(
      disallowedTabsImportMessages(
        "src/features/news/BadTabsImport.tsx",
        'import { Tabs as RadixTabs } from "radix-ui";',
      ),
    ).toEqual([
      'src/features/news/BadTabsImport.tsx: import { Tabs as RadixTabs } from "radix-ui"',
    ]);
  });

  /*
   * There is no Tabs wrapper any more: the v6 console's only segmented control is the feed's task tabs, which
   * are four buttons with `role="tab"` and no panels to switch. Radix Tabs would bring a roving tabindex and a
   * panel model that surface does not have, so nothing may import it — there is no sanctioned wrapper to
   * import it through either.
   */
  it("keeps Radix Tabs out of the console entirely", () => {
    const offenders = collectFiles(srcRoot)
      .filter((path) => sourceExtensions.has(extname(path)))
      .flatMap((path) =>
        disallowedTabsImportMessages(relative(webRoot, path), readFileSync(path, "utf8")),
      );

    expect(offenders).toEqual([]);
    expect(existsSync(join(srcRoot, "shared/ui/tabs.tsx"))).toBe(false);
  });
});

function disallowedTabsImportMessages(relativePath: string, text: string): string[] {
  const importDeclarations = [
    ...text.matchAll(/import\s+(?:type\s+)?([\s\S]*?)\s+from\s+["']([^"']+)["']/g),
  ];

  return importDeclarations.flatMap((match) => {
    const [, importClause, source] = match;
    const importText = match[0].replace(/\s+/g, " ");

    if (source === "@radix-ui/react-tabs") {
      return [`${relativePath}: ${importText}`];
    }

    if (source !== "radix-ui") {
      return [];
    }

    if (!importsNamedTabs(importClause)) {
      return [];
    }

    return [`${relativePath}: ${importText}`];
  });
}

function importsNamedTabs(importClause: string): boolean {
  const namedBlock = importClause.match(/\{([\s\S]*?)\}/);

  if (!namedBlock) {
    return false;
  }

  return namedBlock[1]
    .split(",")
    .map((specifier) =>
      specifier
        .trim()
        .split(/\s+as\s+/i)[0]
        ?.trim(),
    )
    .includes("Tabs");
}

function collectFiles(root: string): string[] {
  return readdirSync(root).flatMap((entry) => {
    const path = join(root, entry);
    return statSync(path).isDirectory() ? collectFiles(path) : [path];
  });
}
