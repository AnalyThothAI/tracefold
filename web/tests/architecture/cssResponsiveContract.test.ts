import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { basename, dirname, extname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const webRoot = join(dirname(fileURLToPath(import.meta.url)), "../..");
const srcRoot = join(webRoot, "src");
const testsRoot = join(webRoot, "tests");
const cockpitUiRoot = join(srcRoot, "features/cockpit/ui");

const shellSelectors = [
  ".cockpit-shell",
  ".cockpit-main",
  ".center-column",
  ".topbar",
  ".topbar-sidebar-trigger",
  ".cockpit-app-sidebar",
] as const;

const oversizedSideEffectCss = new Set<string>();

const unlayeredSideEffectCss = new Set(["styles/tailwind.css"]);

describe("responsive CSS contract", () => {
  it("removes retired Live task-navigation selectors from the frontend", () => {
    const forbiddenFragments = [
      ".live-task-nav",
      ".mobile-task-nav",
      ".mobile-task-radar",
      ".mobile-task-tape",
      ".mobile-task-surface",
      "[data-mobile-task-panel",
    ];
    const offenders = collectFiles(srcRoot)
      .filter((path) => [".css", ".ts", ".tsx"].includes(extname(path)))
      .flatMap((path) => {
        const source = readFileSync(path, "utf8");
        return forbiddenFragments
          .filter((fragment) => source.includes(fragment))
          .map((fragment) => `${relativeToWeb(path)} retains ${fragment}`);
      });

    expect(offenders).toEqual([]);
  });

  it("removes the retired Live Radar feature and its owner CSS", () => {
    expect(existsSync(join(srcRoot, "features/live"))).toBe(false);
    const offenders = collectFiles(srcRoot)
      .filter((path) => [".css", ".ts", ".tsx"].includes(extname(path)))
      .flatMap((path) => {
        const source = readFileSync(path, "utf8");
        return [".live-radar-", ".radar-panel", "features/live", "token-radar"]
          .filter((fragment) => source.includes(fragment))
          .map((fragment) => `${relativeToWeb(path)} retains ${fragment}`);
      });

    expect(offenders).toEqual([]);
  });

  it("keeps navigation reachable at every width", () => {
    const matches = cockpitShellCssUnits().flatMap((path) => {
      const css = readFileSync(path, "utf8");

      return findRules(css)
        .filter((rule) => selectorContains(rule.selector, ".topbar-sidebar-trigger"))
        .filter((rule) => declarationValue(rule.body, "display") === "inline-grid")
        .map(() => relativeToWeb(path));
    });

    expect(
      matches,
      ".topbar-sidebar-trigger must be visible so tablet and collapsed desktop users can open the drawer",
    ).not.toEqual([]);

    /*
     * Below the tablet breakpoint there is no drawer to open (#87): the shell renders `AppBottomNav`
     * instead and the trigger is not rendered at all. Something must still carry navigation there, so the
     * bottom bar's own CSS has to turn it on inside a phone media query — without that rule the phone would
     * have no navigation whatsoever, which is exactly the regression this test exists to catch.
     */
    const bottomNavCss = readFileSync(join(cockpitUiRoot, "AppBottomNav.css"), "utf8");
    expect(bottomNavCss).toMatch(/@media \(max-width: 767px\)/);
    expect(
      findRules(bottomNavCss)
        .filter((rule) => selectorContains(rule.selector, ".cockpit-bottom-nav"))
        .some((rule) => declarationValue(rule.body, "display") === "grid"),
      "AppBottomNav.css must make .cockpit-bottom-nav visible at phone width",
    ).toBe(true);
    const shell = readFileSync(join(cockpitUiRoot, "CockpitShell.tsx"), "utf8");
    expect(shell).toContain("<AppBottomNav />");
    expect(shell).toContain("(max-width: 767px)");
    /*
     * The route column is the page's `main` landmark. The shadcn `SidebarInset` used to supply one; a console
     * without it makes a screen-reader user walk the sidebar and the topbar again on every route.
     */
    expect(shell).toContain('<main className="center-column">');
  });

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
                `${relativeToWeb(path)}:${lineNumber(css, rule.start)} owns ${selector} via ${compactSelector(
                  rule.selector,
                )}`,
            ),
        );
      });

    expect(offenders).toEqual([]);
  });

  it("does not keep retired rail/mobile-route-nav selectors in cockpit shell CSS", () => {
    const retiredFragments = [".desktop-side-rail", ".mobile-route-nav", ".cockpit-grid"];
    const offenders = cockpitShellCssUnits().flatMap((path) => {
      const css = readFileSync(path, "utf8");

      return findRules(css).flatMap((rule) =>
        retiredFragments
          .filter((fragment) => rule.selector.includes(fragment))
          .map(
            (fragment) =>
              `${relativeToWeb(path)}:${lineNumber(css, rule.start)} still owns retired ${fragment} via ${compactSelector(
                rule.selector,
              )}`,
          ),
      );
    });

    expect(offenders).toEqual([]);
  });

  it("removes the retired shell-level business control panel path", () => {
    const responsiveControlPanelOffenders = collectFiles(srcRoot)
      .filter((path) => [".css", ".ts", ".tsx"].includes(extname(path)))
      .filter((path) => readFileSync(path, "utf8").includes("responsive-control-panel"))
      .map(relativeToSrc);

    expect(
      responsiveControlPanelOffenders,
      [
        "Shell must not keep the retired responsive-control-panel compatibility path.",
        "Route-specific filters belong to their feature pages; the shell owns only navigation, frame, and scroll.",
      ].join("\n"),
    ).toEqual([]);
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
                `${relativeToSrc(path)}:${lineNumber(css, rule.start)} owns shared primitive ${selector} via ${compactSelector(
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

  it("reports side-effect CSS files above the 500-line budget", () => {
    const oversized = collectFiles(srcRoot)
      .filter(isSideEffectCssFile)
      .map((path) => ({
        path,
        lines: readFileSync(path, "utf8").split(/\r?\n/).length,
        relativePath: relativeToSrc(path),
      }))
      .filter(({ lines }) => lines > 500);

    const newOversized = oversized
      .filter(({ relativePath }) => !oversizedSideEffectCss.has(relativePath))
      .map(({ relativePath, lines }) => `${relativePath} has ${lines} lines`);

    expect(
      newOversized,
      [
        "Side-effect CSS files must stay at or below 500 lines.",
        "Split feature and primitive styling into adjacent owner CSS files instead of growing route-wide buckets.",
        ...oversized.map(({ relativePath, lines }) => `- ${relativePath}: ${lines} lines`),
      ].join("\n"),
    ).toEqual([]);
  });

  it("keeps page.setViewportSize calls in responsive or explicit desktop-only specs", () => {
    const offenders = collectFiles(testsRoot)
      .filter((path) => extname(path) === ".ts")
      .filter((path) => readFileSync(path, "utf8").includes("page.setViewportSize"))
      .filter((path) => !isViewportSpecAllowed(path, readFileSync(path, "utf8")))
      .map(
        (path) =>
          `${relativeToWeb(path)} uses page.setViewportSize outside a responsive or desktop-only spec`,
      );

    expect(offenders).toEqual([]);
  });

  it("requires side-effect CSS layers unless the file is on the Task 4 migration allowlist", () => {
    const offenders = collectFiles(srcRoot)
      .filter(isSideEffectCssFile)
      .filter((path) => !hasAppLayerDeclaration(readFileSync(path, "utf8")))
      .filter((path) => !unlayeredSideEffectCss.has(relativeToSrc(path)))
      .map(
        (path) =>
          `${relativeToSrc(
            path,
          )} is unlayered. Task 4 must wrap new side-effect CSS in @layer app.features, @layer app.shell, or another explicit app layer; only the exact migration allowlist may remain unlayered.`,
      );

    expect(offenders).toEqual([]);
  });
});

function cockpitShellCssUnits(): string[] {
  return readdirSync(cockpitUiRoot)
    .map((entry) => join(cockpitUiRoot, entry))
    .filter((path) => statSync(path).isFile())
    .filter((path) => basename(path).endsWith(".css") || basename(path).endsWith(".module.css"))
    .sort();
}

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
  body: string;
  selector: string;
  start: number;
};

function findRules(css: string): CssRule[] {
  const rules: CssRule[] = [];
  const pattern = /([^{}]+)\{([^{}]*)\}/g;
  let match: RegExpExecArray | null;

  while ((match = pattern.exec(css)) !== null) {
    const selector = match[1].trim();

    if (!selector || selector.startsWith("@")) {
      continue;
    }

    rules.push({
      selector,
      body: match[2],
      start: match.index,
    });
  }

  return rules;
}

function selectorContains(selectorList: string, classSelector: string): boolean {
  const className = classSelector.slice(1).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`(^|[^a-zA-Z0-9_-])\\.${className}(?![a-zA-Z0-9_-])`).test(selectorList);
}

function declarationValue(body: string, property: string): string | undefined {
  const declarationPattern = new RegExp(`${property}\\s*:\\s*([^;!}]+)`, "i");
  return body.match(declarationPattern)?.[1]?.trim().toLowerCase();
}

function compactSelector(selector: string): string {
  return selector.replace(/\s+/g, " ");
}

function lineNumber(input: string, index: number): number {
  return input.slice(0, index).split(/\r?\n/).length;
}

function isViewportSpecAllowed(path: string, contents: string): boolean {
  const relativePath = relativeToWeb(path);

  return (
    /(^|\/)(responsive|mobile|tablet|viewport)[^/]*\.spec\.ts$/.test(relativePath) ||
    /(^|\/)[^/]*(desktop-only|desktop)[^/]*\.spec\.ts$/.test(relativePath) ||
    contents.includes("@responsive-spec") ||
    contents.includes("@desktop-only-spec")
  );
}

function hasAppLayerDeclaration(css: string): boolean {
  return /@layer\s+(?:[^;{]*,\s*)?app\.(base|primitives|shell|features|overrides)\b/.test(css);
}
