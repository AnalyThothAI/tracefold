import { existsSync, readFileSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const webRoot = join(dirname(fileURLToPath(import.meta.url)), "../..");
const srcRoot = join(webRoot, "src");

describe("data router architecture", () => {
  it("does not wrap the app root in BrowserRouter", () => {
    const appRoot = readSource("app/AppRoot.tsx");

    expect(appRoot).not.toMatch(/\bBrowserRouter\b/);
  });

  it("defines the app route tree through a React Router data router config", () => {
    const routerPath = join(srcRoot, "routes/router.tsx");

    expect(existsSync(routerPath), `${relative(webRoot, routerPath)} must exist`).toBe(true);

    const router = readFileSync(routerPath, "utf8");
    expect(router).toContain("createBrowserRouter");
    expect(router).toContain("createMemoryRouter");
    expect(router).toMatch(/\blazy\s*:/);
    expect(router).toContain("errorElement");
  });

  it("keeps root route config from eagerly importing feature pages or page route modules", () => {
    const router = readSource("routes/router.tsx");
    const importSources = [
      ...router.matchAll(/import\s+(?:type\s+)?[\s\S]*?\s+from\s+["']([^"']+)["']/g),
    ]
      .map((match) => match[1])
      .filter((source): source is string => Boolean(source));

    const eagerPageImports = importSources.filter(
      (source) =>
        source.startsWith("@features/") ||
        (source.endsWith(".route") && source !== "./shell.route"),
    );

    expect(eagerPageImports).toEqual([]);
  });

  it("keeps eager shell data imports away from page-exporting feature barrels", () => {
    const shellSources = [
      "routes/shell.route.tsx",
      "routes/shellChromeContext.ts",
      "routes/shellChromeData.ts",
    ].map(readSource);
    const importSources = shellSources.flatMap(importSpecifiers);
    const pageExportingFeatureBarrels = [
      "@features/macro",
      "@features/news",
      "@features/search",
      "@features/token-case",
    ];

    expect(importSources.filter((source) => pageExportingFeatureBarrels.includes(source))).toEqual(
      [],
    );
  });

  it("keeps shell chrome data above cockpit and search shell switches", () => {
    const router = readSource("routes/router.tsx");
    const shellRoute = readSource("routes/shell.route.tsx");

    expect(router).toContain("<ShellChromeRoute />");
    expect(shellRoute).toContain("ShellChromeContext.Provider");
    expect(shellRoute).toContain("<Outlet />");
    expect(shellRoute).toContain("useShellChrome()");
  });

  it("keeps eager shell dependencies from importing page-exporting search barrels", () => {
    const eagerShellSources = ["routes/shellChromeData.ts"].map(readSource);

    expect(
      eagerShellSources.flatMap(importSpecifiers).filter((source) => source === "@features/search"),
    ).toEqual([]);
  });

  it("keeps macro routing on the data-router module path without legacy prop wrappers", () => {
    const macroFeatureRoot = join(srcRoot, "features/macro");
    const legacyFiles = ["MacroPage.tsx", "api/useMacroQuery.ts"]
      .map((path) => join(macroFeatureRoot, path))
      .filter(existsSync)
      .map((path) => relative(webRoot, path));
    const macroBarrel = readSource("features/macro/index.ts");

    expect(legacyFiles).toEqual([]);
    expect(macroBarrel).not.toMatch(/\bMacroPage\b/);
    expect(macroBarrel).not.toContain("useMacroQuery");
  });

  it("keeps only the current public News route family", () => {
    const routerSource = readSource("routes/router.tsx");

    expect(routerSource).toContain('path: "news"');
    expect(routerSource).toContain('path: "news/events/:eventId"');
    expect(routerSource).toContain('path: "news/status"');
    expect(routerSource).not.toContain('path: "news/stories/:storyId"');
    expect(routerSource).not.toContain('path: "news/brief"');
    expect(routerSource).not.toContain('path: "news/sources"');
    expect(routerSource).not.toContain('path: "news/items/:newsItemId"');
    expect(routerSource).not.toContain("news/stories");
    expect(routerSource).not.toContain("news/brief");
    expect(routerSource).not.toContain("news/sources");
  });

  it("does not keep the retired Signal Lab page routes or navigation target", () => {
    const routerSource = readSource("routes/router.tsx");
    const navigationSource = readSource("features/cockpit/ui/appNavigation.ts");

    expect(routerSource).not.toContain('path: "signal-lab"');
    expect(routerSource).not.toContain('path: "signal-lab/pulse/:candidateId"');
    expect(navigationSource).not.toContain('to: "/signal-lab"');
  });
});

function readSource(path: string): string {
  return readFileSync(join(srcRoot, path), "utf8");
}

function importSpecifiers(source: string): string[] {
  return [...source.matchAll(/import\s+(?:type\s+)?[\s\S]*?\s+from\s+["']([^"']+)["']/g)]
    .map((match) => match[1])
    .filter((specifier): specifier is string => Boolean(specifier));
}
