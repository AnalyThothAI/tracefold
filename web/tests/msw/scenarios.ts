import {
  appStatusFixture,
  notificationSummaryFixture,
  notificationFixture,
  searchInspectFixture,
  targetSocialTimelineFixture,
  tokenRadarFixture,
} from "@tests/fixtures/appRouteFixtures";
import { tokenCaseFixture, tokenCasePostsFixture } from "@tests/fixtures/tokenCaseFixture";

import type { ApiMock } from "./fixtures";
import { defaultBootstrap, ok } from "./fixtures";

export function mockBootstrap(apiMock: ApiMock) {
  apiMock.getBootstrapImpl = async () => defaultBootstrap();
}

export function mockLiveRadarRoute(apiMock: ApiMock) {
  const summary = notificationSummaryFixture();
  apiMock.getApiImpl = async (path, requestOptions) => {
    if (path === "/api/status") return ok(appStatusFixture());
    if (path === "/api/notifications") {
      return ok({ items: [], summary });
    }
    if (path === "/api/token-radar") return ok(tokenRadarFixture());
    if (path === "/api/stocks-radar") return ok(stocksRadarFixture());
    if (path === "/api/news/stories") return ok(newsStoriesFixture());
    if (path === "/api/token-case") return ok(tokenCaseFixture());
    if (path === "/api/search/inspect") {
      const q = String(requestOptions?.params?.q ?? "$RKC");
      if (q.toLowerCase().includes("hansa")) return ok(tokenCaseSearchInspectFixture(q));
      return ok(searchInspectFixture({ query: { ...searchInspectFixture().query, q } }));
    }
    if (path === "/api/target-social-timeline") return ok(targetSocialTimelineFixture());
    if (path === "/api/target-posts") return ok(tokenCasePostsFixture());
    throw new Error(`unexpected path ${path}`);
  };
}

export function mockNotificationRoute(apiMock: ApiMock) {
  const summary = {
    ...notificationSummaryFixture(),
    unread_count: 1,
    high_unread_count: 1,
    account_unread_counts: { traderpow: 1 },
  };
  const notification = notificationFixture();
  apiMock.getApiImpl = async (path) => {
    if (path === "/api/status") {
      return ok(appStatusFixture());
    }
    if (path === "/api/notifications") return ok({ items: [notification], summary });
    if (path === "/api/token-radar") return ok(tokenRadarFixture());
    if (path === "/api/stocks-radar") return ok(stocksRadarFixture());
    if (path === "/api/news/stories") return ok(newsStoriesFixture());
    if (path === "/api/token-case") return ok(tokenCaseFixture());
    if (path === "/api/target-social-timeline") return ok(targetSocialTimelineFixture());
    if (path === "/api/target-posts") return ok(tokenCasePostsFixture());
    throw new Error(`unexpected path ${path}`);
  };
}

function newsStoriesFixture() {
  return { items: [], next_cursor: null };
}

function stocksRadarFixture() {
  return {
    window: "1h",
    scope: "all",
    rows: [],
    health: { returned_count: 0, quote_ready_count: 0, quote_unavailable_count: 0 },
  };
}

function tokenCaseSearchInspectFixture(q: string) {
  const dossier = tokenCaseFixture();
  return searchInspectFixture({
    query: {
      q,
      normalized_q: q.toLowerCase(),
      window: "24h",
      scope: "all",
      result_kind: "token_result",
    },
    resolver: {
      target_candidates: [dossier.target],
      selected_target: dossier.target,
      reasons: ["msw_token_case_fixture"],
    },
    token_result: dossier,
    topic_result: null,
    ambiguous_result: null,
  });
}
