import { readdirSync, readFileSync, statSync } from "node:fs";
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

  it("keeps RadarPage and its queue as one bounded full-height scroll surface", () => {
    const liveCssPath = join(srcRoot, "features/live/ui/live.css");
    const liveCss = readFileSync(liveCssPath, "utf8");
    const rules = findRules(liveCss);
    const livePageRules = rules.filter((rule) =>
      selectorContains(rule.selector, ".live-radar-page"),
    );
    const radarQueueRules = rules.filter((rule) =>
      selectorContains(rule.selector, ".live-radar-queue"),
    );
    const radarItemsRules = rules.filter((rule) =>
      selectorContains(rule.selector, ".live-radar-items"),
    );

    expect(
      livePageRules.some(
        (rule) =>
          declarationValue(rule.body, "grid-template-rows") === "minmax(0, 1fr)" &&
          declarationValue(rule.body, "overflow") === "hidden",
      ),
      ".live-radar-page must reserve exactly one full-height Radar row",
    ).toBe(true);
    expect(
      radarQueueRules.some(
        (rule) =>
          selectorContains(rule.selector, ".live-radar-queue") &&
          !selectorContains(rule.selector, ".live-radar-queue--delayed") &&
          declarationValue(rule.body, "grid-template-rows") === "auto minmax(0, 1fr)" &&
          declarationValue(rule.body, "overflow") === "hidden",
      ),
      ".live-radar-queue must bound its header and queue scroller without an empty delay row",
    ).toBe(true);
    expect(
      rules.some(
        (rule) =>
          selectorContains(rule.selector, ".live-radar-queue--delayed") &&
          declarationValue(rule.body, "grid-template-rows") === "auto auto minmax(0, 1fr)",
      ),
      ".live-radar-queue must add a bounded row only while delay status is visible",
    ).toBe(true);
    expect(
      radarItemsRules.some(
        (rule) =>
          declarationValue(rule.body, "overflow") === "auto" &&
          declarationValue(rule.body, "min-height") === "0",
      ),
      ".live-radar-items must own the bounded queue scroll",
    ).toBe(true);
  });

  it("keeps the shadcn sidebar trigger in the shell contract", () => {
    const matches = cockpitShellCssUnits().flatMap((path) => {
      const css = readFileSync(path, "utf8");

      return findRules(css)
        .filter((rule) => selectorContains(rule.selector, ".topbar-sidebar-trigger"))
        .filter((rule) => declarationValue(rule.body, "display") === "inline-grid")
        .map(() => relativeToWeb(path));
    });

    expect(
      matches,
      ".topbar-sidebar-trigger must be visible so mobile and collapsed desktop users can open navigation",
    ).not.toEqual([]);
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

  it("keeps retired generic radar table selectors out of live CSS", () => {
    const liveCssPath = join(srcRoot, "features/live/ui/live.css");
    const liveCss = readFileSync(liveCssPath, "utf8");
    const forbiddenSelectors = [
      ".radar-head",
      ".radar-row",
      ".radar-row-select",
      ".radar-control-row",
      ".token-cell",
      ".case-cell",
      ".venue-cell",
      ".metric",
      ".phase",
      ".direction",
      ".radar-skeleton",
      ".segmented",
      ".scope-toggle",
      ".venue-filter",
      ".sort-toggle",
      ".account-lane-card",
      ".account-kv",
      ".entity-tags",
      ".timeline-summary",
      ".timeline-chart",
      ".timeline-skeleton",
      ".replay-focus-head",
      ".replay-focus-grid",
      ".replay-event-rail",
      ".replay-metrics",
      ".score-overview",
      ".settlement-grid",
      ".evidence-query-kv",
      ".tabs",
    ];
    const offenders = findRules(liveCss).flatMap((rule) =>
      forbiddenSelectors
        .filter((selector) => selectorContains(rule.selector, selector))
        .map(
          (selector) =>
            `${relativeToSrc(liveCssPath)}:${lineNumber(liveCss, rule.start)} keeps retired ${selector} via ${compactSelector(
              rule.selector,
            )}`,
        ),
    );

    expect(offenders, "Live Radar must use live-radar-* selectors owned by live.css.").toEqual([]);
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
      ".token-profile-card",
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

  it("keeps Research shared UI selectors out of feature CSS buckets", () => {
    const offenders = collectFiles(join(srcRoot, "features"))
      .filter(isCssFile)
      .flatMap((path) => {
        const css = readFileSync(path, "utf8");

        return findRules(css)
          .filter((rule) => rule.selector.includes(".research-"))
          .map(
            (rule) =>
              `${relativeToSrc(path)}:${lineNumber(css, rule.start)} reaches into Research UI via ${compactSelector(
                rule.selector,
              )}`,
          );
      });

    expect(
      offenders,
      "Feature CSS may place shared Research components, but must not restyle .research-* internals.",
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
