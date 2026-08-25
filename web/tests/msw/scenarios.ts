import { appStatusFixture } from "@tests/fixtures/appRouteFixtures";
import {
  newsEventDetailFixture,
  newsFeedFixture,
  newsReviewFixture,
  newsStatusFixture,
  newsSymbolFixture,
} from "@tests/fixtures/newsFixture";

import type { ApiMock } from "./fixtures";
import { defaultBootstrap, ok } from "./fixtures";

export function mockBootstrap(apiMock: ApiMock) {
  apiMock.getBootstrapImpl = async () => defaultBootstrap();
}

export function mockAppRoutes(apiMock: ApiMock) {
  apiMock.getApiImpl = async (path) => {
    if (path === "/api/status") return ok(appStatusFixture());
    if (path === "/api/news/feed") return ok(newsFeedFixture());
    if (path === "/api/news/status") return ok(newsStatusFixture());
    // #88: the price surfaces answer on every route because the shell reads the review summary.
    if (path === "/api/news/quotes") return ok({ measured_at_ms: 0, quotes: [] });
    if (path === "/api/news/review") return ok(newsReviewFixture());
    if (path.startsWith("/api/news/events/")) return ok(newsEventDetailFixture());
    // #207 PR-W1: keyed on the path segment so a route test that visits `/news/symbols/WIF` gets WIF back.
    if (path.startsWith("/api/news/symbols/"))
      return ok(
        newsSymbolFixture({ base_symbol: decodeURIComponent(path.split("/").pop() ?? "WIF") }),
      );
    throw new Error(`unexpected path ${path}`);
  };
}
