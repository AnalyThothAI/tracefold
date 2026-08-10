import {
  appStatusFixture,
  searchInspectFixture,
  targetSocialTimelineFixture,
  tokenRadarFixture,
} from "@tests/fixtures/appRouteFixtures";
import {
  newsFeedFixture,
  newsGlobalBriefFixture,
  newsSourcesFixture,
  newsStatusFixture,
} from "@tests/fixtures/newsFixture";
import { tokenCaseFixture, tokenCasePostsFixture } from "@tests/fixtures/tokenCaseFixture";

import type { ApiMock } from "./fixtures";
import { defaultBootstrap, ok } from "./fixtures";

export function mockBootstrap(apiMock: ApiMock) {
  apiMock.getBootstrapImpl = async () => defaultBootstrap();
}

export function mockLiveRadarRoute(apiMock: ApiMock) {
  apiMock.getApiImpl = async (path, requestOptions) => {
    if (path === "/api/status") return ok(appStatusFixture());
    if (path === "/api/token-radar") return ok(tokenRadarFixture());
    if (path === "/api/news/feed") return ok(newsFeedFixture());
    if (path === "/api/news/brief") return ok(newsGlobalBriefFixture());
    if (path === "/api/news/status") return ok(newsStatusFixture());
    if (path === "/api/news/sources") return ok(newsSourcesFixture());
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

function tokenCaseSearchInspectFixture(q: string) {
  const dossier = tokenCaseFixture();
  return searchInspectFixture({
    query: {
      q,
      normalized_q: q.toLowerCase(),
      window: "24h",
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
