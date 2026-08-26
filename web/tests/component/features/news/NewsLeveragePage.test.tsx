import { NewsPage } from "@features/news";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import {
  newsFeedFixture,
  newsOiFrameFixture,
  newsStatusFixture,
} from "@tests/fixtures/newsFixture";
import {
  tradingCaseFixture,
  tradingOrderFixture,
  tradingOrdersFixture,
} from "@tests/fixtures/tradingFixture";
import { server } from "@tests/msw/server";
import { HttpResponse, http } from "msw";
import type { ReactNode } from "react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

/**
 * 杠杆异动 (#256). The capital lane's reading of the deterministic OI frames.
 *
 * The tests here are mostly about what the page refuses to say: it lists no frame that authored no case, it
 * names the rule instead of paraphrasing a thesis nobody wrote, and it never mixes a frozen cutoff figure
 * with a live quote in one row.
 */
describe("NewsLeveragePage", () => {
  beforeEach(() => {
    server.use(
      http.get(/.*\/api\/news\/status$/, () =>
        HttpResponse.json({ ok: true, data: newsStatusFixture() }),
      ),
      http.get(/.*\/api\/news\/feed$/, () =>
        HttpResponse.json({
          ok: true,
          data: newsFeedFixture({
            events: [
              newsOiFrameFixture(),
              newsOiFrameFixture({ event_id: "evt-oi-hype", leader_title: "HYPE OI Rise 2.10%" }),
              newsOiFrameFixture({ event_id: "evt-never-judged" }),
            ],
          }),
        }),
      ),
      http.get(/.*\/api\/trading\/orders$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingOrdersFixture({ orders: [tradingOrderFixture()] }),
        }),
      ),
      http.get(/.*\/api\/news\/quotes$/, () =>
        HttpResponse.json({ ok: true, data: { measured_at_ms: 0, quotes: [] } }),
      ),
    );
  });

  afterEach(cleanup);

  it("lists only frames that reached the capital lane, and points at the audit for the rest", async () => {
    renderLeverage();
    await screen.findByRole("heading", { name: "市场杠杆结构" });

    const list = await screen.findByRole("region", { name: "案例列表" });
    const cards = within(list).getAllByRole("button");
    expect(cards).toHaveLength(1);
    expect(cards[0]).toHaveTextContent("WIF");
    // The third frame never authored a case and must not appear as one.
    expect(within(list).queryByText(/evt-never-judged/)).toBeNull();
    expect(screen.getByRole("link", { name: "OI 遥测审计" })).toHaveAttribute("href", "/news/oi");
  });

  it("names the rule and never invents a thesis for a pure-rule strategy", async () => {
    server.use(
      http.get(/.*\/api\/trading\/orders$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingOrdersFixture({
            cases_without_orders: [tradingCaseFixture({ event_id: "evt-oi-wif" })],
            orders: [],
          }),
        }),
      ),
    );
    renderLeverage("/news/leverage?lev=no_trade");
    await screen.findByRole("heading", { name: "市场杠杆结构" });

    const detail = await screen.findByRole("region", { name: /^案例 / });
    expect(detail).toHaveTextContent("为什么不交易 · NAMED RULE");
    expect(detail).toHaveTextContent("不交易：鲸盈利未达地板。");
    expect(detail).toHaveTextContent("whale_profit_below_floor");
    // No position, so nothing to invalidate — said outright rather than left blank.
    expect(detail).toHaveTextContent("—（no_trade 无持仓可失效）");
  });

  it("keeps the frozen plane and the live one on separate tabs", async () => {
    /*
     * The single most misleading thing this page could draw is a frozen cutoff figure beside a live quote in
     * adjacent cells: it invites subtracting one from the other and calling the difference a return.
     */
    renderLeverage();
    const detail = await screen.findByRole("region", { name: /^案例 / });

    expect(detail).toHaveTextContent("冻结 · cutoff 之前可见的事实");
    expect(detail).toHaveTextContent("冻结入场参考");
    expect(detail).not.toHaveTextContent("当前报价");

    fireEvent.click(within(detail).getByRole("tab", { name: "现在" }));
    expect(detail).toHaveTextContent("当前报价");
    expect(detail).toHaveTextContent("独立报价轮询，从不回写案例");
    expect(detail).not.toHaveTextContent("冻结入场参考");
  });

  it("carries the selected case and tab in the URL", async () => {
    renderLeverage();
    await screen.findByRole("region", { name: "案例列表" });

    fireEvent.click(screen.getByRole("tab", { name: /不交易/ }));
    expect(screen.getByTestId("location").textContent).toBe("/news/leverage?lev=no_trade");
  });

  it("shows the case a shared link names even when the tab would filter it out", async () => {
    // Substituting a different case silently is what makes a shared link point at the wrong money.
    server.use(
      http.get(/.*\/api\/trading\/orders$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingOrdersFixture({
            cases_without_orders: [tradingCaseFixture({ event_id: "evt-oi-hype" })],
            orders: [tradingOrderFixture()],
          }),
        }),
      ),
    );
    renderLeverage("/news/leverage?lev=live&case=evt-oi-hype");

    const detail = await screen.findByRole("region", { name: /^案例 / });
    expect(detail).toHaveTextContent("NO TRADE · 不交易");
  });

  it("never describes a stop the ledger has not proven as a working one", async () => {
    /*
     * `stopVerified` recognises exactly `OPEN` — a filled position with a read-back reduce-only stop
     * covering it. Telling an operator that crossing `stop_price` triggers a native stop on an
     * `ACKNOWLEDGED` order is false risk assurance on precisely the row that may carry exposure with no
     * working protection (#185 P0-3).
     */
    server.use(
      http.get(/.*\/api\/trading\/orders$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingOrdersFixture({
            cases_without_orders: [],
            orders: [tradingOrderFixture({ state: "ACKNOWLEDGED" })],
          }),
        }),
      ),
    );
    renderLeverage();

    const detail = await screen.findByRole("region", { name: /^案例 / });
    expect(detail).toHaveTextContent("账本尚未证明交易所持有对应的原生止损（ACKNOWLEDGED）");
    expect(detail).not.toHaveTextContent("触发原生止损");
  });

  it("reads a cold status failure as a failure, not as an unconfigured floor", async () => {
    // `EMPTY_FLOORS` would otherwise print 未配置地板 / 不适用 for thresholds the operator has configured.
    server.use(
      http.get(/.*\/api\/news\/status$/, () =>
        HttpResponse.json({ ok: false, error: "status unavailable" }, { status: 503 }),
      ),
    );
    renderLeverage();

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText("未配置地板", { exact: false })).toBeNull();
  });

  it("says the lane produced nothing rather than drawing an empty frame", async () => {
    server.use(
      http.get(/.*\/api\/trading\/orders$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingOrdersFixture({ cases_without_orders: [], orders: [] }),
        }),
      ),
    );
    renderLeverage();

    expect(await screen.findByText("24 小时内没有成案")).toBeInTheDocument();
    expect(
      screen.getByText("过去 24 小时的账本批次里没有一条案例。帧与闸门在 OI 遥测审计。"),
    ).toBeInTheDocument();
  });

  it("binds no document-level keys", async () => {
    // #82 cut the console's keyboard layer whole and `keyboardLayerHardCut.test.ts` keeps it cut. The
    // artifact's `j/k/f` list bindings are deliberately not here; the cards are real buttons in tab order.
    renderLeverage();
    const list = await screen.findByRole("region", { name: "案例列表" });

    fireEvent.keyDown(document, { key: "j" });
    expect(within(list).getAllByRole("button")[0]).toHaveAttribute("aria-pressed", "true");
  });
});

function renderLeverage(path = "/news/leverage") {
  return renderWithRouter(<NewsPage token="test-token" view="leverage" />, path);
}

function renderWithRouter(node: ReactNode, path: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <div className="center-column">
          {node}
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
