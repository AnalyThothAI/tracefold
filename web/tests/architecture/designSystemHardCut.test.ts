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
      // Two colour axes that never share a hue (#74): direction is red/green, the pipeline is not.
      "--dir-bullish:",
      "--dir-bearish:",
      "--signal-done:",
      "--signal-caution:",
      "--signal-alert:",
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

  it("keeps shared UI free of retired Search dossier and Token Case primitives", () => {
    for (const removed of [
      "shared/ui/ResearchPrimitives.tsx",
      "shared/ui/ResearchPrimitives.css",
      "shared/ui/researchLanguage.ts",
      "shared/ui/case-file",
      "shared/ui/toggle-group.tsx",
    ]) {
      expect(existsSync(join(srcRoot, removed)), `${removed} must stay deleted`).toBe(false);
    }
    const sharedUiCss = collectFiles(join(srcRoot, "shared/ui"))
      .filter((path) => extname(path) === ".css")
      .map((path) => readFileSync(path, "utf8"))
      .join("\n");
    expect(sharedUiCss).not.toMatch(/\.research-|\.token-case|\.case-file/);
  });

  it("exposes the two News destinations with no nested tree", () => {
    const items = APP_NAVIGATION_GROUPS.flatMap((group) => group.items);
    // News is the whole product (#68), so its feed and its pipeline status are the navigation (#82). Event
    // detail is not a destination of its own — it highlights the feed it came from.
    expect(items.map((item) => item.to)).toEqual(["/news", "/news/review", "/news/status"]);
    expect(items.find((item) => item.to === "/news")?.isActive("/news/events/ev-1")).toBe(true);
    expect(items.find((item) => item.to === "/news")?.isActive("/news/status")).toBe(false);
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
    expect(topbar).not.toMatch(/socketStatus|lastSocketMessageAt|providers|main-route-button/);
    expect(topbar).toContain('"news search"');
    expect(topbar).toContain("搜索新闻事件 / 标题 / 资产");
  });

  it("assigns every supported route family to a page archetype", () => {
    // The archetype is declared where the surface is composed and applied by the shared shell, which is the
    // only place `data-page-archetype` is written.
    const owners = {
      case: ["features/news/ui/detail/NewsEventDetailPage.tsx"],
      scan: [
        "features/news/ui/feed/NewsFeedPage.tsx",
        "features/news/ui/status/NewsStatusPage.tsx",
      ],
    } as const;

    for (const [archetype, paths] of Object.entries(owners)) {
      for (const path of paths) {
        expect(readSource(path)).toContain(`archetype="${archetype}"`);
      }
    }
    expect(readSource("features/news/ui/feed/NewsFeedPage.tsx")).not.toContain('archetype="case"');
    expect(readSource("features/news/ui/chrome/NewsChrome.tsx")).toContain(
      "data-page-archetype={archetype}",
    );
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
