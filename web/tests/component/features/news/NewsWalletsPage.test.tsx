import { NewsPage } from "@features/news";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import {
  newsWalletBuyFixture,
  newsWalletCardsFixture,
  newsWalletCardsForParams,
  newsWalletsFixture,
} from "@tests/fixtures/newsFixture";
import { server } from "@tests/msw/server";
import { HttpResponse, http } from "msw";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

describe("wallet research", () => {
  beforeEach(() =>
    server.use(
      http.get(/.*\/api\/news\/wallets\/cards$/, ({ request }) =>
        HttpResponse.json({
          ok: true,
          data: newsWalletCardsForParams(new URL(request.url).searchParams),
        }),
      ),
      http.get(/.*\/api\/news\/wallets$/, () =>
        HttpResponse.json({ ok: true, data: newsWalletsFixture() }),
      ),
    ),
  );
  afterEach(cleanup);

  it("defaults to buys and opens evidence without requiring a notification", async () => {
    const requests: URL[] = [];
    server.use(
      http.get(/.*\/api\/news\/wallets\/cards$/, ({ request }) => {
        const url = new URL(request.url);
        requests.push(url);
        return HttpResponse.json({ ok: true, data: newsWalletCardsForParams(url.searchParams) });
      }),
    );
    renderWallets();
    expect(await screen.findByRole("heading", { name: "钱包研究" })).toBeVisible();
    const row = await screen.findByRole("button", { name: /观察期首次买入/ });
    expect(requests[0].searchParams.get("kind")).toBe("buy");
    expect(row).toHaveTextContent("500");
    expect(row).toHaveTextContent("未到观察时点");
    expect(row).not.toHaveTextContent("未发送");
    fireEvent.click(row);
    expect(await screen.findByText("未发送")).toBeVisible();
    expect(screen.getByText("-20.00%")).toBeVisible();
    expect(screen.getByText("已计价成交均价")).toBeVisible();
    expect(screen.getByText("观察价 · USD / token")).toBeVisible();
    expect(screen.getByTestId("location")).toHaveTextContent("item=");
    expect(screen.queryByText("已核实新仓")).toBeNull();
  });

  it("keeps the selected observation readable when newer segment facts arrive, until returning to latest", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    let newerArrived = false;
    let requestCount = 0;
    server.use(
      http.get(/.*\/api\/news\/wallets\/cards$/, ({ request }) => {
        requestCount += 1;
        const anchored = new URL(request.url).searchParams.has("to_ms");
        return HttpResponse.json({
          ok: true,
          data: newsWalletCardsFixture({
            cards: [
              newsWalletBuyFixture(
                newerArrived && !anchored
                  ? { item_id: "e".repeat(64), handle: "newer-observation" }
                  : { handle: "selected-observation" },
              ),
            ],
          }),
        });
      }),
    );
    renderWallets("/news/wallets", client);
    fireEvent.click(await screen.findByRole("button", { name: /selected-observation/ }));
    expect(await screen.findByText("已计价成交均价")).toBeVisible();
    newerArrived = true;
    const beforeRefresh = requestCount;
    await act(async () => {
      await client.invalidateQueries();
    });
    await waitFor(() => expect(requestCount).toBeGreaterThan(beforeRefresh));
    await waitFor(() => expect(client.isFetching()).toBe(0));
    expect(screen.getByRole("button", { name: /selected-observation/ })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    expect(screen.getByText("已计价成交均价")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "回到最新" }));
    expect(await screen.findByRole("button", { name: /newer-observation/ })).toBeVisible();
    expect(screen.getByTestId("location")).not.toHaveTextContent("to_ms=");
  });

  it("keeps unverified prices out of the comparable return while exposing their raw evidence", async () => {
    const buy = newsWalletBuyFixture();
    server.use(
      http.get(/.*\/api\/news\/wallets\/cards$/, () =>
        HttpResponse.json({
          ok: true,
          data: newsWalletCardsFixture({
            cards: [
              newsWalletBuyFixture({
                price_status: "identity_unverified",
                mark_source: "robinhoodtrenches_mark",
                mark_price: "181.77",
                outcomes: buy.outcomes.map((o) => ({
                  ...o,
                  status: "identity_unverified",
                  return_bps: null,
                })),
              }),
            ],
          }),
        }),
      ),
    );
    renderWallets();
    const row = await screen.findByRole("button", { name: /观察期首次买入/ });
    expect(row).toHaveTextContent("价格待核实");
    expect(row).not.toHaveTextContent("0.00%");
    fireEvent.click(row);
    expect(await screen.findByText("181.77")).toBeVisible();
    expect(screen.getByText("robinhoodtrenches_mark")).toBeVisible();
  });

  it("preserves address and chain identity when opening a segment and changing the window", async () => {
    const buy = newsWalletBuyFixture();
    const requests: URL[] = [];
    server.use(
      http.get(/.*\/api\/news\/wallets\/cards$/, ({ request }) => {
        const url = new URL(request.url);
        requests.push(url);
        return HttpResponse.json({ ok: true, data: newsWalletCardsForParams(url.searchParams) });
      }),
    );
    renderWallets();
    fireEvent.click(await screen.findByRole("button", { name: /观察期首次买入/ }));
    fireEvent.click(screen.getByRole("link", { name: /展开本段/ }));
    await waitFor(() =>
      expect(requests.at(-1)?.searchParams.get("segment_key")).toBe(buy.segment_key),
    );
    expect(requests.at(-1)?.searchParams.get("wallet_address")).toBe(buy.wallet);
    expect(requests.at(-1)?.searchParams.get("token_address")).toBe(buy.token);
    expect(requests.at(-1)?.searchParams.get("chain_id")).toBe("4663");
    expect(requests.at(-1)?.searchParams.get("view")).toBe("observations");
    fireEvent.click(screen.getByRole("button", { name: "7d" }));
    await waitFor(() => expect(requests.at(-1)?.searchParams.get("window")).toBe("7d"));
    expect(requests.at(-1)?.searchParams.get("token_address")).toBe(buy.token);
  });

  it("uses full-scope totals while paging a single segment and preserves the snapshot", async () => {
    const requests: URL[] = [];
    server.use(
      http.get(/.*\/api\/news\/wallets\/cards$/, ({ request }) => {
        const url = new URL(request.url);
        requests.push(url);
        return HttpResponse.json({
          ok: true,
          data: newsWalletCardsFixture({
            cards: [newsWalletBuyFixture({ usd: "3000", buy_count: 3 })],
            totals: {
              segments: 250,
              observations: 600,
              wallets: 12,
              tokens: 18,
              priced_buy_usd: "88000",
            },
            next_cursor: url.searchParams.has("cursor") ? null : "stable-scope-position",
          }),
        });
      }),
    );
    renderWallets();
    const row = await screen.findByRole("button", { name: /观察期首次买入/ });
    expect(row).toHaveTextContent("3,000");
    expect(screen.getByText("600 次观察 · 分页前统计")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() =>
      expect(requests.at(-1)?.searchParams.get("cursor")).toBe("stable-scope-position"),
    );
    expect(requests.at(-1)?.searchParams.has("to_ms")).toBe(true);
    expect(screen.getByTestId("location")).toHaveTextContent("cursor=stable-scope-position");
    expect(screen.getByText("600 次观察 · 分页前统计")).toBeVisible();
  });

  it("keeps real buys, small sells and transfers distinct under the buy filter", async () => {
    const buy = newsWalletBuyFixture();
    renderWallets(`/news/wallets?wallet_address=${buy.wallet}&token_address=${buy.token}`);
    const fills = await screen.findByRole("region", { name: "交易流水" });
    expect(await within(fills).findByText("卖出")).toBeVisible();
    expect(within(fills).getByText("转出")).toBeVisible();
    expect(within(fills).getAllByText("1.234567890123456789")).toHaveLength(2);
    expect(within(fills).getByText("7 raw（精度未知）")).toBeVisible();
    expect(within(fills).getByRole("link", { name: "0x22222222…" })).toHaveAttribute(
      "href",
      `https://robinhoodchain.blockscout.com/tx/0x${"2".repeat(64)}`,
    );
  });

  it("keeps the research readable when the independent roster read fails", async () => {
    server.use(
      http.get(/.*\/api\/news\/wallets$/, () =>
        HttpResponse.json({ ok: false, error: "roster unavailable" }, { status: 503 }),
      ),
    );
    renderWallets();
    expect(await screen.findByRole("button", { name: /观察期首次买入/ })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: /跟踪钱包/ }));
    expect(await screen.findByRole("button", { name: "重试" })).toBeVisible();
  });

  it("keeps both roster selection ranks available when the research endpoint fails", async () => {
    server.use(
      http.get(/.*\/api\/news\/wallets\/cards$/, () =>
        HttpResponse.json({ ok: false, error: "cards unavailable" }, { status: 503 }),
      ),
    );
    renderWallets("/news/wallets?tab=roster");
    expect(await screen.findByRole("columnheader", { name: "来源表现榜" })).toBeVisible();
    expect(screen.getByRole("columnheader", { name: "大户榜" })).toBeVisible();
    expect(screen.getByRole("link", { name: "0xVantaa" })).toBeVisible();
  });

  it("distinguishes an empty scope from a failed read and leaves filters usable", async () => {
    server.use(
      http.get(/.*\/api\/news\/wallets\/cards$/, () =>
        HttpResponse.json({ ok: true, data: newsWalletCardsFixture({ cards: [] }) }),
      ),
    );
    renderWallets();
    expect(await screen.findByText("当前窗口与筛选下没有观察记录。")).toBeVisible();
    expect(screen.getByRole("button", { name: "筛选" })).toBeEnabled();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

function renderWallets(
  path = "/news/wallets",
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } }),
) {
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <div className="center-column">
          <NewsPage token="test-token" view="wallets" />
          <LocationProbe />
        </div>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}
function LocationProbe() {
  const location = useLocation();
  return <span data-testid="location">{location.pathname + location.search}</span>;
}
