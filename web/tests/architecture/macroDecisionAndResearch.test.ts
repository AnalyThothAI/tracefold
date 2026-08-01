import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const webRoot = join(dirname(fileURLToPath(import.meta.url)), "../..");
const macroRoot = join(webRoot, "src/features/macro");
const srcRoot = join(webRoot, "src");

describe("Macro current-module hard cut", () => {
  it("owns only typed current-module pages and charts", () => {
    const files = collectFiles(macroRoot).map((path) => relative(macroRoot, path));

    expect(files.filter((path) => path.endsWith(".tsx"))).toEqual([
      "ui/MacroCharts.tsx",
      "ui/MacroDecisionPage.tsx",
      "ui/MacroModuleSections.tsx",
    ]);
    expect(macroText()).not.toMatch(/MacroThesis|thesis_context|\/api\/macro\/research/);
  });

  it("wires the overview and six typed module routes", () => {
    const router = readSource("routes/router.tsx");

    expect(router.match(/path: "macro"/g)).toHaveLength(1);
    expect(router).not.toContain('path: "macro/research"');
    for (const route of [
      '["rates-fed", "rates_fed"]',
      '["economy-inflation", "economy_inflation"]',
      '["liquidity-funding", "liquidity_funding"]',
      '["credit", "credit"]',
      '["volatility", "volatility"]',
      '["cross-asset", "cross_asset"]',
    ]) {
      expect(router).toContain(route);
    }
  });

  it("keeps semantic charts and deterministic module evidence", () => {
    const sections = readFileSync(join(macroRoot, "ui/MacroModuleSections.tsx"), "utf8");

    expect(sections).toContain("useHashSection");
    expect(sections).toContain("MacroTimeSeriesChart");
    expect(sections).toContain("module.summary.top_changes.map");
    expect(sections).toContain("module.contradictions.map");
    expect(sections).toContain("module.falsifiers.map");
    expect(sections).toContain("module.next_checkpoints.map");
  });
});

function readSource(path: string): string {
  return readFileSync(join(srcRoot, path), "utf8");
}

function macroText(): string {
  return collectFiles(macroRoot)
    .filter((path) => /\.(?:ts|tsx)$/.test(path))
    .map((path) => readFileSync(path, "utf8"))
    .join("\n");
}

function collectFiles(root: string): string[] {
  return readdirSync(root)
    .flatMap((entry) => {
      const path = join(root, entry);
      return statSync(path).isDirectory() ? collectFiles(path) : [path];
    })
    .sort();
}
