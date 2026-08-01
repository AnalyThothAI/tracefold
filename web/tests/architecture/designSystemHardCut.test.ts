import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, extname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { APP_NAVIGATION_GROUPS } from "../../src/features/cockpit/ui/appNavigation";

const srcRoot = join(dirname(fileURLToPath(import.meta.url)), "../../src");
const webRoot = join(srcRoot, "..");
const sourceExtensions = new Set([".css", ".ts", ".tsx"]);

describe("Tracefold design-system hard cut", () => {
  it("defines the sole semantic token contract", () => {
    const tokens = readSource("styles/tokens.css");
    const required = [
      "--surface-canvas:",
      "--surface-root:",
      "--surface-panel:",
      "--text-primary:",
      "--text-muted:",
      "--border-subtle:",
      "--accent-primary:",
      "--signal-positive:",
      "--signal-caution:",
      "--signal-negative:",
      "--signal-info:",
      "--focus-ring:",
      "--shell-topbar-height:",
      "--shell-mobile-topbar-height:",
    ];
    for (const token of required) {
      expect(tokens).toContain(token);
      expect(
        tokens.match(new RegExp(token.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "g")),
      ).toHaveLength(1);
    }
  });

  it("removes old brand, component, selector and token names from production", () => {
    const files = collectFiles(srcRoot).filter((path) => sourceExtensions.has(extname(path)));
    const offenders = files.flatMap((path) => {
      const source = readFileSync(path, "utf8");
      return [
        /\bobsidian\b/i,
        /\bgmgn\.intel\b/i,
        /\bods-/,
        /--(?:void|obsidian|slab(?:-[23])?|bone(?:-2)?|ash|dim|opportunity|health|risk)(?:\b|:)/,
      ]
        .filter((pattern) => pattern.test(source))
        .map((pattern) => `${relative(webRoot, path)}: ${pattern.source}`);
    });

    expect(offenders).toEqual([]);
    for (const removed of [
      "shared/ui/obsidian.tsx",
      "shared/ui/obsidian.css",
      "shared/ui/obsidianRecords.css",
      "shared/ui/obsidianLanguage.ts",
    ]) {
      expect(existsSync(join(srcRoot, removed))).toBe(false);
    }
  });

  it("keeps shared research primitives independent of feature CSS", () => {
    const primitives = readSource("shared/ui/ResearchPrimitives.tsx");
    const css = readSource("shared/ui/ResearchPrimitives.css");

    for (const component of [
      "ResearchPanel",
      "ResearchHeader",
      "ResearchSection",
      "ResearchFieldGrid",
      "ResearchTag",
      "ResearchMark",
    ]) {
      expect(primitives).toContain(`function ${component}`);
    }
    expect([...css.matchAll(/#[0-9a-fA-F]{6}/g)]).toEqual([]);
  });

  it("exposes four primary research destinations with no duplicate Macro tree", () => {
    const items = APP_NAVIGATION_GROUPS.flatMap((group) => group.items);
    expect(items.map((item) => item.to)).toEqual(["/", "/stocks", "/news", "/macro"]);
    expect(items.flatMap((item) => item.children ?? [])).toEqual([]);

    const sidebar = readSource("features/cockpit/ui/AppSidebar.tsx");
    expect(sidebar).toContain("Tracefold");
    expect(sidebar).toContain("Research Workbench");
    expect(sidebar).not.toMatch(/Desk status|Live desk|facts online/);
  });

  it("keeps normal health quiet and exposes anomalies without a browser Ops route", () => {
    const topbar = readSource("features/cockpit/ui/CockpitTopbar.tsx");

    expect(topbar).toContain("healthAnomaly");
    expect(topbar).toContain("topbar-anomaly");
    expect(topbar).not.toContain("opsPath");
    expect(topbar).not.toContain("StatusPills");
    expect(topbar).not.toContain("WsStatusBeacon");
  });

  it("assigns every supported route family to a page archetype", () => {
    const owners = {
      case: ["features/search/ui/SearchIntelPage.tsx", "shared/ui/case-file/TokenCasePanel.tsx"],
      decision: ["features/macro/ui/MacroDecisionPage.tsx"],
      scan: [
        "features/live/ui/LivePage.tsx",
        "features/stocks/ui/StocksRadarPage.tsx",
        "features/news/NewsPage.tsx",
      ],
    } as const;

    for (const [archetype, paths] of Object.entries(owners)) {
      for (const path of paths) {
        expect(readSource(path)).toContain(`data-page-archetype="${archetype}"`);
      }
    }
    expect(readSource("features/news/NewsPage.tsx")).toContain('data-page-archetype="case"');
  });

  it("keeps shell geometry centralized and route content scrollable", () => {
    const cockpitCss = readSource("features/cockpit/ui/cockpitShell.css");
    const shellContractCss = readSource("features/cockpit/ui/cockpitShellContract.css");

    expect(cockpitCss).not.toContain(":root");
    expect(shellContractCss).not.toContain(":root");
    expect(cockpitCss).toMatch(/\.center-column\s*{[^}]*overflow:\s*auto;/s);
  });
});

function readSource(path: string): string {
  return readFileSync(join(srcRoot, path), "utf8");
}

function collectFiles(root: string): string[] {
  return readdirSync(root).flatMap((entry) => {
    const path = join(root, entry);
    return statSync(path).isDirectory() ? collectFiles(path) : [path];
  });
}
