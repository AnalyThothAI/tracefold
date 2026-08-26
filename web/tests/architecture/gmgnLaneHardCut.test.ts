import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, extname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const webRoot = join(dirname(fileURLToPath(import.meta.url)), "../..");
const srcRoot = join(webRoot, "src");
const testRoot = join(webRoot, "tests");
const sourceExtensions = new Set([".ts", ".tsx", ".css"]);

// #50 removed the GMGN lane: social tweet Events, token identity, Search, Token Case, DEX/CEX
// market data, the `/ws` live WebSocket, and the News market marks. The console is News only.
const removedSourcePaths = [
  "domain",
  "features/search",
  "features/token-case",
  "features/cockpit/ui/SearchShell.tsx",
  "lib/gmgn.ts",
  "lib/format.ts",
  "routes/search.route.tsx",
  "routes/token-target.route.tsx",
  "shared/model",
  "shared/query/patchMarketUpdate.ts",
  "shared/socket",
  "shared/ui/case-file",
];

const retiredApiPaths = [
  "/api/recent",
  "/api/events/by-ids",
  "/api/search",
  "/api/token-case",
  "/api/target-posts",
  "/api/target-social-timeline",
  "/api/live-market",
  "/api/token-image",
];

const retiredVocabulary = [
  "IntelSocket",
  "useSocketSnapshot",
  "useMarketSubscription",
  "reconnecting-websocket",
  "live_market_update",
  "market_live",
  "market_candles",
  "market_targets",
  "replay_limit",
  "SearchInspect",
  "TokenCase",
  "tokenCase",
  "token-case",
  "tokenTargetPath",
  "searchPath",
  "gmgn",
  "NewsMarketMark",
  "news-marks",
  "tokenImageUrl",
  "logo_url",
  "global search",
];

describe("GMGN lane hard cut", () => {
  it("keeps the retired Search, Token Case, socket, and market modules deleted", () => {
    const survivors = removedSourcePaths
      .map((path) => join(srcRoot, path))
      .filter(existsSync)
      .map((path) => relative(webRoot, path));

    expect(survivors).toEqual([]);
  });

  it("keeps retired API paths, WebSocket plumbing, and market vocabulary out of production", () => {
    const offenders = collectFiles(srcRoot)
      .filter((path) => sourceExtensions.has(extname(path)))
      .filter((path) => relative(srcRoot, path) !== "lib/types/openapi.ts")
      .flatMap((path) => {
        const text = readFileSync(path, "utf8");
        return [
          ...retiredApiPaths.filter((apiPath) => text.includes(apiPath)),
          ...retiredVocabulary.filter((word) => text.includes(word)),
          /["'`]\/ws["'`]|wsUrl|websocketUrl|new WebSocket\(/.test(text) ? "/ws" : null,
        ]
          .filter((item): item is string => item !== null)
          .map((item) => `${relative(webRoot, path)}: ${item}`);
      });

    expect(offenders).toEqual([]);
  });

  it("keeps the generated OpenAPI mirror to bootstrap, status, News, and the trading reads", () => {
    const openapi = readFileSync(join(srcRoot, "lib/types/openapi.ts"), "utf8");
    const paths = [...openapi.matchAll(/^ {4}"(\/[^"]+)": \{$/gm)].map((match) => match[1]);

    // The `/api/trading/*` entries are named rather than matched by prefix (#207 PR-W4): the capital
    // lane's one hard rule is that no browser can place, amend or cancel an order, and a prefix would let
    // another route into the mirror without anyone reading this line. `/api/trading/gate` is the fourth
    // and is a read of the admission ledger (#269) — the durable answer to "why is there no case", which
    // the console previously had no way to ask for a page of frames at once. It carries no write.
    expect(paths.filter((path) => !path.startsWith("/api/news/"))).toEqual([
      "/api/bootstrap",
      "/api/status",
      "/api/trading/events/{event_id}",
      "/api/trading/gate",
      "/api/trading/orders",
      "/api/trading/status",
      "/healthz",
      "/metrics",
      "/readyz",
    ]);
    expect(openapi).not.toMatch(
      /SearchData|SearchInspectData|TokenCase|LiveMarket|TargetPostsData|TargetSocialTimelineData|ProviderStatus|NewsMarketMarkData|replay_limit/,
    );
  });

  it("keeps the frontend types facade free of hand-authored market and search shapes", () => {
    const contracts = readFileSync(join(srcRoot, "lib/types/frontend-contracts.ts"), "utf8");
    const index = readFileSync(join(srcRoot, "lib/types/index.ts"), "utf8");

    expect(contracts.trim()).toMatch(/^export type ApiResponse<T> = \{[\s\S]*\};$/);
    expect(index).not.toMatch(/Search|Token|Market|Event|LiveMarket|WindowKey/);
  });

  it("keeps the dev server proxy and env free of the retired WebSocket route", () => {
    const viteConfig = readFileSync(join(webRoot, "vite.config.ts"), "utf8");
    const packageJson = readFileSync(join(webRoot, "package.json"), "utf8");

    expect(viteConfig).not.toMatch(/"\/ws"|ws: true|WS_PROXY/);
    expect(packageJson).not.toContain("reconnecting-websocket");
  });

  it("keeps frontend tests, fixtures, and mocks on the News surface", () => {
    const offenders = collectFiles(testRoot)
      .filter((path) => sourceExtensions.has(extname(path)))
      .filter((path) => !relative(testRoot, path).startsWith("architecture/"))
      .flatMap((path) => {
        const text = readFileSync(path, "utf8");
        return [
          ...retiredApiPaths.filter((apiPath) => text.includes(apiPath)),
          ...retiredVocabulary.filter((word) => text.includes(word)),
          text.includes("routeWebSocket") ? "routeWebSocket" : null,
        ]
          .filter((item): item is string => item !== null)
          .map((item) => `${relative(webRoot, path)}: ${item}`);
      });

    expect(offenders).toEqual([]);
  });
});

function collectFiles(root: string): string[] {
  return readdirSync(root).flatMap((entry) => {
    const path = join(root, entry);
    return statSync(path).isDirectory() ? collectFiles(path) : [path];
  });
}
