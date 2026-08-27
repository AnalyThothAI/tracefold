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
    const pageExportingFeatureBarrels = ["@features/news"];

    expect(importSources.filter((source) => pageExportingFeatureBarrels.includes(source))).toEqual(
      [],
    );
  });

  it("keeps shell chrome data above the single cockpit shell", () => {
    const router = readSource("routes/router.tsx");
    const shellRoute = readSource("routes/shell.route.tsx");

    expect(router).toContain("<ShellChromeRoute />");
    expect(router).not.toContain("SearchShellRoute");
    expect(shellRoute).toContain("ShellChromeContext.Provider");
    expect(shellRoute).toContain("<Outlet />");
    expect(shellRoute).toContain("useShellChrome()");
    expect(shellRoute).not.toContain("SearchShell");
    expect(existsSync(join(srcRoot, "features/cockpit/ui/SearchShell.tsx"))).toBe(false);
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
