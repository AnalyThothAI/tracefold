import { APP_NAVIGATION_GROUPS } from "@features/cockpit/ui/appNavigation";
import * as apiClient from "@lib/api/client";
import { createAppRouteObjects } from "@routes/router";
import { describe, expect, it } from "vitest";

/*
 * The browser half of the public surface, and only that half. The `/api/*` path list, the one write
 * operation and every other operation's read-only shape belong to `tests/contract/test_openapi_drift.py`,
 * which regenerates the document from the live FastAPI app instead of re-reading the committed copy: a
 * second reader of `docs/generated/openapi.json` can only ever repeat what the generator already proved,
 * and it repeated it out of date (#589 PR-5).
 */
describe("public browser surface", () => {
  it("exposes only the maintained SPA routes and navigation destinations", () => {
    expect(collectRoutePaths(createAppRouteObjects()).sort()).toEqual([
      "*",
      "news",
      "news/events/:eventId",
      "news/market",
      "news/status",
      "news/symbols/:base",
      "news/wallets",
      "trading",
    ]);

    const navigation = APP_NAVIGATION_GROUPS.flatMap((group) => group.items);
    expect(navigation.map((item) => item.to)).toEqual([
      "/news",
      "/news/market",
      "/news/wallets",
      "/trading",
    ]);
    expect(navigation.flatMap((item) => item.children ?? [])).toEqual([]);
  });

  it("keeps the runtime API facade to GET plus the one POST transport", () => {
    expect(Object.keys(apiClient)).toEqual(
      expect.arrayContaining(["getApi", "postApi", "getBootstrap", "getAuthToken", "setAuthToken"]),
    );
    expect(apiClient).not.toHaveProperty("putApi");
    expect(apiClient).not.toHaveProperty("patchApi");
    expect(apiClient).not.toHaveProperty("deleteApi");
  });
});

function collectRoutePaths(routes: ReturnType<typeof createAppRouteObjects>): string[] {
  return routes.flatMap((route) => [
    ...(route.path === undefined ? [] : [route.path]),
    ...(route.children ? collectRoutePaths(route.children) : []),
  ]);
}
