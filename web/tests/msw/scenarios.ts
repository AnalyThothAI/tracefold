import { appStatusFixture } from "@tests/fixtures/appRouteFixtures";
import {
  newsEventDetailFixture,
  newsFeedFixture,
  newsStatusFixture,
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
    if (path.startsWith("/api/news/events/")) return ok(newsEventDetailFixture());
    throw new Error(`unexpected path ${path}`);
  };
}
