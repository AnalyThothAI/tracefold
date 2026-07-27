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
      "ui/MacroDecisionPage.tsx",
      "ui/MacroModuleSections.tsx",
      "ui/MacroResearchPage.tsx",
    ]);
    expect(files).not.toContain("shell.ts");
    expect(files.some((path) => path.includes("/pages/"))).toBe(false);
    expect(files.some((path) => /(registry|catalog|universal[-_]?renderer)/i.test(path))).toBe(
      false,
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
    expect(source).not.toMatch(/\/api\/macro\/evidence|MacroLive|history window|[?&]window=/i);
  });

  it("shows fixed judgment, separate status planes, gaps and asynchronous Evidence-Pack research", () => {
    const decision = [
      readFileSync(join(macroRoot, "ui/MacroDecisionPage.tsx"), "utf8"),
      readFileSync(join(macroRoot, "ui/MacroModuleSections.tsx"), "utf8"),
    ].join("\n");
    const research = readFileSync(join(macroRoot, "ui/MacroResearchPage.tsx"), "utf8");

    for (const field of [
      "judgment.asset_directions",
      "judgment.dimensions",
      "module.contradictions",
      "module.falsifiers",
      "module.next_checkpoints",
      "module.status.coverage",
      "query.data.status.data_health",
      "query.data.status.judgment",
      "module.evidence.dataset_states",
      "module.evidence.latest_facts",
      "research.evidence_pack_id",
      "research.reviewer_disposition",
    ]) {
      expect(decision).toContain(field);
    }
    for (const field of [
      "publication.sections.map",
      "publication.evidence_gaps.map",
      "publication.citations.map",
      "publication.reviewer_notes.map",
      "publication.audit",
    ]) {
      expect(research).toContain(field);
    }
  });

  it("keeps both feature surfaces namespaced and responsive", () => {
    const researchCss = readFileSync(join(macroRoot, "ui/MacroResearchPage.css"), "utf8");
    const decisionCss = [
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
    expect(researchSelectors.every((selector) => selector.startsWith("macro-research-"))).toBe(
      true,
    );
    expect(decisionSelectors.every((selector) => selector.startsWith("macro-decision"))).toBe(true);
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
