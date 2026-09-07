import { NewsPage } from "@features/news";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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

/**
 * 链上钱包 (#572 PR-3).
 *
 * The page exists because the market list answers "what observations arrived" and cannot answer "what is
 * the tape doing": a roster, an ingest position and a per-card price receipt have no counterpart on any
 * other market kind. What is asserted here is the page not inventing anything the two reads did not say,
 * and the two reads not being able to take each other down.
 */
describe("NewsWalletsPage", () => {
  beforeEach(() => {
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
    );
  });

  afterEach(cleanup);

  it("reads exactly its own two endpoints and no market or trading read", async () => {
    const paths: string[] = [];
    server.use(
      http.get(/.*\/api\/.*/, ({ request }) => {
        paths.push(new URL(request.url).pathname);
        return undefined;
      }),
    );

    renderWallets();
    await screen.findByRole("heading", { level: 1, name: "链上钱包" });

    await waitFor(() => expect(paths).toContain("/api/news/wallets"));
    await waitFor(() => expect(paths).toContain("/api/news/wallets/cards"));
    expect(paths.filter((path) => path.startsWith("/api/trading"))).toEqual([]);
    expect(paths.filter((path) => path === "/api/news/market")).toEqual([]);
  });

  it("defaults to buy and keeps unsent candidates, actual price references and unknown initial history", async () => {
    const requested: URL[] = [];
    server.use(
      http.get(/.*\/api\/news\/wallets\/cards$/, ({ request }) => {
        const url = new URL(request.url);
        requested.push(url);
        return HttpResponse.json({ ok: true, data: newsWalletCardsForParams(url.searchParams) });
      }),
    );
    renderWallets();
    const cards = await screen.findByRole("region", { name: "钱包卡片" });
    expect(await within(cards).findByText("观察期首次买入")).toBeInTheDocument();
    expect(requested[0].searchParams.get("kind")).toBe("buy");
    expect(within(cards).getByText("未发送")).toBeInTheDocument();
    expect(within(cards).getByText("记录原因：buy_below_min_usd")).toBeInTheDocument();
    expect(within(cards).getByText("价格基准：observed")).toBeInTheDocument();
    expect(within(cards).getByText("-20.00%")).toBeInTheDocument();
    expect(within(cards).getByRole("columnheader", { name: "已计价均价" })).toBeInTheDocument();
    expect(within(cards).getByRole("columnheader", { name: "观察价" })).toBeInTheDocument();
    expect(within(cards).queryByText("已核实新仓")).not.toBeInTheDocument();
    expect(within(cards).queryByRole("link", { name: "减仓" })).not.toBeInTheDocument();
  });

  it("shows buy candidates even when the independent roster read fails", async () => {
    server.use(
      http.get(/.*\/api\/news\/wallets$/, () =>
        HttpResponse.json({ ok: false, error: "roster unavailable" }, { status: 503 }),
      ),
    );
    renderWallets();
    expect(await screen.findByText("观察期首次买入")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "重试" })).toBeInTheDocument();
  });

  it("preserves exact wallet/token filters across window changes and requests a complete scoped timeline", async () => {
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
    fireEvent.click(await screen.findByRole("link", { name: "同钱包与代币的后续变化" }));
    await waitFor(() =>
      expect(requests.at(-1)?.searchParams.get("wallet_address")).toBe(buy.wallet),
    );
    expect(requests.at(-1)?.searchParams.get("token_address")).toBe(buy.token);
    expect(requests.at(-1)?.searchParams.has("kind")).toBe(false);
    expect(requests.at(-1)?.searchParams.has("token")).toBe(false);
    expect(screen.getByRole("button", { name: "全部" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByRole("button", { name: "7d" }));
    await waitFor(() => expect(requests.at(-1)?.searchParams.get("window")).toBe("7d"));
    expect(requests.at(-1)?.searchParams.get("wallet_address")).toBe(buy.wallet);
    expect(requests.at(-1)?.searchParams.get("token_address")).toBe(buy.token);
    expect(screen.getByTestId("location")).toHaveTextContent(
      `window=7d&kind=all&wallet_address=${buy.wallet}&token_address=${buy.token}`,
    );
  });

  it("submits exact address filters and clears them without losing the chosen kind", async () => {
    renderWallets("/news/wallets?kind=crowding&window=72h");
    const buy = newsWalletBuyFixture();
    fireEvent.change(screen.getByRole("textbox", { name: "钱包地址" }), {
      target: { value: buy.wallet },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "代币合约" }), {
      target: { value: buy.token },
    });
    fireEvent.click(screen.getByRole("button", { name: "筛选" }));
    await waitFor(() =>
      expect(screen.getByTestId("location")).toHaveTextContent(
        `wallet_address=${buy.wallet}&token_address=${buy.token}`,
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: "清除地址" }));
    expect(screen.getByTestId("location")).toHaveTextContent(
      "/news/wallets?window=72h&kind=crowding",
    );
  });

  it("keeps raw small sells and transfers visible under the buy filter with exact quantities", async () => {
    const buy = newsWalletBuyFixture();
    renderWallets(`/news/wallets?wallet_address=${buy.wallet}&token_address=${buy.token}`);
    const fills = await screen.findByRole("region", { name: "交易流水" });
    expect(await within(fills).findByText("卖出")).toBeInTheDocument();
    expect(within(fills).getByText("转出")).toBeInTheDocument();
    expect(within(fills).getAllByText("1.234567890123456789")).toHaveLength(2);
    expect(within(fills).getByText("7 raw（精度未知）")).toBeInTheDocument();
    expect(within(fills).getByText("未知")).toBeInTheDocument();
    expect(within(fills).getByRole("link", { name: "0x22222222…" })).toHaveAttribute(
      "href",
      `https://robinhoodchain.blockscout.com/tx/0x${"2".repeat(64)}`,
    );
    expect(screen.getByRole("button", { name: "买入" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByRole("link", { name: "减仓" })).not.toBeInTheDocument();
  });

  it("states the day in four tiles from the header read alone", async () => {
    renderWallets();

    // 62 + 44 + 9 fills, and the caption splits them by kind exactly as the server counted them.
    expect(await screen.findByText("115")).toBeInTheDocument();
    expect(screen.getByText("买入 62 · 卖出 44 · 转出 9")).toBeInTheDocument();
    // 4 + 1 + 6 cards, ten of which were sent.
    expect(screen.getByText("11")).toBeInTheDocument();
    expect(screen.getByText("减仓 4 · 拥挤 1 · 摘要 6")).toBeInTheDocument();
    expect(screen.getByText("已送达 10")).toBeInTheDocument();
    // 3 unpriced of the 106 fills a cash leg could have priced; transfers are not trades.
    expect(screen.getByText("2.8%")).toBeInTheDocument();
    expect(screen.getByText("3 / 106 笔")).toBeInTheDocument();
    expect(screen.getByText("success")).toBeInTheDocument();
  });

  it("shows both roster ranks separately, and a wallet that holds only one of them", async () => {
    renderWallets();

    const roster = await screen.findByRole("region", { name: "跟踪名单" });
    const rows = within(within(roster).getAllByRole("rowgroup")[1]).getAllByRole("row");
    expect(within(rows[0]).getByRole("rowheader").textContent).toBe("0xVantaa");
    // 质量榜 1, 大户榜 none: the two lists are two answers and a wallet can be on one.
    expect(within(rows[0]).getAllByRole("cell")[1].textContent).toBe("1");
    expect(within(rows[0]).getAllByRole("cell")[2].textContent).toBe("—");
    expect(within(rows[1]).getByRole("rowheader").textContent).toBe("smol_intern");
    expect(within(rows[1]).getAllByRole("cell")[1].textContent).toBe("—");
    expect(within(rows[1]).getAllByRole("cell")[2].textContent).toBe("2");
  });

  it("publishes every card with its receipts, and says which is missing rather than showing zero", async () => {
    renderWallets("/news/wallets?kind=all");

    const cards = await screen.findByRole("region", { name: "钱包卡片" });
    // Three cards, the header, and the digest's own line row beneath it.
    await waitFor(() => expect(within(cards).getAllByRole("row")).toHaveLength(9));
    // The exit's +1h came back at -5.12%; its +4h horizon has not arrived, which is the absence of a row.
    expect(await screen.findByText("-5.12%")).toBeInTheDocument();
    expect(screen.getByText("+17.30%")).toBeInTheDocument();
    // A horizon nothing could price says so; it is a different fact from "not due yet".
    expect(screen.getByText("无价")).toBeInTheDocument();
    // The digest carries its own answer to "who wrote this", which the card itself cannot say.
    expect(screen.getByText("模型选材")).toBeInTheDocument();
    // And its sentences, which are the whole content of that card and no column can hold.
    expect(
      screen.getByText("合计买入 62 笔 $394,120.55，卖出 44 笔 $309,002.10"),
    ).toBeInTheDocument();
    expect(screen.getByText("窗口内退出卡 4 张")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "摘要" })).toHaveAttribute(
      "href",
      `/news/market/${"c".repeat(64)}`,
    );
    // The exit's basis is printed in the four characters the card uses; a crowding card has none.
    expect(screen.getByText("链上余额")).toBeInTheDocument();
  });

  it("writes the window into the URL and re-requests, because the window is a real request", async () => {
    const windows: string[] = [];
    server.use(
      http.get(/.*\/api\/news\/wallets\/cards$/, ({ request }) => {
        const window = new URL(request.url).searchParams.get("window") ?? "24h";
        windows.push(window);
        return HttpResponse.json({ ok: true, data: newsWalletCardsFixture({ window }) });
      }),
    );
    renderWallets();
    await screen.findByRole("button", { name: "7d" });

    fireEvent.click(screen.getByRole("button", { name: "7d" }));

    await waitFor(() => expect(windows).toContain("7d"));
    expect(screen.getByTestId("location").textContent).toBe("/news/wallets?window=7d");
  });

  it("keeps a failing card read out of the roster it has nothing to do with", async () => {
    server.use(
      http.get(/.*\/api\/news\/wallets\/cards$/, () =>
        HttpResponse.json({ ok: false, error: "boom" }, { status: 503 }),
      ),
    );

    renderWallets();

    // The roster and the tiles are the other read's answer and are untouched.
    expect(await screen.findByRole("region", { name: "跟踪名单" })).toHaveTextContent("0xVantaa");
    expect(screen.getByText("success")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "重试" })).toBeInTheDocument();
  });

  it("says the tape has no roster yet rather than rendering an empty table", async () => {
    server.use(
      http.get(/.*\/api\/news\/wallets$/, () =>
        HttpResponse.json({
          ok: true,
          data: newsWalletsFixture({
            cards: [],
            fills: [],
            roster: { members: [], provider: null, roster_version: 0, taken_at_ms: null },
            tape: null,
          }),
        }),
      ),
      http.get(/.*\/api\/news\/wallets\/cards$/, () =>
        HttpResponse.json({ ok: true, data: newsWalletCardsFixture({ cards: [] }) }),
      ),
    );

    renderWallets();

    expect(
      await screen.findByText("还没有名单版本：链上钱包任务未开启或第一次刷新尚未完成。"),
    ).toBeInTheDocument();
    expect(screen.getByText("当前窗口与筛选下没有观察记录。")).toBeInTheDocument();
  });
});

function renderWallets(path = "/news/wallets") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
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
  return <span data-testid="location">{`${location.pathname}${location.search}`}</span>;
}
