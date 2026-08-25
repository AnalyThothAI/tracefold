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
  });

  it("mixes news and OI frames on one clock and filters them by channel", async () => {
    renderSymbol();

    const table = await screen.findByLabelText("按通道筛选");
    expect(within(table).getByRole("tab", { name: /全部/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await waitFor(() => expect(document.querySelectorAll(".news-symbol-row")).toHaveLength(2));

    fireEvent.click(within(table).getByRole("tab", { name: /OI 帧/ }));

    await waitFor(() => expect(document.querySelectorAll(".news-symbol-row")).toHaveLength(1));
    expect(screen.getByTestId("location")).toHaveTextContent("lane=oi");
    // The lane is the server's admission, never a guess from the title.
    expect(document.querySelector(".news-symbol-lane")).toHaveTextContent("OI 帧");
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

  it("reports this symbol's rank occupancy from the server, not from the rows below", async () => {
    /*
     * The events table is a 24 h window and the rank window is four hours. Folding the occupancy out of the
     * loaded rows would report a fuller window than the judge sees, which is the one number on this page
     * that is about the *next* frame rather than the last ones.
     */
    renderSymbol();

    const window = await screen.findByText(/beyond_window_rank|还有名次|没有合格帧/);
    expect(window).toBeInTheDocument();
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
