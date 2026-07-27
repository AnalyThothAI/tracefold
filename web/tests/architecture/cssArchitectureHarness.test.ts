import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { basename, dirname, extname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const webRoot = join(dirname(fileURLToPath(import.meta.url)), "../..");
const srcRoot = join(webRoot, "src");
const appLayerOrder =
  "@layer properties, theme, base, components, utilities, app.base, app.primitives, app.shell, app.features, app.overrides;";

const retiredGlobalCssBuckets = new Set([
  "cockpit.css",
  "macro.css",
  "macroResponsive.css",
  "shared.css",
  "signalLab.css",
]);
const globalStyleFiles = new Set(["styles/base.css", "styles/tailwind.css", "styles/tokens.css"]);

const featureClassPrefixes: Record<string, string[]> = {
  cockpit: [
    "brand",
    "brand-",
    "center-column",
    "cockpit-",
    "decision-count",
    "main-route-button",
    "rail-",
    "search-focus-mode",
    "search-shell",
    "searchbar",
    "side-rail",
    "status-pills",
    "top-stats",
    "topbar",
    "topbar-",
    "ws-status-beacon",
  ],
  live: [
    "listed-fact",
    "live-",
    "market-",
    "admission-fact",
    "radar-",
    "token-radar-",
    "toolbar-controls",
  ],
  macro: ["active-trigger-column", "macro-"],
  news: ["news-"],
  ops: ["ops-"],
  search: ["search-"],
  stocks: ["stock-", "stocks-"],
};

const modifierClassNames = new Set([
  "account_event",
  "active",
  "admission",
  "bad",
  "bearish",
  "bear",
  "bullish",
  "bull",
  "case",
  "compact",
  "complete",
  "confirm",
  "constructive",
  "contradict",
  "credited",
  "degraded",
  "discard",
  "down",
  "driver",
  "empty",
  "error",
  "flat",
  "frozen",
  "gap",
  "good",
  "health",
  "hold",
  "holders",
  "hot",
  "info",
  "invalidate",
  "investigate",
  "listed",
  "liquidity",
  "market",
  "neutral",
  "official",
  "open",
  "opportunity",
  "primary",
  "read",
  "ready",
  "risk",
  "score",
  "selected",
  "settled",
  "stress",
  "two",
  "unavailable",
  "up",
  "venue",
  "volume",
  "warn",
  "watch",
  "wide",
]);

describe("CSS architecture harness", () => {
  it("declares app cascade layers before split CSS chunks can load", () => {
    const indexHtml = readFileSync(join(webRoot, "index.html"), "utf8");
    const tokensCss = readFileSync(join(srcRoot, "styles/tokens.css"), "utf8");

    expect(tokensCss.trimStart().startsWith(appLayerOrder)).toBe(true);
    expect(indexHtml).toContain(appLayerOrder);
    expect(indexHtml.indexOf(appLayerOrder)).toBeLessThan(indexHtml.indexOf('<link rel="icon"'));
  });

  it("does not recreate retired side-effect CSS buckets", () => {
    const cssFiles = collectFiles(srcRoot).filter(isCssFile);
    const retiredFiles = cssFiles
      .filter((path) => retiredGlobalCssBuckets.has(basename(path)))
      .map(relativeToSrc);

    const retiredImports = collectFiles(srcRoot)
      .filter((path) => [".ts", ".tsx"].includes(extname(path)))
      .flatMap((path) =>
        cssImports(path)
          .filter((item) => retiredGlobalCssBuckets.has(basename(item.specifier)))
          .map((item) => `${relativeToSrc(path)} imports ${item.specifier}`),
      );

    expect([...retiredFiles, ...retiredImports]).toEqual([]);
  });

  it("keeps retired route and sidebar selector fragments out of production side-effect CSS", () => {
    const retiredFragments = ["desktop-side-rail", "mobile-route-nav", "side-rail", "route-nav"];
    const offenders = collectFiles(srcRoot)
      .filter(isSideEffectCssFile)
      .flatMap((path) => {
        const css = readFileSync(path, "utf8");

        return cssRules(css).flatMap((rule) =>
          retiredFragments
            .filter((fragment) => rule.selector.includes(fragment))
            .map(
              (fragment) =>
                `${relativeToSrc(path)}:${lineNumber(css, rule.start)} keeps retired ${fragment} fragment via ${compactSelector(
                  rule.selector,
                )}`,
            ),
        );
      });

    expect(offenders).toEqual([]);
  });

  it("keeps legacy migration selector names out of production CSS", () => {
    const offenders = collectFiles(srcRoot)
      .filter(isCssFile)
      .flatMap((path) => {
        const css = readFileSync(path, "utf8");
        return cssClassNames(css)
          .filter((className) => /legacy/i.test(className))
          .map((className) => `${relativeToSrc(path)} keeps .${className}`);
      });

    expect(offenders).toEqual([]);
  });

  it("keeps global custom property definitions in the token stylesheet", () => {
    const offenders = collectFiles(srcRoot)
      .filter(isSideEffectCssFile)
      .filter((path) => relativeToSrc(path) !== "styles/tokens.css")
      .flatMap((path) => {
        const css = readFileSync(path, "utf8");
        return cssRules(css)
          .filter((rule) =>
            rule.selector.split(",").some((selector) => selector.trim() === ":root"),
          )
          .map((rule) => `${relativeToSrc(path)}:${lineNumber(css, rule.start)} defines :root`);
      });

    expect(offenders).toEqual([]);
  });

  it("keeps side-effect CSS imported only by local owner files", () => {
    const sourceFiles = collectFiles(srcRoot).filter((path) =>
      [".ts", ".tsx"].includes(extname(path)),
    );
    const importersByCssPath = new Map<string, string[]>();

    for (const sourceFile of sourceFiles) {
      for (const cssImport of cssImports(sourceFile)) {
        const cssPath = resolve(dirname(sourceFile), cssImport.specifier);

        if (isModuleCssFile(cssPath) || globalStyleFiles.has(relativeToSrc(cssPath))) {
          continue;
        }

        const importerDir = dirname(sourceFile);
        const cssDir = dirname(cssPath);

        if (!importersByCssPath.has(cssPath)) {
          importersByCssPath.set(cssPath, []);
        }
        importersByCssPath.get(cssPath)?.push(relativeToSrc(sourceFile));

        expect(
          importerDir,
          `${relativeToSrc(sourceFile)} imports ${cssImport.specifier}; side-effect CSS must live beside its owner component or route.`,
        ).toBe(cssDir);
      }
    }

    const orphanedSideEffectCss = collectFiles(srcRoot)
      .filter(isSideEffectCssFile)
      .filter((path) => !globalStyleFiles.has(relativeToSrc(path)))
      .filter((path) => !importersByCssPath.has(path))
      .map(relativeToSrc);

    expect(orphanedSideEffectCss).toEqual([]);
  });

  it("keeps CSS imports relative and local to source owners", () => {
    const offenders = collectFiles(srcRoot)
      .filter((path) => [".ts", ".tsx"].includes(extname(path)))
      .flatMap((path) =>
        cssImports(path)
          .filter((item) => !isAllowedCssImport(path, item.specifier))
          .map((item) => `${relativeToSrc(path)} imports non-local CSS ${item.specifier}`),
      );

    expect(offenders).toEqual([]);
  });

  it("prevents feature CSS from redefining shared UI classes", () => {
    const sharedUiClasses = new Set(
      collectFiles(join(srcRoot, "shared/ui"))
        .filter(isSideEffectCssFile)
        .flatMap((path) => cssClassNames(readFileSync(path, "utf8")))
        .filter((className) => !isModifierClassName(className)),
    );

    const offenders = collectFiles(join(srcRoot, "features"))
      .filter(isCssFile)
      .flatMap((path) => {
        const css = readFileSync(path, "utf8");

        return cssRules(css).flatMap((rule) =>
          rule.classNames
            .filter((className) => sharedUiClasses.has(className))
            .map(
              (className) =>
                `${relativeToSrc(path)}:${lineNumber(css, rule.start)} redefines shared UI class .${className}`,
            ),
        );
      });

    expect(offenders).toEqual([]);
  });

  it("keeps feature side-effect selectors in their feature namespace", () => {
    const offenders = collectFiles(join(srcRoot, "features"))
      .filter(isSideEffectCssFile)
      .flatMap((path) => {
        const featureName = featureNameFromPath(path);
        const allowedPrefixes = featureClassPrefixes[featureName];
        const css = readFileSync(path, "utf8");

        if (!allowedPrefixes) {
          return [`${relativeToSrc(path)} has no CSS namespace policy for feature ${featureName}`];
        }

        return cssRules(css).flatMap((rule) => {
          const ownerClasses = rule.classNames.filter((className) =>
            matchesAnyPrefix(className, allowedPrefixes),
          );
          const unscopedModifiers =
            rule.classNames.length > 0 && ownerClasses.length === 0
              ? rule.classNames.filter(isModifierClassName)
              : [];
          const foreignClasses = rule.classNames.filter(
            (className) =>
              !matchesAnyPrefix(className, allowedPrefixes) && !isModifierClassName(className),
          );

          return [...foreignClasses, ...unscopedModifiers].map(
            (className) =>
              `${relativeToSrc(path)}:${lineNumber(css, rule.start)} uses unowned class .${className} in ${compactSelector(rule.selector)}`,
          );
        });
      });

    expect(
      offenders,
      "Feature side-effect CSS must use feature-owned class prefixes; modifier classes must be attached to an owner class, never defined naked.",
    ).toEqual([]);
  });

  it("keeps side-effect class names from being shared across feature roots", () => {
    const rootsByClassName = new Map<string, Set<string>>();

    for (const path of collectFiles(join(srcRoot, "features")).filter(isSideEffectCssFile)) {
      const featureName = featureNameFromPath(path);

      for (const className of cssClassNames(readFileSync(path, "utf8"))) {
        if (isModifierClassName(className)) {
          continue;
        }
        if (!rootsByClassName.has(className)) {
          rootsByClassName.set(className, new Set());
        }
        rootsByClassName.get(className)?.add(featureName);
      }
    }

    const offenders = [...rootsByClassName.entries()]
      .filter(([, roots]) => roots.size > 1)
      .map(([className, roots]) => `.${className} is defined by ${[...roots].sort().join(", ")}`)
      .sort();

    expect(offenders).toEqual([]);
  });
});

type CssImport = {
  specifier: string;
};

type CssRule = {
  classNames: string[];
  selector: string;
  start: number;
};

function collectFiles(root: string): string[] {
  return readdirSync(root).flatMap((entry) => {
    const path = join(root, entry);
    return statSync(path).isDirectory() ? collectFiles(path) : [path];
  });
}

function cssImports(path: string): CssImport[] {
  const source = readFileSync(path, "utf8");
  const imports: CssImport[] = [];
  const pattern = /import\s+(?:[^"']+\s+from\s+)?["']([^"']+\.css)["'];/g;
  let match: RegExpExecArray | null;

  while ((match = pattern.exec(source)) !== null) {
    imports.push({ specifier: match[1] });
  }

  return imports;
}

function isAllowedCssImport(sourcePath: string, specifier: string): boolean {
  if (!specifier.startsWith("./")) {
    return false;
  }

  const cssPath = resolve(dirname(sourcePath), specifier);
  const sourceRelative = relativeToSrc(sourcePath);
  const cssRelative = relativeToSrc(cssPath);

  if (sourceRelative === "main.tsx" && globalStyleFiles.has(cssRelative)) {
    return existsSync(cssPath);
  }

  return dirname(sourcePath) === dirname(cssPath) && existsSync(cssPath);
}

function cssRules(css: string): CssRule[] {
  const normalizedCss = sanitizeCss(css);
  const rules: CssRule[] = [];
  const pattern = /([^{}]+)\{([^{}]*)\}/g;
  let match: RegExpExecArray | null;

  while ((match = pattern.exec(normalizedCss)) !== null) {
    const selector = match[1].trim();

    if (!selector || selector.startsWith("@")) {
      continue;
    }

    rules.push({
      classNames: cssClassNames(selector),
      selector,
      start: match.index,
    });
  }

  return rules;
}

function cssClassNames(input: string): string[] {
  const normalizedInput = sanitizeCss(input);

  return [
    ...new Set(
      [...normalizedInput.matchAll(/\.(-?[_a-zA-Z]+[_a-zA-Z0-9-]*)/g)].map((item) => item[1]),
    ),
  ];
}

function compactSelector(selector: string): string {
  return selector.replace(/\s+/g, " ");
}

function featureNameFromPath(path: string): string {
  return relativeToSrc(path).split("/")[1] ?? "";
}

function isCssFile(path: string): boolean {
  return extname(path) === ".css";
}

function isModuleCssFile(path: string): boolean {
  return basename(path).endsWith(".module.css");
}

function isSideEffectCssFile(path: string): boolean {
  return isCssFile(path) && !isModuleCssFile(path);
}

function isModifierClassName(className: string): boolean {
  return (
    modifierClassNames.has(className) ||
    className.startsWith("is-") ||
    className.startsWith("severity-") ||
    className.startsWith("state-") ||
    className.startsWith("tone-")
  );
}

function lineNumber(input: string, index: number): number {
  return input.slice(0, index).split(/\r?\n/).length;
}

function matchesAnyPrefix(className: string, prefixes: string[]): boolean {
  return prefixes.some((prefix) =>
    prefix.endsWith("-") ? className.startsWith(prefix) : className === prefix,
  );
}

function relativeToSrc(path: string): string {
  return relative(srcRoot, path);
}

function sanitizeCss(input: string): string {
  return input
    .replace(/\/\*[\s\S]*?\*\//g, (match) => match.replace(/[^\r\n]/g, " "))
    .replace(/(@layer\s+app)\.([_a-zA-Z]+[_a-zA-Z0-9-]*)/g, "$1-$2");
}
