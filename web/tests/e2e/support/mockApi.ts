import type { Page, Route } from "@playwright/test";
import {
  newsEventDetailFixture,
  newsEventFixture,
  newsFeedEventFixture,
  newsFeedFixture,
  newsOiFrameFixture,
  newsSymbolOiFrameFixture,
  newsOutcomeFixture,
  newsQuoteFixture,
  newsReactionFixture,
  newsStatusFixture,
  newsSymbolFixture,
  newsTriageFixture,
} from "@tests/fixtures/newsFixture";
import {
  TRADING_NOW_MS,
  tradingCasesForUnderlying,
  tradingExecutionFixture,
  tradingExecutionsFixture,
  tradingGateFixture,
  tradingSignalsForMarket,
  tradingStatusFixture,
} from "@tests/fixtures/tradingFixture";

const NOW = 1_777_746_300_000;
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
    // The OI monitor asks the same endpoint for one admission (#207), and the rows it gets back carry the
    // `oi` block. Serving the ordinary feed there would exercise only the degraded path.
    if (path === "/api/news/feed") {
      const symbol = url.searchParams.get("symbol");
      if (url.searchParams.get("admission") === "telemetry_deterministic") {
        return fulfill(route, newsOiFeedData(url.searchParams.get("oi")));
      }
      return fulfill(
        route,
        symbol && !options.emptyFeed
          ? newsSymbolFeedData(symbol)
          : newsFeedData(prepended, options.emptyFeed),
      );
    }
    if (path === "/api/news/status") return fulfill(route, newsStatusFixture());
    if (path === "/api/news/quotes") return fulfill(route, newsQuotesData(url));
    if (path.startsWith("/api/news/events/")) return fulfill(route, newsEventDetailData(path));
    // #207 PR-W1: identity is keyed on the path segment, so the token page's baseline names the base the
    // URL asked for rather than the fixture's default.
    // #207 PR-W4: the shell reads trading status on every route for the 交易 badge, so every e2e page
    // needs it answered or the unhandled-request assertion fires on routes that have nothing to do with it.
    if (path === "/api/trading/status") {
      return fulfill(
        route,
        tradingStatusFixture(
          options.tradingExecution ? { execution: options.tradingExecution } : undefined,
        ),
      );
    }
    // #282: the token page asks for one underlying, and what it gets back has to belong to that name —
    // otherwise 资本复盘 renders its empty path on every baseline and the panel is frozen in the one
    // state it is least useful in.
    if (path === "/api/trading/cases") {
      return fulfill(route, tradingCasesForUnderlying(url.searchParams.get("underlying")));
    }
    if (path === "/api/trading/signals") {
      return fulfill(route, tradingSignalsForMarket(url.searchParams.get("market")));
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
    // #269: the admission ledger the OI audit reads for a whole page of frames at once.
    if (path === "/api/trading/gate") return fulfill(route, tradingGateFixture());
    if (path.startsWith("/api/trading/gate/")) {
      return fulfill(route, {
        decision: null,
        event_id: path.split("/").pop() ?? "",
        joinable: false,
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

/**
 * The feed as the token page asks for it: this name's own OI frame on the same clock as its news.
 *
 * Production serves this shape: the page's own tab strip counts an OI 帧 lane, and answering a symbol
 * query with the generic rows every other route gets made it read 0 on every baseline.
 */
function newsSymbolFeedData(symbol: string) {
  const feed = newsFeedData();
  // The clock the case is on: this frame is the one it was opened from (`case_observed_at_ms`).
  const frame = newsSymbolOiFrameFixture(symbol, TRADING_NOW_MS - 400_000);
  return { ...feed, events: [frame, ...feed.events] };
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
 * Three frames, so the monitor's baseline shows the four measurements across a range of values and the
 * unparseable shape rather than one row repeated. Since #458 the lane has one judged rule and one failure
 * rule, so the tabs are 全部 and 解析失败.
 */
function newsOiFeedData(oi: string | null) {
  const frames = [
    newsOiFrameFixture(),
    newsOiFrameFixture({
      assets: [{ base_symbol: "DOGE", listed: true, symbol: "DOGE", venue: "binance.perp" }],
      event_id: "evt-oi-doge",
      leader_title:
        "DOGE OI Rise 2.08%, OI Value 892.31M, Whale Long Profit 63.00%, Whale/OI Ratio 54.20%",
      oi: {
        ...newsOiFrameFixture().oi!,
        oi_change_bps: 208,
        oi_value_usd: 892_310_000,
        symbol: "DOGE",
        whale_long_profit_bps: 6_300,
        whale_oi_ratio_bps: 5_420,
      },
      outcome: newsOutcomeFixture({
        group: "held",
        kind: "dropped",
        reason_zh: "持仓异动按客观规则判断",
        text_zh: "未推送",
      }),
      reaction: newsReactionFixture({
        p0: "0.2180",
        return_1h_bps: 84,
        return_4h_bps: -31,
      }),
      triage: newsTriageFixture({
        assets: [{ role: "primary", symbol: "DOGE" }],
        direction: "bullish",
        direction_zh: "利多",
        final_decision: "drop",
        headline_zh: "▲ DOGE 持仓异动2.08%",
        override_rule: "telemetry_deterministic",
      }),
    }),
    newsOiFrameFixture({
      assets: [],
      event_id: "evt-oi-pengu",
      leader_title:
        "PENGU OI Rise 3.4%, OI Value --, Whale Long Profit 55.10%, Whale/OI Ratio 71.00%",
      oi: {
        failure_stage: "template_match",
        oi_change_bps: null,
        oi_value_usd: null,
        parsed: false,
        parser_version: "oi_signal_parser_v1",
        rule: "oi_parse_failed",
        // The template never matched, so nothing — not even the subject — was read out of the title.
        symbol: null,
        title_sha256: "9f2c41ab7d10",
        whale_long_profit_bps: null,
        whale_oi_ratio_bps: null,
      },
      outcome: newsOutcomeFixture({
        group: "held",
        kind: "dropped",
        reason_zh: "持仓异动供应商格式无法解析，已安全拦截",
        text_zh: "未推送",
      }),
      reaction: null,
      triage: newsTriageFixture({
        assets: [],
        // An unparseable frame carries no direction: nothing was measured, so nothing is stated (#207).
        direction: "neutral",
        direction_zh: "中性",
        error_code: "oi_parse_failed",
        final_decision: "drop",
        headline_zh: "持仓异动帧无法解析",
        override_rule: "telemetry_deterministic",
      }),
    }),
  ];
  const wanted = { parse_failed: 2 }[oi ?? ""];
  return {
    ...newsFeedFixture(),
    events: wanted == null ? frames : [frames[wanted]],
  };
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
