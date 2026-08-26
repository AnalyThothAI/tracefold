import { TradingPage } from "@features/trading";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import {
  tradingOrderFixture,
  tradingOrdersFixture,
  tradingStatusFixture,
} from "@tests/fixtures/tradingFixture";
import { server } from "@tests/msw/server";
import { HttpResponse, http } from "msw";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

/**
 * 模拟仓 (#207 PR-W4, #185).
 *
 * Almost everything worth testing here is a refusal: the page must not translate a ledger state into a
 * stronger claim, must not put a protection mark on an unprotected position, must not offer an action on an
 * order nobody has reconciled, and must not read as broken when the lane is simply switched off.
 */
describe("TradingPage", () => {
  beforeEach(() => {
    server.use(
      http.get(/.*\/api\/trading\/status$/, () =>
        HttpResponse.json({ ok: true, data: tradingStatusFixture() }),
      ),
      http.get(/.*\/api\/trading\/orders$/, () =>
        HttpResponse.json({ ok: true, data: tradingOrdersFixture() }),
      ),
    );
  });

  afterEach(cleanup);

  it("says the lane is switched off rather than letting empty panels read as an outage", async () => {
    server.use(
      http.get(/.*\/api\/trading\/status$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingStatusFixture({
            counts: {
              ...tradingStatusFixture().counts,
              active_orders: 0,
              closed_orders_today: 0,
            },
            readiness: {
              ...tradingStatusFixture().readiness,
              enabled: false,
              execution_backend: "disabled",
              execution_configured: false,
            },
          }),
        }),
      ),
      http.get(/.*\/api\/trading\/orders$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingOrdersFixture({ cases_without_orders: [], orders: [] }),
        }),
      ),
    );

    renderTrading();

    expect(await screen.findByText(/资本通道未启用/)).toBeInTheDocument();
    expect(await screen.findByText("当前没有任何持仓或未决意图。")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重试" })).not.toBeInTheDocument();
  });

  it("keeps the ledger's own state word instead of translating it into 已成交", async () => {
    server.use(
      http.get(/.*\/api\/trading\/orders$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingOrdersFixture({
            orders: [
              tradingOrderFixture({
                filled_quantity: null,
                order_id: "order-ack",
                position_opened_at_ms: null,
                state: "ACKNOWLEDGED",
              }),
            ],
          }),
        }),
      ),
    );

    renderTrading();

    const row = await screen.findByText("ACKNOWLEDGED");
    expect(row).toBeInTheDocument();
    expect(screen.queryByText("已成交")).not.toBeInTheDocument();
    // ACK is the venue answering; there is no authoritative quantity yet and the hold clock has not started.
    expect(row.closest(".trading-exposure-row")).toHaveTextContent("—");
  });

  it("marks the native stop only on OPEN, which is the state that proved it", async () => {
    renderTrading();

    const open = await screen.findByText("OPEN");
    expect(
      within(open.closest(".trading-exposure-row") as HTMLElement).getByLabelText("原生止损已验证"),
    ).toBeInTheDocument();
  });

  it("gives an unprotected order a stop price and no verification mark", async () => {
    /*
     * Every prepared order carries a stop *price* — that is the frozen intent. The tick means a read came
     * back proving a reduce-only stop covering the filled quantity, which is exactly what `OPEN` adds. A
     * mark on a price would put a protection badge on an unprotected position.
     */
    server.use(
      http.get(/.*\/api\/trading\/orders$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingOrdersFixture({
            orders: [tradingOrderFixture({ order_id: "order-unprotected", state: "UNPROTECTED" })],
          }),
        }),
      ),
    );

    renderTrading();

    const row = (await screen.findByText("UNPROTECTED")).closest(
      ".trading-exposure-row",
    ) as HTMLElement;
    expect(row).toHaveTextContent("0.8244");
    expect(within(row).queryByLabelText("原生止损已验证")).not.toBeInTheDocument();
  });

  it("offers no action on an ambiguous order, or on any other", async () => {
    server.use(
      http.get(/.*\/api\/trading\/orders$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingOrdersFixture({
            orders: [tradingOrderFixture({ order_id: "order-ambiguous", state: "AMBIGUOUS" })],
          }),
        }),
      ),
    );

    renderTrading();

    const row = (await screen.findByText("AMBIGUOUS")).closest(
      ".trading-exposure-row",
    ) as HTMLElement;
    // The one thing that must never happen from a screen is a human resending an order nobody reconciled.
    expect(within(row).queryAllByRole("button")).toHaveLength(0);
    expect(row).toHaveAttribute("data-ambiguous", "true");
  });

  it("reports live readiness and offers no way to change it", async () => {
    renderTrading();

    expect(
      await screen.findByLabelText("MODE paper · LIVE READY not_applicable"),
    ).toBeInTheDocument();
    // `live` appears nowhere as a control — no switch, no toggle, no button naming it.
    for (const control of screen.queryAllByRole("button")) {
      expect(control.textContent ?? "").not.toMatch(/live/i);
    }
    expect(screen.queryByRole("switch")).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  });

  it("takes the unrealized label from the row mode instead of assuming paper", async () => {
    server.use(
      http.get(/.*\/api\/trading\/orders$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingOrdersFixture({
            orders: [tradingOrderFixture({ mode: "live_reviewed" })],
          }),
        }),
      ),
    );

    renderTrading();

    expect(await screen.findByText("未实现")).toBeInTheDocument();
    expect(screen.queryByText("未实现（纸面）")).not.toBeInTheDocument();
  });

  it("never reads as ready when the lane is enabled but its provider contract is not", async () => {
    /*
     * #207's own requirement, and the shape #185 P1-2 exists for: `enabled: true` with a live mode whose
     * OpenTrade contract failed to compose. The page states `execution_configured: false` and the ledger's
     * `not_proven`; there is no state of configuration in which it prints "ready".
     */
    server.use(
      http.get(/.*\/api\/trading\/status$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingStatusFixture({
            readiness: {
              control: "RUNNING",
              enabled: true,
              execution_backend: "opentrade_reviewed",
              execution_configured: false,
              live_mode_supported: true,
              live_ready: false,
              live_readiness: "not_proven",
              mode: "live_reviewed",
              venues: ["binance"],
            },
          }),
        }),
      ),
    );

    renderTrading();

    /*
     * The *value* under LIVE READY, not the block: the label names the field and is not a claim, and
     * matching the block's text would compare against `LIVE READYnot_proven`, where the two run together
     * and no word-boundary assertion can tell them apart.
     */
    const readiness = await screen.findByLabelText("MODE live_reviewed · LIVE READY not_proven");
    expect(readiness).toBeInTheDocument();
    // Enabled, so the "lane is switched off" banner is not the explanation here.
    expect(screen.queryByText(/资本通道未启用/)).not.toBeInTheDocument();
  });

  it("names the case that stopped, and the rule it stopped on", async () => {
    renderTrading();

    // The rejected population has no order to join through and is where the capital floors actually bite.
    expect(await screen.findByText("whale_profit_below_floor")).toBeInTheDocument();
    expect(screen.getByText("地板拒绝")).toBeInTheDocument();
    // The floors are beside it: a rejection reason means nothing without the number it failed.
    await waitFor(() => expect(screen.getByText(/鲸鱼盈利 ≥ 95%/)).toBeInTheDocument());
  });

  it("names why the frames that never reached a case were refused", async () => {
    // #264: the funnel started at 成案, so an entire population — 87 rejected frames in the fixture's
    // 24 h — had no representation at all. A lane at zero orders could not tell "the upstream is quiet"
    // from "every frame was below the liquidity floor".
    renderTrading();

    const panel = (await screen.findByText("未成案的来源帧")).closest("section") as HTMLElement;
    expect(within(panel).getByText("eligibility:oi_value_below_floor")).toBeInTheDocument();
    expect(within(panel).getByText("持仓额低于流动性地板")).toBeInTheDocument();
    // Descending by count, so the binding constraint is the first row rather than an alphabetical one.
    const keys = within(panel)
      .getAllByText(/^[a-z_]+:[a-z_]+$/)
      .map((node) => node.textContent);
    expect(keys[0]).toBe("eligibility:rank_above_limit");
    // A frame that *did* open a case is not a refusal and does not belong in this list.
    expect(within(panel).queryByText("freeze:case_created")).not.toBeInTheDocument();
  });

  it("starts the funnel before the case, and names the clock each row is counted on", async () => {
    renderTrading();

    const funnel = (await screen.findByText(/案例去向/)).closest("section") as HTMLElement;
    // Matched on the label alone; the clock is a nested `<em>` and rides on `textContent` below.
    const rows = within(funnel).getAllByText(/^(上游帧|过准入|成案|政策放行|提交订单|已了结|在场)$/);
    expect(rows.map((node) => node.textContent)).toEqual([
      "上游帧24h",
      "过准入24h",
      "成案日",
      "政策放行日",
      "提交订单日",
      "已了结日",
      "在场当前",
    ]);
    // 87 + 2 + 1 + 1 = 91 frames the admission ledger holds for the window.
    expect(within(funnel).getByText("91")).toBeInTheDocument();
    // Two clocks, labelled rather than merged: a rolling 24 h and a UTC calendar day.
    expect(within(funnel).getAllByText("24h")).toHaveLength(2);
  });

  it("says so plainly when the admission ledger has never held a frame", async () => {
    server.use(
      http.get(/.*\/api\/trading\/status$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingStatusFixture({
            counts: {
              ...tradingStatusFixture().counts,
              candidate_counts_24h: {},
              candidate_counts_7d: {},
              candidate_reasons_24h: {},
            },
          }),
        }),
      ),
    );
    renderTrading();

    const panel = (await screen.findByText("未成案的来源帧")).closest("section") as HTMLElement;
    expect(within(panel).getByText(/尚未运行过/)).toBeInTheDocument();
  });

  it("shows real liquidation shadow cohorts without presenting them as orders", async () => {
    renderTrading();

    fireEvent.click(await screen.findByText(/技术证据/));
    const continuation = await screen.findByText(/清算延续（影子） 2（完成 1） · 1h 均值 0.25%/);
    const cohort = continuation.closest(".trading-floors") as HTMLElement;
    expect(cohort).toHaveTextContent("清算衰竭（影子） 2（完成 1） · 1h 均值 0.25%");
    expect(cohort).toHaveTextContent("来源契约不完整 4");
    expect(cohort).toHaveTextContent("不可晋级：source_contract_incomplete");

    const study = screen.getByText("清算事件研究").closest(".trading-floors") as HTMLElement;
    expect(study).toHaveTextContent("清算延续（影子） · binance/unknown");
    expect(study).toHaveTextContent("holdout 2/2 · 覆盖 50%");
    expect(study).toHaveTextContent("延迟均值 1200ms");
    expect(study).toHaveTextContent("5m 0.25% [0.25%, 0.25%]");
    expect(study).toHaveTextContent("MFE/MAE 0.80%/-0.20%");
    expect(study).toHaveTextContent("出场：max_holding 1");
    expect(study).toHaveTextContent("净值(不含资金费) 0.12% [0.08%, 0.16%]");
    expect(study).toHaveTextContent("horizon:5s:source_bar_resolution_unsupported 1");
    expect(study).toHaveTextContent(
      "晋级阻断：source_contract_incomplete、intraminute_coverage_missing",
    );

    // Shadow evaluations are evidence rows only. They must not become exposure or closed-order rows.
    for (const row of document.querySelectorAll(".trading-exposure-row, .trading-closed-row")) {
      expect(row).not.toHaveTextContent(/清算延续|清算衰竭/);
    }
  });

  it("shows an unmeasured close as — rather than a zero that would drag an average", async () => {
    server.use(
      http.get(/.*\/api\/trading\/orders$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingOrdersFixture({
            orders: [
              tradingOrderFixture({
                exit_price: "0.8590",
                exit_reason: "operator_resolution",
                order_id: "order-resolved",
                position_closed_at_ms: TRADING_CLOSED_MS,
                realized_bps: null,
                state: "CLOSED",
              }),
            ],
          }),
        }),
      ),
    );

    renderTrading();

    const row = (await screen.findByText("operator_resolution")).closest(
      ".trading-closed-row",
    ) as HTMLElement;
    expect(within(row).getByTitle("出场未被测量，不进入已实现口径")).toBeInTheDocument();
    expect(row).not.toHaveTextContent("0.00%");
  });
});

const TRADING_CLOSED_MS = Date.parse("2026-08-25T11:59:00Z");

function renderTrading() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/trading"]}>
        <div className="center-column">
          <TradingPage token="test-token" />
        </div>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}
