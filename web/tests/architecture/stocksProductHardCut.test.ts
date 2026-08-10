import { existsSync, readFileSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const webRoot = join(dirname(fileURLToPath(import.meta.url)), "../..");
const srcRoot = join(webRoot, "src");

describe("Stocks product hard cut", () => {
  it("keeps no retired Stocks route or feature source", () => {
    const retiredSources = [
      "features/stocks/api/useStocksRadarQuery.ts",
      "features/stocks/index.ts",
      "features/stocks/shell.ts",
      "features/stocks/state/stocksRouteState.ts",
      "features/stocks/ui/StocksRadarPage.tsx",
      "features/stocks/ui/stocks.css",
      "routes/stocks.route.tsx",
    ]
      .map((path) => join(srcRoot, path))
      .filter(existsSync)
      .map((path) => relative(webRoot, path));

    expect(retiredSources).toEqual([]);
  });

  it("keeps shared routing and shell modules free of Stocks product hooks", () => {
    const sharedSources = [
      "features/cockpit/ui/appNavigation.ts",
      "routes/router.tsx",
      "routes/shellChromeData.ts",
      "shared/query/queryKeys.ts",
      "shared/routing/paths.ts",
    ].map((path) => readFileSync(join(srcRoot, path), "utf8"));

    for (const source of sharedSources) {
      expect(source).not.toMatch(/stocks(?:Radar|Path)|\/stocks|Stocks/);
    }
  });
});
