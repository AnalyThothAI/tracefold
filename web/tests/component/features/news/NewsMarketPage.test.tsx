import { NewsPage } from "@features/news";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import {
  NEWS_NOW_MS,
  newsMarketFixture,
  newsMarketGroupFixture,
  newsMarketItemFixture,
  newsMarketObservationFixture,
  newsStatusFixture,
} from "@tests/fixtures/newsFixture";
import { server } from "@tests/msw/server";
import { HttpResponse, http } from "msw";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

/**
 * 市场事实 (#553 PR-1).
 *
 * The page exists because OI frames, liquidations, smart-money prints and unparsed market sources stopped
 * being Events: they are stored observations read through `/api/news/market`, and everything the console
 * says about them has to come from that read. Most of what is asserted here is the page not inventing
 * anything the server did not say — and not letting a second endpoint's failure take the observations
 * with it, which is exactly what the page it replaced did.
 */
describe("NewsMarketPage", () => {
  beforeEach(() => {
    server.use(
      http.get(/.*\/api\/news\/market\/.+$/, ({ params }) =>
        HttpResponse.json({
          ok: true,
          data: newsMarketItemFixture({
            observation: newsMarketObservationFixture({
              item_id: String((params as { 0?: string })[0] ?? "mkt-oi-wif-3"),
            }),
          }),
        }),
      ),
      http.get(/.*\/api\/news\/market$/, () =>
        HttpResponse.json({ ok: true, data: marketWithEveryKind() }),
      ),
      http.get(/.*\/api\/news\/status$/, () =>
        HttpResponse.json({ ok: true, data: newsStatusFixture() }),
      ),
    );
  });

  afterEach(cleanup);

  it("reads the market endpoint alone, and no Trading endpoint at all", async () => {
    /*
     * A market observation is not an Event, so there is no `event_id` for an admission verdict to join on
     * and nothing here for `/api/trading/gate` to answer. The page it replaced polled that endpoint for a
     * whole page of frames on every visit; asserting the request count is what keeps it from coming back.
     */
    const paths: string[] = [];
    server.use(
      http.get(/.*\/api\/.*/, ({ request }) => {
        paths.push(new URL(request.url).pathname);
        return HttpResponse.json({ ok: true, data: marketWithEveryKind() });
      }),
    );

    renderMarket();
    await screen.findByRole("heading", { name: "市场事实" });

    await waitFor(() => expect(paths).toContain("/api/news/market"));
    expect(paths.filter((path) => path.startsWith("/api/trading"))).toEqual([]);
    // And the observations are on screen while that is true, so "Trading is unavailable" is not a
    // state this page can be in: it never asks.
    expect(await screen.findByText(/WIF OI Rise 6.71%/)).toBeInTheDocument();
    expect(document.querySelectorAll(".news-market-row")).toHaveLength(3);
  });

  it("renders the observations when the pipeline status read fails", async () => {
    /*
     * The specific defect this page was cut to remove. Its predecessor wrapped its whole body in a
     * `PageState.Stale` gated on `/api/news/status`, so a 5xx on a pipeline dashboard endpoint blanked
     * the market data. Status is a supporting read: it may name its own failure and nothing else.
     */
    server.use(
      http.get(/.*\/api\/news\/status$/, () =>
        HttpResponse.json({ ok: false, error: "news status unavailable" }, { status: 503 }),
      ),
    );

    renderMarket();

    expect(await screen.findByText(/WIF OI Rise 6.71%/)).toBeInTheDocument();
    expect(await screen.findByText("状态未知")).toBeInTheDocument();
    expect(screen.getByText(/未受影响/)).toBeInTheDocument();
    expect(document.querySelectorAll(".news-market-row")).toHaveLength(3);
  });

  it("prints each group's own push status and reason, and no page-level channel banner", async () => {
    /*
     * #553 PR-2. There is no "is push wired" flag any more, and a banner would be a second, weaker
     * answer to a question every row already answers: one group was sent, another is still merging,
     * and both are true at the same moment.
     */
    renderMarket();
    await screen.findByText(/WIF OI Rise 6.71%/);

    expect(screen.queryByText("推送通道")).not.toBeInTheDocument();
    // Printed as written, never glossed: the operator greps these strings.
    expect(screen.getAllByText("sent").length).toBeGreaterThan(0);
    expect(screen.getByText("merging")).toBeInTheDocument();
    expect(screen.getByText("liquidation_followup_window_open")).toBeInTheDocument();
  });

  it("renders the frozen card and the observations it covered, not just their count", async () => {
    /*
     * #553 PR-2. The operator's real-channel receipt check is "does the message my channel received
     * match the snapshot the ledger froze". That comparison is impossible if the console only prints
     * how many observations the card spoke for, so both the card's own lines and the covered ids are
     * on the page.
     */
    renderMarket();
    const rows = await screen.findAllByRole("button", { expanded: false });
    fireEvent.click(rows[0]);

    const snapshot = await screen.findByText("持仓异动 WIF");
    expect(snapshot).toBeInTheDocument();
    expect(screen.getByText(/发送快照 · 2 条观测/)).toBeInTheDocument();
    expect(screen.getByText(/覆盖观测 · 1/)).toBeInTheDocument();
    expect(screen.getByText("mkt-oi-wif-3")).toBeInTheDocument();
  });

  it("says so when a prepared card has not frozen a snapshot yet", async () => {
    server.use(
      http.get(/.*\/api\/news\/market\/.+$/, () =>
        HttpResponse.json({
          ok: true,
          data: newsMarketItemFixture({
            notification_covered_item_ids: [],
            notification_delivery: {
              ...newsMarketItemFixture().notification_delivery!,
              attempts: 0,
              card: {},
              state: "pending",
            },
          }),
        }),
      ),
    );
    renderMarket();
    const rows = await screen.findAllByRole("button", { expanded: false });
    fireEvent.click(rows[0]);

    expect(await screen.findByText(/快照要到首次尝试才冻结/)).toBeInTheDocument();
    expect(screen.getByText(/还没有认领任何观测/)).toBeInTheDocument();
  });

  it("keeps the parse answer and the push answer in separate cells", async () => {
    renderMarket();
    const rows = await screen.findAllByRole("button", { expanded: false });

    // A record that parsed cleanly and was never pushed is not a parse failure, and the row says both.
    const oi = rows[0];
    expect(within(oi).getByText("解析")).toBeInTheDocument();
    expect(within(oi).getByText("已解析")).toBeInTheDocument();
    expect(within(oi).getByText("推送")).toBeInTheDocument();
    expect(within(oi).getByText("sent")).toBeInTheDocument();
    expect(oi.querySelectorAll(".news-market-flag")).toHaveLength(2);
  });

  it("keeps a retained-but-unparsed observation on screen with its own reason", async () => {
    // An `unknown_market` source has no parser at all: the record is stored raw and the row is the only
    // place an operator sees it. Dropping it, or reading numbers out of its title, are both inventions.
    renderMarket();
    await screen.findByText(/WIF OI Rise 6.71%/);

    const raw = document.querySelector('.news-market-row[data-kind="unknown_market"]');
    expect(raw).not.toBeNull();
    expect(raw).toHaveTextContent("仅原文");
    expect(raw).toHaveTextContent("no_parser_for_source");
    expect(raw).toHaveTextContent("PENGU OI Rise 3.4%");
    // Nothing was read out of the title, so the subject cell says nothing.
    expect(raw?.querySelector(".news-market-subject")?.textContent).toBe("—");
  });

  it("puts the chosen kinds in the URL and narrows the request with them", async () => {
    const observed: string[] = [];
    server.use(
      http.get(/.*\/api\/news\/market$/, ({ request }) => {
        const kind = new URL(request.url).searchParams.get("kind");
        observed.push(kind ?? "");
        const market = marketWithEveryKind();
        const kinds = (kind ?? "").split(",").filter(Boolean);
        return HttpResponse.json({
          ok: true,
          data: kinds.length
            ? {
                ...market,
                filters: { ...market.filters, kind },
                groups: market.groups.filter((group) => kinds.includes(group.market_kind)),
              }
            : market,
        });
      }),
    );

    renderMarket();
    await screen.findByText(/WIF OI Rise 6.71%/);
    await waitFor(() => expect(observed).toContain(""));

    fireEvent.click(screen.getByRole("button", { name: "清算" }));

    await waitFor(() => expect(observed).toContain("liquidation"));
    expect(screen.getByTestId("location").textContent).toBe("/news/market?kind=liquidation");
    await waitFor(() => expect(document.querySelectorAll(".news-market-row")).toHaveLength(1));
    expect(screen.getByRole("button", { name: "清算" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "OI" })).toHaveAttribute("aria-pressed", "false");
  });

  it("summarizes each source over the whole window, raw beside parsed rather than under it", async () => {
    renderMarket();
    const sources = await screen.findByLabelText("来源汇总");

    // `raw` is the shape of a source with no parser, not a failure count, so it is its own figure.
    const unknown = within(sources).getByTitle(/未识别来源/);
    expect(unknown).toHaveTextContent("仅原文");
    expect(unknown).toHaveTextContent("2");
    expect(within(sources).getByTitle(/持仓异动/)).toHaveTextContent("61");
    // Every kind the endpoint serves keeps a tile, whether or not it sent anything: "this source was
    // quiet for 72 hours" is the answer a reader came for, and a hidden tile cannot give it.
    expect(sources.querySelectorAll(".news-market-source")).toHaveLength(4);
  });

  it("expands a group to its stored payload and its retained timeline", async () => {
    renderMarket();
    const rows = await screen.findAllByRole("button", { expanded: false });

    fireEvent.click(rows[0]);

    expect(await screen.findByText("供应商原文")).toBeInTheDocument();
    expect(screen.getByText("已入库字段")).toBeInTheDocument();
    expect(screen.getByText("PROVIDER_PARAMS")).toBeInTheDocument();
    expect(screen.getByText(/本组时间线/)).toBeInTheDocument();
    // The trace is stored columns read back, never a number derived in the browser.
    expect(screen.getByText("oi_change_bps")).toBeInTheDocument();
    expect(screen.getByText("671")).toBeInTheDocument();
  });

  it("says so plainly when the window holds no matching observation", async () => {
    server.use(
      http.get(/.*\/api\/news\/market$/, () =>
        HttpResponse.json({ ok: true, data: newsMarketFixture({ groups: [] }) }),
      ),
    );

    renderMarket();
    expect(await screen.findByText("这个窗口里没有符合当前筛选的市场观测。")).toBeInTheDocument();
    // The filter is still reachable: the window was empty, not the page.
    expect(screen.getByRole("button", { name: "OI" })).toBeInTheDocument();
  });

  it("offers a retry when the market read itself fails, and shows nothing it did not read", async () => {
    server.use(
      http.get(/.*\/api\/news\/market$/, () =>
        HttpResponse.json({ ok: false, error: "market unavailable" }, { status: 503 }),
      ),
    );

    renderMarket();
    const alert = await screen.findByRole("alert");

    expect(alert).toHaveTextContent("请求失败");
    expect(within(alert).getByRole("button", { name: "重试" })).toBeInTheDocument();
    expect(document.querySelectorAll(".news-market-row")).toHaveLength(0);
  });
});

function marketWithEveryKind(overrides: Partial<ReturnType<typeof newsMarketFixture>> = {}) {
  return newsMarketFixture({
    groups: [
      newsMarketGroupFixture(),
      newsMarketGroupFixture({
        first_event_at_ms: NEWS_NOW_MS - 260_000,
        latest: newsMarketObservationFixture({
          event_at_ms: NEWS_NOW_MS - 240_000,
          group_key: "liquidation:DOGE",
          item_id: "mkt-liq-doge-1",
          liquidated_position_side: "long",
          market_kind: "liquidation",
          // Still inside its 60 s follow-up window: sent and merging are both current states of the
          // same page, and the row is the only place either is visible.
          notification_reason: "liquidation_followup_window_open",
          notification_status: "merging",
          notional_usd: "412530.00",
          oi_change_bps: null,
          oi_value_usd: null,
          symbol: "DOGE",
          title: "DOGE Long Liquidation 412.53K at 0.2181",
          whale_long_profit_bps: null,
          whale_oi_ratio_bps: null,
        }),
        observation_count: 1,
      }),
      newsMarketGroupFixture({
        first_event_at_ms: NEWS_NOW_MS - 1_820_000,
        latest: newsMarketObservationFixture({
          event_at_ms: NEWS_NOW_MS - 1_800_000,
          group_key: "unknown_market:2026",
          item_id: "mkt-unknown-1",
          market_kind: "unknown_market",
          oi_change_bps: null,
          oi_value_usd: null,
          parse_error: "no_parser_for_source",
          parse_status: "raw",
          symbol: null,
          title: "PENGU OI Rise 3.4%, OI Value --, Whale Long Profit 55.10%",
          whale_long_profit_bps: null,
          whale_oi_ratio_bps: null,
        }),
        observation_count: 1,
      }),
    ],
    ...overrides,
  });
}

function renderMarket(path = "/news/market") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <div className="center-column">
          <NewsPage token="test-token" view="market" />
          <LocationProbe />
        </div>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function LocationProbe() {
  const location = useLocation();
  return <span data-testid="location">{`${location.pathname}${location.search}`}</span>;
}
