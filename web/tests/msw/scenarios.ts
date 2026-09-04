import { appStatusFixture } from "@tests/fixtures/appRouteFixtures";
import {
  newsEventDetailFixture,
  newsFeedFixture,
  newsSymbolOiFrameFixture,
  newsStatusFixture,
  newsSymbolFixture,
} from "@tests/fixtures/newsFixture";
import {
  TRADING_NOW_MS,
  tradingGateFixture,
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
    const symbol = param("symbol");
    if (path === "/api/status") return ok(appStatusFixture());
    if (path === "/api/news/feed") {
      const feed = newsFeedFixture();
      return ok(
        symbol
          ? {
              ...feed,
              // On the case's clock: this frame is the one it was opened from.
              events: [newsSymbolOiFrameFixture(symbol, TRADING_NOW_MS - 400_000), ...feed.events],
            }
          : feed,
      );
    }
    if (path === "/api/news/status") return ok(newsStatusFixture());
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
    // #269: the durable admission ledger, read by the OI audit's admission column.
    if (path === "/api/trading/gate") return ok(tradingGateFixture());
    if (path.startsWith("/api/trading/gate/"))
      return ok({ decision: null, event_id: "evt", joinable: false });
    if (path.startsWith("/api/news/symbols/"))
      return ok(
        newsSymbolFixture({ base_symbol: decodeURIComponent(path.split("/").pop() ?? "WIF") }),
      );
    throw new Error(`unexpected path ${path}`);
  };
}
