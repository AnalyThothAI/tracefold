import { NewsPage } from "@features/news";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import {
  newsFeedEventFixture,
  newsFeedFixture,
  newsOiFrameFixture,
  newsReactionFixture,
  newsStatusFixture,
  newsSymbolFixture,
} from "@tests/fixtures/newsFixture";
import {
  tradingCaseFixture,
  tradingCasesFixture,
  tradingCasesForUnderlying,
  tradingSignalsForMarket,
} from "@tests/fixtures/tradingFixture";
import { server } from "@tests/msw/server";
import { HttpResponse, http } from "msw";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

/**
 * 代币页 (#207 PR-W1) — what a name is, and everything that happened to it.
 *
 * The page composes four endpoints it does not own, so most of what is worth testing is that it keeps their
 * answers separate: identity is not a market claim, a quote is not a return, and a name nothing lists is an
 * answer rather than an error.
 */
describe("NewsSymbolPage", () => {
  beforeEach(() => {
    server.use(
      http.get(/.*\/api\/news\/status$/, () =>
        HttpResponse.json({ ok: true, data: newsStatusFixture() }),
      ),
      http.get(/.*\/api\/news\/quotes$/, () =>
        HttpResponse.json({ ok: true, data: { measured_at_ms: 0, quotes: [] } }),
      ),
      http.get(/.*\/api\/news\/symbols\/.*/, () =>
        HttpResponse.json({ ok: true, data: newsSymbolFixture() }),
      ),
      http.get(/.*\/api\/news\/feed$/, () =>
        HttpResponse.json({
          ok: true,
          data: newsFeedFixture({ events: [newsOiFrameFixture(), newsFeedEventFixture()] }),
        }),
      ),
      /*
       * The page reads this on every render and `tests/setup.ts` runs MSW with
       * `onUnhandledRequest: "error"`, so without it every test here asserted against a page whose capital
       * read had failed — silently, because the failure surfaced only as `data === undefined` and both
       * sections rendered their empty copy. A test that means to see that state now says so.
       */
      http.get(/.*\/api\/trading\/signals.*/, ({ request }) =>
        HttpResponse.json({
          ok: true,
          data: tradingSignalsForMarket(new URL(request.url).searchParams.get("market")),
        }),
      ),
      http.get(/.*\/api\/trading\/cases.*/, ({ request }) =>
        HttpResponse.json({
          ok: true,
          data: tradingCasesForUnderlying(new URL(request.url).searchParams.get("underlying")),
        }),
      ),
    );
  });

  afterEach(cleanup);

  it("asks the feed for this symbol alone, and asks identity by the collapsed base", async () => {
    const feedParams: Record<string, string | null> = {};
    let identityPath = "";
    server.use(
      http.get(/.*\/api\/news\/feed$/, ({ request }) => {
        const params = new URL(request.url).searchParams;
        for (const name of ["symbol", "hours"]) feedParams[name] = params.get(name);
        return HttpResponse.json({ ok: true, data: newsFeedFixture({ events: [] }) });
      }),
      http.get(/.*\/api\/news\/symbols\/.*/, ({ request }) => {
        identityPath = new URL(request.url).pathname;
        return HttpResponse.json({ ok: true, data: newsSymbolFixture() });
      }),
    );

    // The provider's prefixed spelling in the URL must resolve to the one instrument, not a second page.
    renderSymbol("/news/symbols/xyz-wif", "xyz-wif");

    await waitFor(() => expect(feedParams.symbol).toBe("WIF"));
    expect(feedParams.hours).toBe("24");
    expect(identityPath).toBe("/api/news/symbols/WIF");
  });

  it("names the contracts and says whether the lane can act on the symbol", async () => {
    renderSymbol();

    await screen.findByRole("heading", { name: "WIF" });
    expect(await screen.findByText("binance.perp:WIFUSDT")).toBeInTheDocument();
    expect(screen.getByText("已落标的表")).toBeInTheDocument();
    // #87: the alias collapse is why several contracts share one storyline, so it is on the card.
    expect(screen.getByText("XYZ-WIF")).toBeInTheDocument();
  });

  it("says a name no venue lists is unlisted, and does not call it an error", async () => {
    server.use(
      http.get(/.*\/api\/news\/symbols\/.*/, () =>
        HttpResponse.json({
          ok: true,
          data: newsSymbolFixture({
            base_symbol: "SPOT",
            contracts: [],
            known: false,
            normalization: null,
            tradeable: false,
            venues: [],
          }),
        }),
      ),
    );

    renderSymbol("/news/symbols/SPOT", "SPOT");

    expect(await screen.findByText(/标的表里查不到/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重试" })).not.toBeInTheDocument();
  });

  it("keeps a reference-tier listing visible instead of calling the symbol tradeable", async () => {
    /*
     * #91. `us.listed` proves the ticker exists; it does not prove anyone can trade it. Filtering the row
     * out would lose the answer to "what is this", and counting it as tradeable would claim an instrument
     * the lane cannot reach.
     */
    server.use(
      http.get(/.*\/api\/news\/symbols\/.*/, () =>
        HttpResponse.json({
          ok: true,
          data: newsSymbolFixture({
            base_symbol: "COPPER",
            contracts: [
              {
                instrument_class: "commodity",
                quote_asset: null,
                reference_only: true,
                venue: "us.listed",
                venue_symbol: "HG",
              },
            ],
            known: true,
            normalization: null,
            tradeable: false,
            venues: ["us.listed"],
          }),
        }),
      ),
    );

    renderSymbol("/news/symbols/COPPER", "COPPER");

    expect(await screen.findByText("仅参考行情，无可交易合约")).toBeInTheDocument();
    // And the contract that established the identity is on screen: saying "no tradeable contract" while
    // naming none of them answers half the question the reader came with.
    expect(screen.getByText("us.listed:HG")).toBeInTheDocument();
    expect(screen.getByText("仅参考")).toBeInTheDocument();
  });

  it("filters every persisted Event kind on one clock", async () => {
    server.use(
      http.get(/.*\/api\/news\/feed$/, () =>
        HttpResponse.json({
          ok: true,
          data: newsFeedFixture({
            events: [
              newsFeedEventFixture({ event_id: "evt-news", event_kind: "news" }),
              newsFeedEventFixture({ event_id: "evt-listing", event_kind: "listing" }),
              newsFeedEventFixture({
                admission: "candidate",
                event_id: "evt-oi",
                event_kind: "oi",
              }),
              newsFeedEventFixture({
                admission: "telemetry_deterministic",
                event_id: "evt-liquidation",
                event_kind: "liquidation",
              }),
              newsFeedEventFixture({
                event_id: "evt-unsupported",
                event_kind: "unsupported_market",
              }),
            ],
          }),
        }),
      ),
    );
    renderSymbol();

    const table = await screen.findByLabelText("按事件类型筛选");
    expect(within(table).getByRole("tab", { name: /全部/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await waitFor(() => expect(document.querySelectorAll(".news-symbol-row")).toHaveLength(5));
    for (const label of ["新闻", "上币/下币", "OI 帧", "强平", "未支持市场"]) {
      expect(within(table).getByRole("tab", { name: label })).toBeInTheDocument();
    }

    fireEvent.click(within(table).getByRole("tab", { name: "强平" }));

    await waitFor(() => expect(document.querySelectorAll(".news-symbol-row")).toHaveLength(1));
    expect(screen.getByTestId("location")).toHaveTextContent("lane=liquidation");
    const kind = document.querySelector(".news-symbol-row .news-kind");
    expect(kind).toHaveAttribute("data-kind", "liquidation");
    expect(kind).toHaveTextContent("强平");
  });

  it("shows a pending horizon as 未到期 rather than a zero return", async () => {
    server.use(
      http.get(/.*\/api\/news\/feed$/, () =>
        HttpResponse.json({
          ok: true,
          data: newsFeedFixture({
            events: [
              newsFeedEventFixture({
                reaction: newsReactionFixture({
                  return_1h_bps: null,
                  return_4h_bps: null,
                  state: "pending",
                  state_zh: "未到期",
                }),
              }),
            ],
          }),
        }),
      ),
    );

    renderSymbol();

    const row = await screen.findByText("央行政策转向，风险资产承压");
    const cell = row.closest(".news-symbol-row")?.querySelector(".news-symbol-reaction");
    expect(cell).toHaveTextContent("未到期");
    expect(cell).not.toHaveTextContent("0.00%");
  });

  /*
   * #282's acceptance asks for three tokens with three different right answers, and the failure mode it is
   * guarding against is a panel filling a gap in with something plausible. #331 moved the answer to the
   * aggregate that owns it: the 交易视角 quadrant panel is gone with the quadrant itself, and 资本复盘
   * renders the Case rows the server decided rather than re-deriving a verdict in the browser.
   */
  it("renders the Cases the server decided for this token, and never invents one", async () => {
    server.use(
      http.get(/.*\/api\/trading\/cases.*/, ({ request }) =>
        HttpResponse.json({
          ok: true,
          data: tradingCasesForUnderlying(new URL(request.url).searchParams.get("underlying")),
        }),
      ),
    );
    renderSymbol("/news/symbols/HYPE", "HYPE");

    expect(await screen.findByText("Alpha 复盘 · Case → Signal")).toBeInTheDocument();
    expect(await screen.findByText("不交易")).toBeInTheDocument();
    expect(screen.getByText("鲸鱼占比未超过地板")).toBeInTheDocument();
    expect(screen.getByText("未发出")).toBeInTheDocument();
    // No quadrant, and no threshold comparison recomputed here: the frozen checks live with the Case.
    expect(screen.queryByText("buildup_up")).not.toBeInTheDocument();
    expect(screen.queryByText("过地板")).not.toBeInTheDocument();
  });

  it("says a token the lane never opened a case for has no Case, not a refusal", async () => {
    server.use(
      http.get(/.*\/api\/trading\/cases.*/, () =>
        HttpResponse.json({ ok: true, data: tradingCasesFixture({ cases: [] }) }),
      ),
    );
    renderSymbol();

    expect(await screen.findByText("当前窗口没有这个代币的 Case。")).toBeInTheDocument();
    expect(screen.queryByText("过地板")).not.toBeInTheDocument();
  });

  it("distinguishes an unreadable capital ledger from an empty one", async () => {
    server.use(
      http.get(/.*\/api\/trading\/cases.*/, () =>
        HttpResponse.json({ ok: false }, { status: 503 }),
      ),
    );
    renderSymbol();

    expect(
      await screen.findByText("Alpha Case 账本本轮不可用；不能据此断言没有案例。"),
    ).toBeInTheDocument();
  });

  it("keeps an unreadable Signal ledger distinct from a Case with no Signal", async () => {
    let signalReads = 0;
    server.use(
      http.get(/.*\/api\/trading\/cases.*/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingCasesFixture({
            cases: [
              tradingCaseFixture({
                policy_decision: "long",
                policy_reason: "signal_emitted",
                state: "SIGNAL_EMITTED",
              }),
            ],
          }),
        }),
      ),
      http.get(/.*\/api\/trading\/signals.*/, () => {
        signalReads += 1;
        return signalReads === 1
          ? HttpResponse.json({ ok: false }, { status: 503 })
          : HttpResponse.json({ ok: true, data: tradingSignalsForMarket("crypto:perp:HYPE:USDT") });
      }),
    );
    renderSymbol("/news/symbols/HYPE", "HYPE");

    expect(
      await screen.findByText("Signal 账本本轮不可用；不能据此断言未发出 Signal。"),
    ).toBeInTheDocument();
    expect(screen.queryByText("未发出")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "重试" }));

    expect(await screen.findByText("未发出")).toBeInTheDocument();
    expect(signalReads).toBe(2);
  });

  it("prints this window's counts once, in the identity band the artifact puts them in", async () => {
    renderSymbol();

    expect(await screen.findByText("24H 事件")).toBeInTheDocument();
    // The header carried the same two numbers as a stamp, so a reader saw them twice on one screen.
    expect(screen.queryByText(/条 · 已推送/)).not.toBeInTheDocument();
  });

  it("no longer carries a rank window, which was the page's one forward-looking claim", async () => {
    /*
     * #458 removed the News push rule and the four-hour rank window it spent. This page's occupancy card
     * was the only place that said what would happen to the *next* frame for this name, so it is gone
     * rather than reworded: nothing on the console can answer that question now, and a card that kept the
     * shape while the rule left would be the console asserting a gate that no code applies.
     */
    renderSymbol();
    await screen.findByRole("heading", { level: 1 });

    expect(screen.queryByText(/beyond_window_rank|还有名次|没有合格帧/)).toBeNull();
    expect(screen.queryByText("OI 窗口名次")).toBeNull();
    expect(screen.queryByText("OI 窗口")).toBeNull();
  });
});

function renderSymbol(path = "/news/symbols/WIF", base = "WIF") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <div className="center-column">
          <NewsPage base={base} token="test-token" view="symbol" />
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
