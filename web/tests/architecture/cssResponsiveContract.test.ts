import { readdirSync, readFileSync, statSync } from "node:fs";
import { basename, dirname, extname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

import postcss from "postcss";
import selectorParser from "postcss-selector-parser";
import { describe, expect, it } from "vitest";

const webRoot = join(dirname(fileURLToPath(import.meta.url)), "../..");
const srcRoot = join(webRoot, "src");

const shellSelectors = [
  ".cockpit-shell",
  ".cockpit-main",
  ".center-column",
  ".topbar",
  ".topbar-sidebar-trigger",
  ".cockpit-app-sidebar",
] as const;

const unlayeredSideEffectCss = new Set(["styles/tailwind.css"]);

describe("responsive CSS contract", () => {
  it("prevents non-cockpit feature CSS from owning cockpit shell selectors", () => {
    const offenders = collectFiles(join(srcRoot, "features"))
      .filter(isCssFile)
      .filter((path) => !relative(srcRoot, path).startsWith("features/cockpit/"))
      .flatMap((path) => {
        const css = readFileSync(path, "utf8");

        return findRules(css).flatMap((rule) =>
          shellSelectors
            .filter((selector) => selectorContains(rule.selector, selector))
            .map(
              (selector) =>
                `${relativeToWeb(path)}:${rule.line} owns ${selector} via ${compactSelector(
                  rule.selector,
                )}`,
            ),
        );
      });

    expect(offenders).toEqual([]);
  });

  it("keeps shared primitive selectors out of feature CSS buckets", () => {
    const primitiveSelectors = [
      ".compact-panel",
      ".decision-tag",
      ".icon-button",
      ".page-state-empty",
      ".page-state-error",
      ".page-state-loading",
      ".page-state-stale",
      ".page-state-table-block",
      ".page-state-table-row",
      ".page-state-table-skeleton",
    ];
    const offenders = collectFiles(join(srcRoot, "features"))
      .filter(isCssFile)
      .flatMap((path) => {
        const css = readFileSync(path, "utf8");

        return findRules(css).flatMap((rule) =>
          primitiveSelectors
            .filter((selector) => selectorContains(rule.selector, selector))
            .map(
              (selector) =>
                `${relativeToSrc(path)}:${rule.line} owns shared primitive ${selector} via ${compactSelector(
                  rule.selector,
                )}`,
            ),
        );
      });

    expect(
      offenders,
      "Feature CSS may lay out feature containers, but shared primitive internals belong under shared/ui.",
    ).toEqual([]);
  });

  it("requires every app-owned side-effect stylesheet to declare its cascade layer", () => {
    const offenders = collectFiles(srcRoot)
      .filter(isSideEffectCssFile)
      .filter((path) => !hasAppLayerDeclaration(readFileSync(path, "utf8")))
      .filter((path) => !unlayeredSideEffectCss.has(relativeToSrc(path)))
      .map(
        (path) =>
          `${relativeToSrc(
            path,
          )} is unlayered; app-owned side-effect CSS must name an app cascade layer.`,
      );

    expect(offenders).toEqual([]);
  });
});

function collectFiles(root: string): string[] {
  return readdirSync(root).flatMap((entry) => {
    const path = join(root, entry);
    return statSync(path).isDirectory() ? collectFiles(path) : [path];
  });
}

function isCssFile(path: string): boolean {
  return extname(path) === ".css";
}

function isSideEffectCssFile(path: string): boolean {
  return isCssFile(path) && !basename(path).endsWith(".module.css");
}

function relativeToWeb(path: string): string {
  return relative(webRoot, path);
}

function relativeToSrc(path: string): string {
  return relative(srcRoot, path);
}

type CssRule = {
  classNames: string[];
  line: number;
  selector: string;
};

function findRules(css: string): CssRule[] {
  const rules: CssRule[] = [];
  postcss.parse(css).walkRules((rule) => {
    rules.push({
      classNames: cssClassNames(rule.selector),
      line: rule.source?.start?.line ?? 1,
      selector: rule.selector,
    });
  });

  return rules;
}

function selectorContains(selectorList: string, classSelector: string): boolean {
  return cssClassNames(selectorList).includes(classSelector.slice(1));
}

function cssClassNames(selector: string): string[] {
  const classes = new Set<string>();
  selectorParser((selectors) => {
    selectors.walkClasses((node) => {
      classes.add(node.value);
    });
  }).processSync(selector);
  return [...classes];
}

function compactSelector(selector: string): string {
  return selector.replace(/\s+/g, " ");
}

function hasAppLayerDeclaration(css: string): boolean {
  let declared = false;
  postcss.parse(css).walkAtRules("layer", (rule) => {
    if (
      rule.params
        .split(",")
        .map((name) => name.trim())
        .some((name) => APP_LAYERS.has(name))
    ) {
      declared = true;
    }
  });
  return declared;
}

const APP_LAYERS = new Set([
  "app.base",
  "app.primitives",
  "app.shell",
  "app.features",
  "app.overrides",
]);
