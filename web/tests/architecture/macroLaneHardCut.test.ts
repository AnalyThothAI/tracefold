import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, extname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

import { APP_NAVIGATION_GROUPS } from "@features/cockpit/ui/appNavigation";
import { describe, expect, it } from "vitest";

const webRoot = join(dirname(fileURLToPath(import.meta.url)), "../..");
const srcRoot = join(webRoot, "src");
const testRoot = join(webRoot, "tests");
const sourceExtensions = new Set([".ts", ".tsx", ".css"]);

// #68 removed the Macro product line: the six current modules, the official-source acquisition lane,
// the Fed document analyses, and the whole `/macro` route tree. The console is News V3 only.
const removedSourcePaths = ["features/macro", "routes/macro.route.tsx"];

const removedTestPaths = [
  "component/features/macro",
  "routes/macro.route.test.tsx",
  "fixtures/macroFixture.ts",
  "e2e/full-stack/macro-full-stack.spec.ts",
  "e2e/golden-paths/macro-decision.spec.ts",
  "architecture/macroDecisionAndResearch.test.ts",
];

const retiredApiPaths = [
  "/api/macro/overview",
  "/api/macro/rates-fed",
  "/api/macro/economy-inflation",
  "/api/macro/liquidity-funding",
  "/api/macro/credit",
  "/api/macro/volatility",
  "/api/macro/cross-asset",
];

const retiredVocabulary = [
  "MacroOverviewPage",
  "MacroDecisionPage",
  "MacroModuleSections",
  "MacroCharts",
  "useMacroDecisionQuery",
  "macroModuleFixture",
  "macroOverviewFixture",
  "macroPath",
  "@features/macro",
  "macro-decision",
];

describe("macro lane hard cut (#68)", () => {
  it("keeps the retired Macro feature and route modules deleted", () => {
    const survivors = [
      ...removedSourcePaths.map((path) => join(srcRoot, path)),
      ...removedTestPaths.map((path) => join(testRoot, path)),
    ].filter(existsSync);

    expect(survivors.map((path) => relative(webRoot, path))).toEqual([]);
  });

  it("keeps Macro routes, reads, and vocabulary out of web/src", () => {
    const offenders = collectFiles(srcRoot)
      .filter((path) => sourceExtensions.has(extname(path)))
      // The generated OpenAPI mirror is regenerated from the served schema, not hand-edited.
      .filter((path) => relative(srcRoot, path) !== "lib/types/openapi.ts")
      .flatMap((path) => {
        const text = readFileSync(path, "utf8");
        return [...retiredApiPaths, ...retiredVocabulary, 'path: "macro']
          .filter((needle) => text.includes(needle))
          .map((needle) => `${relative(webRoot, path)}: ${needle}`);
      });

    expect(offenders).toEqual([]);
  });

  it("keeps the generated OpenAPI mirror free of Macro reads", () => {
    const openapi = readFileSync(join(srcRoot, "lib/types/openapi.ts"), "utf8");
    const paths = [...openapi.matchAll(/^ {4}"(\/[^"]+)": \{$/gm)].map((match) => match[1]);

    expect(paths.filter((path) => path.startsWith("/api/macro"))).toEqual([]);
    expect(openapi).not.toMatch(/MacroOverviewReadData|MacroModuleId|macro_module_current/);
  });

  it("keeps Macro fixtures and mocks out of the frontend test surface", () => {
    const offenders = collectFiles(testRoot)
      .filter((path) => sourceExtensions.has(extname(path)))
      .filter((path) => !relative(testRoot, path).startsWith("architecture/"))
      .flatMap((path) => {
        const text = readFileSync(path, "utf8");
        return [...retiredApiPaths, ...retiredVocabulary]
          .filter((needle) => text.includes(needle))
          .map((needle) => `${relative(webRoot, path)}: ${needle}`);
      });

    expect(offenders).toEqual([]);
  });

  it("leaves exactly the three News working surfaces in the primary navigation tree", () => {
    const items = APP_NAVIGATION_GROUPS.flatMap((group) => group.items);

    expect(items.map((item) => item.to)).toEqual(["/news", "/news/oi", "/news/review"]);
    expect(items.flatMap((item) => item.children ?? [])).toEqual([]);
  });
});

function collectFiles(root: string): string[] {
  return readdirSync(root).flatMap((entry) => {
    const path = join(root, entry);
    return statSync(path).isDirectory() ? collectFiles(path) : [path];
  });
}
