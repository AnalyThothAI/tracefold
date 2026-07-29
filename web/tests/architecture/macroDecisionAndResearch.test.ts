import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const webRoot = join(dirname(fileURLToPath(import.meta.url)), "../..");
const macroRoot = join(webRoot, "src/features/macro");
const srcRoot = join(webRoot, "src");

describe("daily macro decision and research hard cut", () => {
  it("owns typed decision sections, one research page, and no generic renderer hierarchy", () => {
    const files = collectFiles(macroRoot).map((path) => relative(macroRoot, path));

    expect(files.filter((path) => path.endsWith(".tsx"))).toEqual([
      "ui/MacroCharts.tsx",
      "ui/MacroDecisionPage.tsx",
      "ui/MacroModuleSections.tsx",
      "ui/MacroResearchAppendices.tsx",
      "ui/MacroResearchDossier.tsx",
      "ui/MacroResearchEvidence.tsx",
      "ui/MacroResearchPage.tsx",
    ]);
    expect(files).not.toContain("shell.ts");
    expect(files.some((path) => path.includes("/pages/"))).toBe(false);
    expect(files.some((path) => /(registry|catalog|universal[-_]?renderer)/i.test(path))).toBe(
      false,
    );
    const sections = readFileSync(join(macroRoot, "ui/MacroModuleSections.tsx"), "utf8");
    expect(macroText()).not.toMatch(/StructuredValue|JSON\.stringify|ObjectTable/);
    expect(sections).toContain("useHashSection");
    expect(sections).toContain("MacroTimeSeriesChart");
    expect(sections).toContain("module.summary.top_changes.map");
    expect(sections).toContain("module.contradictions.map");
    expect(sections).toContain("module.falsifiers.map");
    expect(sections).toContain("module.next_checkpoints.map");
    expect(sections).not.toMatch(
      /module\.(?:contradictions|falsifiers|next_checkpoints)\[0\]|尚无足够历史|等待自然频率积累/,
    );
    expect(sections).not.toMatch(
      /readRecord\(fed,\s*"event_counts"\)|readRecord\(fed,\s*"unique_official_counts"\)|readRecords\(confirmations,\s*"etfs"\)|payload\.normalized(?!_groups)|parseAssets|normalizedSeries|decisionPrimary|intradayProxy|curve-shape-v1/,
    );
  });

  it("wires the overview, research, and six typed module routes", () => {
    const router = readSource("routes/router.tsx");

    expect(router.match(/path: "macro"/g)).toHaveLength(1);
    expect(router).toContain('path: "macro/research"');
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
    expect(router).not.toMatch(/rates-inflation|growth-labor|window=/);
  });

  it("uses typed persisted APIs with no generic evidence or global window contract", () => {
    const source = macroText();
    const paths = [...source.matchAll(/["`]\/api\/macro(?:\/[^"`]*)?["`]/g)].map((match) =>
      match[0].slice(1, -1),
    );

    expect(new Set(paths)).toEqual(
      new Set([
        "/api/macro/overview",
        "/api/macro/rates-fed",
        "/api/macro/economy-inflation",
        "/api/macro/liquidity-funding",
        "/api/macro/credit",
        "/api/macro/volatility",
        "/api/macro/cross-asset",
        "/api/macro/research",
      ]),
    );
    expect(source).not.toMatch(/\/api\/macro\/evidence|history window|[?&]window=/i);
  });

  it("shows one Thesis, deterministic delta, separate quality planes, and immutable history", () => {
    const decision = [
      readFileSync(join(macroRoot, "ui/MacroDecisionPage.tsx"), "utf8"),
      readFileSync(join(macroRoot, "ui/MacroModuleSections.tsx"), "utf8"),
    ].join("\n");
    const research = [
      readFileSync(join(macroRoot, "ui/MacroResearchPage.tsx"), "utf8"),
      readFileSync(join(macroRoot, "ui/MacroResearchDossier.tsx"), "utf8"),
      readFileSync(join(macroRoot, "ui/MacroResearchAppendices.tsx"), "utf8"),
      readFileSync(join(macroRoot, "ui/MacroResearchEvidence.tsx"), "utf8"),
    ].join("\n");

    for (const field of [
      "thesis.mainline",
      "data.mainline_presentation",
      "thesis.core_tensions",
      "thesis.changes_from_prior",
      "data.asset_presentation",
      "data.claim_presentation",
      "asset.horizons",
      "asset.group === group.id",
      "data.live_delta",
      "data.live_delta.mainline_validity",
      "data.live_delta?.scopes",
      "thesis.mainline.falsifiers",
      "thesis.mainline.checkpoints",
      "data.thesis?.gaps",
      "module.contradictions",
      "module.falsifiers",
      "module.next_checkpoints",
      "module.status.coverage",
      "module.status.current_health",
      "module.status.history_depth",
      "module.evidence.dataset_states",
      "module.evidence.latest_facts",
    ]) {
      expect(decision).toContain(field);
    }
    for (const field of [
      "claims.map",
      "thesis.core_tensions.map",
      "claim.asset_implications",
      "claim.module_evidence",
      "thesis.review.findings.map",
      "thesis.provenance",
      "outcomeReplay?.horizons.map",
    ]) {
      expect(research).toContain(field);
    }
    expect(decision).not.toMatch(
      /assessment\.analysis|horizon\.causal_channel|thesisContext\.analysis/,
    );
    expect(research).not.toMatch(
      /role\.analysis|asset\.causal_channel|thesis\.module_assessments|thesis\.narrative_sections/,
    );
    expect(`${decision}\n${research}`).not.toMatch(
      /\.replace\([^)]*(?:macro-module|no_call|degraded)/,
    );
  });

  it("keeps both feature surfaces namespaced and responsive", () => {
    const researchCss = [
      readFileSync(join(macroRoot, "ui/MacroResearchPage.css"), "utf8"),
      readFileSync(join(macroRoot, "ui/MacroResearchDossier.css"), "utf8"),
    ].join("\n");
    const decisionCss = [
      readFileSync(join(macroRoot, "ui/MacroCharts.css"), "utf8"),
      readFileSync(join(macroRoot, "ui/MacroDecisionBrief.css"), "utf8"),
      readFileSync(join(macroRoot, "ui/MacroDecisionOverview.css"), "utf8"),
      readFileSync(join(macroRoot, "ui/MacroDecisionPage.css"), "utf8"),
      readFileSync(join(macroRoot, "ui/MacroDecisionEvidence.css"), "utf8"),
      readFileSync(join(macroRoot, "ui/MacroDecisionPageResponsive.css"), "utf8"),
    ].join("\n");
    const researchSelectors = selectors(researchCss);
    const decisionSelectors = selectors(decisionCss);

    expect(researchCss).toContain("@layer app.features");
    expect(researchCss).toContain("@media (max-width: 767px)");
    expect(decisionCss).toContain("@layer app.features");
    expect(decisionCss).toContain("@media (max-width: 767px)");
    expect(decisionCss).toMatch(
      /\.macro-decision\s*{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)/s,
    );
    expect(researchSelectors.every((selector) => selector.startsWith("macro-research-"))).toBe(
      true,
    );
    expect(
      decisionSelectors.every(
        (selector) => selector.startsWith("macro-decision") || selector.startsWith("macro-chart"),
      ),
    ).toBe(true);
  });

  it("reserves full plot geometry for semantic SVG charts instead of icon sizing", () => {
    const baseCss = readSource("styles/base.css");
    const chartsCss = readFileSync(join(macroRoot, "ui/MacroCharts.css"), "utf8");

    expect(baseCss).toMatch(/\.lucide\s*{[^}]*width:\s*13px[^}]*height:\s*13px/s);
    expect(baseCss).not.toMatch(/(^|})\s*svg\s*{[^}]*width:\s*13px[^}]*height:\s*13px/s);
    expect(chartsCss).toMatch(
      /\.macro-chart > svg\s*{[^}]*width:\s*100%[^}]*height:\s*auto[^}]*min-height:\s*18rem/s,
    );
    expect(chartsCss).not.toMatch(/\.macro-chart--(?:curve|bars) > svg[^}]*min-width:\s*34rem/s);
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

function selectors(css: string): string[] {
  return [...css.matchAll(/(?<![\w-])\.([a-z][\w-]*)/g)].map((match) => match[1]);
}

function collectFiles(root: string): string[] {
  return readdirSync(root)
    .flatMap((entry) => {
      const path = join(root, entry);
      return statSync(path).isDirectory() ? collectFiles(path) : [path];
    })
    .sort();
}
