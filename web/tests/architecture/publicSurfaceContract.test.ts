import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { APP_NAVIGATION_GROUPS } from "@features/cockpit/ui/appNavigation";
import * as apiClient from "@lib/api/client";
import { createAppRouteObjects } from "@routes/router";
import { describe, expect, it } from "vitest";

const webRoot = join(dirname(fileURLToPath(import.meta.url)), "../..");

describe("public browser surface", () => {
  it("exposes only the maintained SPA routes and navigation destinations", () => {
    expect(collectRoutePaths(createAppRouteObjects()).sort()).toEqual([
      "*",
      "news",
      "news/alpha",
      "news/events/:eventId",
      "news/oi",
      "news/status",
      "news/symbols/:base",
      "trading",
    ]);

    const navigation = APP_NAVIGATION_GROUPS.flatMap((group) => group.items);
    expect(navigation.map((item) => item.to)).toEqual([
      "/news",
      "/news/alpha",
      "/trading",
      "/news/oi",
    ]);
    expect(navigation.flatMap((item) => item.children ?? [])).toEqual([]);
  });

  it("keeps every generated browser API operation read-only", () => {
    const document = JSON.parse(
      readFileSync(join(webRoot, "../docs/generated/openapi.json"), "utf8"),
    ) as { paths?: Record<string, Record<string, unknown>> };
    const paths = document.paths ?? {};
    const apiPaths = Object.keys(paths)
      .filter((path) => path.startsWith("/api/"))
      .sort();

    expect(apiPaths).toEqual([
      "/api/bootstrap",
      "/api/news/events/{event_id}",
      "/api/news/feed",
      "/api/news/quotes",
      "/api/news/status",
      "/api/news/symbols/{base}",
      "/api/status",
      "/api/trading/cases",
      "/api/trading/execution/observations",
      "/api/trading/gate",
      "/api/trading/gate/{event_id}",
      "/api/trading/signals",
      "/api/trading/status",
    ]);

    const operations = apiPaths.flatMap((path) =>
      Object.keys(paths[path] ?? {})
        .filter((method) => HTTP_METHODS.has(method))
        .map((method) => `${method.toUpperCase()} ${path}`),
    );
    expect(operations.every((operation) => operation.startsWith("GET "))).toBe(true);
    expect(operations).toHaveLength(apiPaths.length);
  });

  it("keeps the runtime API facade without a browser write verb", () => {
    expect(Object.keys(apiClient)).toEqual(
      expect.arrayContaining(["getApi", "getBootstrap", "getAuthToken", "setAuthToken"]),
    );
    expect(apiClient).not.toHaveProperty("postApi");
  });
});

const HTTP_METHODS = new Set(["get", "post", "put", "patch", "delete"]);

function collectRoutePaths(routes: ReturnType<typeof createAppRouteObjects>): string[] {
  return routes.flatMap((route) => [
    ...(route.path === undefined ? [] : [route.path]),
    ...(route.children ? collectRoutePaths(route.children) : []),
  ]);
}
