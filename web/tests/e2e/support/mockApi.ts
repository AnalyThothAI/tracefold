import type { Page, Route } from "@playwright/test";
import {
  newsEventDetailFixture,
  newsEventFixture,
  newsFeedEventFixture,
  newsFeedFixture,
  newsMarketFixture,
  newsMarketGroupFixture,
  newsMarketItemFixture,
  newsMarketObservationFixture,
  newsQuoteFixture,
  newsStatusFixture,
  newsSymbolFixture,
} from "@tests/fixtures/newsFixture";
import {
  TRADING_NOW_MS,
  tradingCasesForUnderlying,
  tradingExecutionFixture,
  tradingExecutionsFixture,
  tradingStatusFixture,
} from "@tests/fixtures/tradingFixture";

const NOW = 1_777_746_300_000;
/* The market fixtures' own clock, so a group's window reads as minutes rather than as a month of drift. */
const MARKET_NOW = 1_779_000_000_000;
const unhandledApiRequests = new WeakMap<Page, string[]>();

export type MockApiOptions = {
  bootstrapStatus?: number;
  delayNonBootstrapMs?: number;
  emptyFeed?: boolean;
  failNonBootstrap?: boolean;
  tradingExecution?: ReturnType<typeof tradingExecutionFixture>;
  tradingExecutions?: ReturnType<typeof tradingExecutionsFixture>;
};

export type MockFeedControl = {
  /** Put a newer Event at the top of the next poll, the way a live feed does. */
  prependEvent: (eventId: string) => void;
};

export async function installMockApi(
  page: Page,
  options: MockApiOptions = {},
): Promise<MockFeedControl> {
  unhandledApiRequests.set(page, []);
  const prepended: string[] = [];

  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;

    if (path !== "/api/bootstrap") {
      if (options.delayNonBootstrapMs) {
        await new Promise((resolve) => setTimeout(resolve, options.delayNonBootstrapMs));
      }
      if (options.failNonBootstrap) {
        return route.abort("failed");
      }
    }

    if (path === "/api/bootstrap" && options.bootstrapStatus) {
      return route.fulfill({
        status: options.bootstrapStatus,
        contentType: "application/json",
        body: JSON.stringify({ ok: false, error: "unauthorized" }),
      });
    }
    if (path === "/api/bootstrap") return fulfill(route, { ws_token: "secret" });
    if (path === "/api/status") return fulfill(route, statusData());
    if (path === "/api/news/feed") {
      return fulfill(route, newsFeedData(prepended, options.emptyFeed));
    }
    if (path === "/api/news/status") return fulfill(route, newsStatusFixture());
    // #553 PR-1: market observations are their own endpoint. The mock narrows on `kind` because the page's
    // filter is a real request, and a mock that ignored it would let a browser-side split pass a baseline.
    if (path === "/api/news/market") return fulfill(route, marketData(url));
    if (path.startsWith("/api/news/market/")) {
      const itemId = decodeURIComponent(path.split("/").pop() ?? "");
      return fulfill(
        route,
        newsMarketItemFixture({ observation: newsMarketObservationFixture({ item_id: itemId }) }),
      );
    }
    if (path === "/api/news/quotes") return fulfill(route, newsQuotesData(url));
    if (path.startsWith("/api/news/events/")) return fulfill(route, newsEventDetailData(path));
    // #207 PR-W1: identity is keyed on the path segment, so the token page's baseline names the base the
    // URL asked for rather than the fixture's default.
    // #537 PR-5: only `/trading` reads trading status now, but the mock still answers it everywhere —
    // an unhandled-request assertion on a route that never asks costs nothing and one that does is real.
    if (path === "/api/trading/status") {
      return fulfill(
        route,
        tradingStatusFixture(
          options.tradingExecution ? { execution: options.tradingExecution } : undefined,
        ),
      );
    }
    // #282: a caller that asks for one underlying has to get that name back. `/trading` asks for none
    // and gets the whole window, which is what the Case drawer opens one row of.
    if (path === "/api/trading/cases") {
      return fulfill(route, tradingCasesForUnderlying(url.searchParams.get("underlying")));
    }
    if (path === "/api/trading/executions") {
      return fulfill(route, options.tradingExecutions ?? tradingExecutionsFixture());
    }
    if (path === "/api/trading/execution/commands") {
      return fulfill(route, {
        command_id: "a".repeat(64),
        disposition: "awaiting_runtime",
        reason: null,
        requested_at_ns: TRADING_NOW_MS * 1_000_000,
        seq: 7,
        truth: "intent_recorded_not_runtime_or_venue",
      });
    }
    if (path.startsWith("/api/news/symbols/")) {
      return fulfill(
        route,
        newsSymbolFixture({ base_symbol: decodeURIComponent(path.split("/").pop() ?? "WIF") }),
      );
    }
    recordUnhandledApiRequest(page, url);
    return route.fulfill({
      status: 404,
      contentType: "application/json",
      body: JSON.stringify({ ok: false, error: `unhandled ${path}` }),
    });
  });

  return { prependEvent: (eventId: string) => prepended.push(eventId) };
}

export function getUnhandledApiRequests(page: Page): string[] {
  return [...(unhandledApiRequests.get(page) ?? [])];
}

function recordUnhandledApiRequest(page: Page, url: URL) {
  const requests = unhandledApiRequests.get(page) ?? [];
  requests.push(`${url.pathname}${url.search}`);
  unhandledApiRequests.set(page, requests);
}

function newsFeedData(prepended: string[] = [], empty = false) {
  const event = newsFeedEventFixture({
    event_id: "evt-global-policy",
    leader_description:
      "Liquidity rotation is visible across crypto beta and rates-sensitive assets.",
    leader_title: "Macro desk flags liquidity rotation",
  });
  const row = (eventId: string, title: string) => ({
    ...event,
    // No Chinese headline on the mock rows, so the wire line is the row headline (distinct per row).
    triage: event.triage ? { ...event.triage, headline_zh: null } : null,
    event_id: eventId,
    leader_title: title,
  });
  const data = {
    ...newsFeedFixture(),
    events: [
      ...prepended.map((eventId) => row(eventId, `Breaking ${eventId}`)),
      ...Array.from({ length: 5 }, (_, index) =>
        index === 0
          ? row(event.event_id, event.leader_title)
          : row(`evt-global-policy-${index + 1}`, `Global policy update ${index + 1}`),
      ),
    ],
  };
  return empty ? { ...data, events: [] } : data;
}

/**
 * Four groups, so the baseline shows every kind the endpoint serves and both parse states rather than one
 * row repeated: a parsed OI run, a liquidation, a smart-money print, and an unknown source retained raw.
 */
function marketData(url: URL) {
  const groups = [
    newsMarketGroupFixture(),
    newsMarketGroupFixture({
      first_event_at_ms: MARKET_NOW - 260_000,
      latest: newsMarketObservationFixture({
        event_at_ms: MARKET_NOW - 240_000,
        group_key: "liquidation:DOGE",
        item_id: "mkt-liq-doge-1",
        market_kind: "liquidation",
        notional_usd: "412530.00",
        oi_change_bps: null,
        oi_value_usd: null,
        liquidated_position_side: "long",
        price: "0.2181",
        received_at_ms: MARKET_NOW - 239_000,
        symbol: "DOGE",
        title: "DOGE Long Liquidation 412.53K at 0.2181",
        whale_long_profit_bps: null,
        whale_oi_ratio_bps: null,
      }),
      observation_count: 1,
    }),
    newsMarketGroupFixture({
      first_event_at_ms: MARKET_NOW - 620_000,
      latest: newsMarketObservationFixture({
        account_address: "0x9f2c41ab7d10",
        action: "open",
        event_at_ms: MARKET_NOW - 600_000,
        group_key: "smart_money:0x9f2c",
        item_id: "mkt-sm-1",
        market_kind: "smart_money",
        notional_usd: "1250000.00",
        oi_change_bps: null,
        oi_value_usd: null,
        position_side: "long",
        received_at_ms: MARKET_NOW - 599_000,
        symbol: "ETH",
        title: "Smart money opened ETH long 1.25M",
        trader_label: "whale-0x9f2c",
        whale_long_profit_bps: null,
        whale_oi_ratio_bps: null,
      }),
      observation_count: 2,
    }),
    /*
     * A source with no parser. It is retained with its raw line and a stated reason, and it is a kind
     * rather than a failure — the baseline exists so that stays visible.
     */
    newsMarketGroupFixture({
      first_event_at_ms: MARKET_NOW - 1_820_000,
      latest: newsMarketObservationFixture({
        event_at_ms: MARKET_NOW - 1_800_000,
        group_key: "unknown_market:2026",
        item_id: "mkt-unknown-1",
        market_kind: "unknown_market",
        oi_change_bps: null,
        oi_value_usd: null,
        parse_error: "no_parser_for_source",
        parse_status: "raw",
        received_at_ms: MARKET_NOW - 1_799_000,
        symbol: null,
        title: "PENGU OI Rise 3.4%, OI Value --, Whale Long Profit 55.10%",
        whale_long_profit_bps: null,
        whale_oi_ratio_bps: null,
      }),
      observation_count: 1,
    }),
  ];
  const kinds = (url.searchParams.get("kind") ?? "").split(",").filter(Boolean);
  const market = newsMarketFixture({ groups });
  return kinds.length
    ? {
        ...market,
        filters: { ...market.filters, kind: url.searchParams.get("kind") },
        groups: groups.filter((group) => kinds.includes(group.market_kind)),
      }
    : market;
}

function newsEventDetailData(path: string) {
  const eventId = decodeURIComponent(path.split("/").pop() ?? "evt-global-policy");
  return newsEventDetailFixture({
    event: newsEventFixture({
      event_id: eventId,
      leader_description:
        "Liquidity rotation is visible across crypto beta and rates-sensitive assets.",
      leader_title: "Macro desk flags liquidity rotation",
    }),
  });
}

async function fulfill(route: Route, data: unknown) {
  return route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({ ok: true, data }),
  });
}

function statusData() {
  return {
    measured_at_ms: NOW,
    runtime: {
      ok: true,
      reasons: [],
      db: {
        ok: true,
        schema_ok: true,
        current_revision: "20260812_0255",
        expected_revision: "20260812_0255",
        error_code: null,
      },
      workers_runtime: {
        runtime_id: "1d36ca48-c41d-4d7b-a26d-86c2429a3e10",
        runtime_version: "a521557",
        state: "running",
        started_at_ms: NOW,
        heartbeat_at_ms: NOW,
        heartbeat_stale_after_ms: 15_000,
        fatal_code: null,
        unavailable_reason: null,
      },
    },
  };
}

/** One quote per requested symbol, exactly like the server: a symbol it cannot price says `unlisted`. */
function newsQuotesData(url: URL) {
  const symbols = (url.searchParams.get("symbols") ?? "").split(",").filter(Boolean);
  const prices: Record<string, string> = {
    DOGE: "0.2191",
    ETH: "3521.80",
    WIF: "0.8431",
  };
  return {
    measured_at_ms: NOW,
    quotes: symbols.map((symbol) =>
      newsQuoteFixture({
        base_symbol: symbol,
        price: prices[symbol] ?? "68123.4",
        requested_symbol: symbol,
        symbol,
        venue_symbol: `${symbol}USDT`,
      }),
    ),
  };
}
