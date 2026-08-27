import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { basename, dirname, extname, isAbsolute, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

import postcss from "postcss";
import selectorParser from "postcss-selector-parser";
import { describe, expect, it } from "vitest";

const webRoot = join(dirname(fileURLToPath(import.meta.url)), "../..");
const srcRoot = join(webRoot, "src");
const appLayerOrder =
  "@layer properties, theme, base, components, utilities, app.base, app.primitives, app.shell, app.features, app.overrides;";

const globalStylesDir = join(srcRoot, "styles");
const existingGlobalUtilityClasses = new Set(["lucide", "sr-only"]);

const featureClassPrefixes: Record<string, string[]> = {
  cockpit: ["brand", "brand-", "center-column", "cockpit-", "searchbar", "topbar", "topbar-"],
  news: ["news-"],
  // #207 PR-W4: the capital lane is its own feature with its own namespace. The 成案 badge is rendered on a
  // News surface but keeps `trading-` — the words in it are the ledger's, and so is the ownership.
  trading: ["trading-"],
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
  it("keeps the application's global CSS entrypoints inside styles", () => {
    const mainPath = join(srcRoot, "main.tsx");
    const importedByMain = cssImports(mainPath)
      .map((cssImport) => resolve(dirname(mainPath), cssImport.specifier))
      .sort();

    expect(importedByMain.length).toBeGreaterThan(0);
    expect(
      importedByMain.every((path) => isSideEffectCssFile(path) && isGlobalStyleFile(path)),
    ).toBe(true);
  });

  it("keeps component and feature selectors out of global styles", () => {
    const offenders = collectFiles(globalStylesDir)
      .filter(isSideEffectCssFile)
      .flatMap((path) =>
        cssRules(readFileSync(path, "utf8")).flatMap((rule) =>
          rule.classNames
            .filter(
              (className) =>
                !existingGlobalUtilityClasses.has(className) && !className.startsWith("tf-global-"),
            )
            .map(
              (className) =>
                `${relativeToSrc(path)}:${rule.line} defines non-global class .${className}`,
            ),
        ),
      );

    expect(
      offenders,
      "Component and feature classes live beside their owners; new application-wide utilities use the tf-global- namespace.",
    ).toEqual([]);
  });

  it("keeps relative global stylesheet imports inside styles", () => {
    const offenders = collectFiles(globalStylesDir)
      .filter(isSideEffectCssFile)
      .flatMap((path) =>
        cssAtImports(readFileSync(path, "utf8")).flatMap((specifier) => {
          if (specifier === "tailwindcss") return [];
          const importedPath = resolve(dirname(path), specifier);
          if (
            specifier.startsWith(".") &&
            isGlobalStyleFile(importedPath) &&
            isSideEffectCssFile(importedPath) &&
            existsSync(importedPath)
          ) {
            return [];
          }
          return [`${relativeToSrc(path)} imports global CSS from ${specifier}`];
        }),
      );

    expect(
      offenders,
      "Global styles may import Tailwind or other CSS under src/styles; component and feature CSS stays owner-local.",
    ).toEqual([]);
  });

  it("declares app cascade layers before split CSS chunks can load", () => {
    const indexHtml = readFileSync(join(webRoot, "index.html"), "utf8");
    const tokensCss = readFileSync(globalTokenOwner(), "utf8");

    expect(tokensCss.trimStart().startsWith(appLayerOrder)).toBe(true);
    expect(indexHtml).toContain(appLayerOrder);
    expect(indexHtml.indexOf(appLayerOrder)).toBeLessThan(indexHtml.indexOf('<link rel="icon"'));
  });

  it("keeps global custom property definitions in one token owner", () => {
    const tokenOwner = globalTokenOwner();
    const offenders = collectFiles(srcRoot)
      .filter(isSideEffectCssFile)
      .filter((path) => path !== tokenOwner)
      .flatMap((path) => {
        const css = readFileSync(path, "utf8");
        return cssRules(css)
          .filter((rule) =>
            rule.selector.split(",").some((selector) => selector.trim() === ":root"),
          )
          .map((rule) => `${relativeToSrc(path)}:${rule.line} defines :root`);
      });

    expect(offenders).toEqual([]);
  });

  it("keeps literal colours in the token owner", () => {
    const tokenOwner = globalTokenOwner();
    const offenders = collectFiles(srcRoot)
      .filter(isCssFile)
      .filter((path) => path !== tokenOwner)
      .flatMap((path) => {
        const css = readFileSync(path, "utf8");
        return cssDeclarations(css)
          .filter((declaration) => /#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)/.test(declaration.value))
          .map(
            (declaration) =>
              `${relativeToSrc(path)}:${declaration.line} uses ${declaration.property}: ${declaration.value}`,
          );
      });

    expect(
      offenders,
      "Semantic colours belong in the global token owner; route and component CSS must consume a token.",
    ).toEqual([]);
  });

  it("keeps semantic colour derivation in the token owner", () => {
    const tokenOwner = globalTokenOwner();
    const offenders = collectFiles(srcRoot)
      .filter(isCssFile)
      .filter((path) => path !== tokenOwner)
      .flatMap((path) => {
        const css = readFileSync(path, "utf8");
        return cssDeclarations(css)
          .filter((declaration) => declaration.value.includes("color-mix("))
          .map(
            (declaration) =>
              `${relativeToSrc(path)}:${declaration.line} derives ${declaration.property}: ${declaration.value}`,
          );
      });

    expect(
      offenders,
      "Components consume semantic colour tokens; they do not create new shades at the call site.",
    ).toEqual([]);
  });

  it("keeps type sizes and radii on the global scale", () => {
    const typographyOffenders: string[] = [];
    const radiusOffenders: string[] = [];
    const tokenOwner = globalTokenOwner();

    for (const path of collectFiles(srcRoot)
      .filter(isCssFile)
      .filter((path) => path !== tokenOwner)) {
      const css = readFileSync(path, "utf8");
      for (const declaration of cssDeclarations(css)) {
        if (
          (declaration.property === "font" || declaration.property === "font-size") &&
          /\b\d*\.?\d+(?:px|rem)\b/.test(declaration.value)
        ) {
          typographyOffenders.push(
            `${relativeToSrc(path)}:${declaration.line} uses ${declaration.property}: ${declaration.value}`,
          );
        }
        if (
          declaration.property === "border-radius" &&
          !declaration.value.trim().startsWith("var(")
        ) {
          radiusOffenders.push(
            `${relativeToSrc(path)}:${declaration.line} uses border-radius: ${declaration.value}`,
          );
        }
      }
    }

    expect(
      typographyOffenders,
      "Production type sizes must consume one of the seven steps from the global token owner.",
    ).toEqual([]);
    expect(
      radiusOffenders,
      "Production radii must consume the shape scale from the global token owner.",
    ).toEqual([]);
  });

  it("does not reference undefined CSS custom properties", () => {
    const cssFiles = collectFiles(srcRoot).filter(isCssFile);
    const defined = new Set(
      cssFiles.flatMap((path) =>
        cssDeclarations(readFileSync(path, "utf8"))
          .filter((declaration) => declaration.property.startsWith("--"))
          .map((declaration) => declaration.property),
      ),
    );
    const offenders = cssFiles.flatMap((path) => {
      const css = readFileSync(path, "utf8");
      return cssDeclarations(css).flatMap((declaration) =>
        [...declaration.value.matchAll(/var\((--[a-z0-9-]+)(\s*,[^)]*)?\)/g)]
          .filter((match) => !defined.has(match[1]) && !match[2])
          .map((match) => `${relativeToSrc(path)}:${declaration.line} references ${match[1]}`),
      );
    });

    expect(
      offenders,
      "Every CSS custom property must be defined, or supply a local fallback at its use site.",
    ).toEqual([]);
  });

  it("keeps side-effect CSS imported only by local owner files", () => {
    const sourceFiles = collectFiles(srcRoot).filter((path) =>
      [".ts", ".tsx"].includes(extname(path)),
    );
    const importersByCssPath = new Map<string, string[]>();

    for (const sourceFile of sourceFiles) {
      for (const cssImport of cssImports(sourceFile)) {
        const cssPath = resolve(dirname(sourceFile), cssImport.specifier);

        if (isModuleCssFile(cssPath) || isGlobalStyleFile(cssPath)) {
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
      .filter((path) => !isGlobalStyleFile(path))
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
        .flatMap((path) => cssRules(readFileSync(path, "utf8")).flatMap((rule) => rule.classNames))
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
                `${relativeToSrc(path)}:${rule.line} redefines shared UI class .${className}`,
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
              `${relativeToSrc(path)}:${rule.line} uses unowned class .${className} in ${compactSelector(rule.selector)}`,
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

      for (const className of cssRules(readFileSync(path, "utf8")).flatMap(
        (rule) => rule.classNames,
      )) {
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
  line: number;
  selector: string;
};

type CssDeclaration = {
  line: number;
  property: string;
  value: string;
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

function cssAtImports(css: string): string[] {
  const imports: string[] = [];
  postcss.parse(css).walkAtRules("import", (rule) => {
    const match = rule.params.trim().match(/^(?:url\(\s*)?["']?([^"')\s]+)["']?/);
    imports.push(match?.[1] ?? rule.params.trim());
  });
  return imports;
}

function isAllowedCssImport(sourcePath: string, specifier: string): boolean {
  if (!specifier.startsWith("./")) {
    return false;
  }

  const cssPath = resolve(dirname(sourcePath), specifier);
  const sourceRelative = relativeToSrc(sourcePath);

  if (sourceRelative === "main.tsx" && isGlobalStyleFile(cssPath)) {
    return existsSync(cssPath);
  }

  return dirname(sourcePath) === dirname(cssPath) && existsSync(cssPath);
}

function cssRules(css: string): CssRule[] {
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

function cssClassNames(input: string): string[] {
  const classes = new Set<string>();
  selectorParser((selectors) => {
    selectors.walkClasses((node) => {
      classes.add(node.value);
    });
  }).processSync(input);
  return [...classes];
}

function cssDeclarations(css: string): CssDeclaration[] {
  const declarations: CssDeclaration[] = [];
  postcss.parse(css).walkDecls((declaration) => {
    declarations.push({
      line: declaration.source?.start?.line ?? 1,
      property: declaration.prop,
      value: declaration.value,
    });
  });
  return declarations;
}

function globalTokenOwner(): string {
  const owners = collectFiles(globalStylesDir)
    .filter(isSideEffectCssFile)
    .filter((path) => {
      const css = readFileSync(path, "utf8");
      let ownsRootTokens = false;
      postcss.parse(css).walkRules((rule) => {
        if (
          rule.selector.split(",").some((selector) => selector.trim() === ":root") &&
          rule.nodes.some((node) => node.type === "decl" && node.prop === "--surface-canvas")
        ) {
          ownsRootTokens = true;
        }
      });
      return ownsRootTokens;
    });

  if (owners.length !== 1) {
    throw new Error(
      `Expected exactly one global semantic token owner, found: ${owners.map(relativeToSrc).join(", ") || "none"}`,
    );
  }
  return owners[0];
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

function matchesAnyPrefix(className: string, prefixes: string[]): boolean {
  return prefixes.some((prefix) =>
    prefix.endsWith("-") ? className.startsWith(prefix) : className === prefix,
  );
}

function relativeToSrc(path: string): string {
  return relative(srcRoot, path);
}

function isGlobalStyleFile(path: string): boolean {
  const ownerRelative = relative(globalStylesDir, path);
  return (
    ownerRelative !== "" &&
    ownerRelative !== ".." &&
    !ownerRelative.startsWith(`..${sep}`) &&
    !isAbsolute(ownerRelative)
  );
}
