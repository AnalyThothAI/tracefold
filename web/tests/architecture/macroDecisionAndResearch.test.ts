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
      "ui/MacroModuleWorkspace.tsx",
    ]);
    expect(macroText()).not.toMatch(/MacroThesis|thesis_context|\/api\/macro\/research/);
  });

  it("wires the overview and six typed module routes", () => {
    const router = readSource("routes/router.tsx");

    expect(router.match(/path: "macro"/g)).toHaveLength(1);
    expect(router).not.toContain('path: "macro/research"');
    expect(router).not.toContain('from "@features/macro');
    expect(router).not.toContain('["rates-fed", "rates_fed"]');
    expect(router).toContain('lazy: () => import("./macro.route")');
    expect(
      readFileSync(join(macroRoot, "model/macroModules.ts"), "utf8").match(
        /routePath: "\/macro\//g,
      ),
    ).toHaveLength(6);
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

  it("uses generated module contracts without browser-side schema repair", () => {
    const source = macroText();

    expect(source).not.toMatch(
      /\bJsonObject\b|\basRecord(?:s)?\b|\bread(?:Text|Number|Boolean|Record|Records)\b/,
    );
    expect(source).not.toMatch(/indicator-\$\{|release-\$\{|contract-\$\{|`L\$\{|`R\$\{/);
    expect(source).not.toContain('readText(item, "text")');
  });

  it("keeps overview entry free of the detail workbench and retired CSS", () => {
    const files = collectFiles(macroRoot).map((path) => relative(macroRoot, path));
    const page = readFileSync(join(macroRoot, "ui/MacroDecisionPage.tsx"), "utf8");

    expect(page).not.toContain('from "./MacroModuleSections"');
    expect(page).toContain('import("./MacroModuleWorkspace")');
    expect(files).not.toContain("ui/MacroDecisionBrief.css");
    expect(files).not.toContain("ui/MacroDecisionOverview.css");
  });

  it("owns Macro value formatting and chart palette in one presentation module", () => {
    const presentation = readFileSync(join(macroRoot, "model/macroPresentation.ts"), "utf8");
    const sections = readFileSync(join(macroRoot, "ui/MacroModuleSections.tsx"), "utf8");
    const charts = readFileSync(join(macroRoot, "ui/MacroCharts.tsx"), "utf8");

    expect(presentation).toContain("formatMacroValue");
    expect(presentation).toContain("macroChartColor");
    expect(sections).not.toMatch(/function (?:chartColor|unitLabel|formatInstant|formatNumber)\b/);
    expect(charts).not.toMatch(
      /const SERIES_COLORS|function (?:unitLabel|formatInstant|formatNumber)\b/,
    );
  });

  it("does not style retired curve windows", () => {
    const chartCss = readFileSync(join(macroRoot, "ui/MacroCharts.css"), "utf8");

    expect(chartCss).not.toContain('[data-window="1m"]');
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
