import { appStatusFixture } from "@tests/fixtures/appRouteFixtures";
import {
  newsEventDetailFixture,
  newsFeedFixture,
  newsMarketFixture,
  newsMarketItemFixture,
  newsMarketObservationFixture,
  newsStatusFixture,
  newsSymbolFixture,
  newsWalletCardsFixture,
  newsWalletsFixture,
} from "@tests/fixtures/newsFixture";
import {
  tradingCasesForUnderlying,
  tradingExecutionsFixture,
  tradingStatusFixture,
} from "@tests/fixtures/tradingFixture";

import type { ApiMock } from "./fixtures";
import { defaultBootstrap, ok } from "./fixtures";

export function mockBootstrap(apiMock: ApiMock) {
  apiMock.getBootstrapImpl = async () => defaultBootstrap();
}

export function mockAppRoutes(apiMock: ApiMock) {
  apiMock.getApiImpl = async (path, options) => {
    /*
     * `getApi` carries its query in `options.params`, never on the path (#282). Reading `underlying` off
     * the path's search string found nothing at all, so the filter these mocks exist to model was a no-op
     * and a token page got back every other name's cases.
     */
    const param = (key: string) => {
      const value = options?.params?.[key];
      return value == null ? null : String(value);
    };
    if (path === "/api/status") return ok(appStatusFixture());
    if (path === "/api/news/feed") return ok(newsFeedFixture());
    if (path === "/api/news/status") return ok(newsStatusFixture());
    /*
     * #553 PR-1: market observations are their own endpoint, not a slice of the feed. The list answers a
     * kind subset the same way the server does — the filter is a real request, so a mock that ignored it
     * would let a browser-side split pass.
     */
    if (path === "/api/news/market") {
      const kinds = (param("kind") ?? "").split(",").filter(Boolean);
      const market = newsMarketFixture();
      return ok(
        kinds.length
          ? {
              ...market,
              filters: { ...market.filters, kind: param("kind") },
              groups: market.groups.filter((group) => kinds.includes(group.market_kind)),
            }
          : market,
      );
    }
    if (path.startsWith("/api/news/market/")) {
      const itemId = decodeURIComponent(path.split("/").pop() ?? "");
      return ok(
        newsMarketItemFixture({ observation: newsMarketObservationFixture({ item_id: itemId }) }),
      );
    }
    /*
     * #572 PR-3: the wallet tape's own two reads. The card list answers the window it was asked for,
     * because the window is a real request — a mock that ignored it would let a browser-side slice pass.
     */
    if (path === "/api/news/wallets") return ok(newsWalletsFixture());
    if (path === "/api/news/wallets/cards") {
      const window = param("window") ?? "24h";
      return ok(newsWalletCardsFixture({ window }));
    }
    if (path === "/api/news/quotes") return ok({ measured_at_ms: 0, quotes: [] });
    if (path.startsWith("/api/news/events/")) return ok(newsEventDetailFixture());
    // #537 PR-5: only `/trading` reads this now. The shell polled it on every News route for a
    // sidebar badge and two chrome figures until the badge and the figures were deleted.
    if (path === "/api/trading/status") return ok(tradingStatusFixture());
    // #282: the endpoint filters by `underlying`, so a mock that ignored it handed a token page a case
    // for a different name, carrying an `event_id` no loaded frame matches.
    if (path.startsWith("/api/trading/cases")) {
      return ok(tradingCasesForUnderlying(param("underlying")));
    }
    if (path === "/api/trading/executions") return ok(tradingExecutionsFixture());
    if (path.startsWith("/api/news/symbols/"))
      return ok(
        newsSymbolFixture({ base_symbol: decodeURIComponent(path.split("/").pop() ?? "WIF") }),
      );
    throw new Error(`unexpected path ${path}`);
  };
}
