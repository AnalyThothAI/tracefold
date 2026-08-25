import { TradingPage } from "@features/trading";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
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
      http.get(/.*\/api\/trading\/orders$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingOrdersFixture({ cases_without_orders: [], orders: [] }),
        }),
      ),
    );

    renderTrading();

    expect(await screen.findByText(/资本通道未启用/)).toBeInTheDocument();
    expect(screen.getByText("当前没有任何持仓或未决意图。")).toBeInTheDocument();
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

    expect(await screen.findByText("not_applicable")).toBeInTheDocument();
    // `live` appears nowhere as a control — no switch, no toggle, no button naming it.
    for (const control of screen.queryAllByRole("button")) {
      expect(control.textContent ?? "").not.toMatch(/live/i);
    }
    expect(screen.queryByRole("switch")).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
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
    const readiness = (await screen.findByText("LIVE READY")).closest(
      ".trading-stat",
    ) as HTMLElement;
    const value = readiness.querySelector("b") as HTMLElement;
    expect(value).toHaveTextContent("not_proven");
    expect(value.textContent).not.toMatch(/^(ready|true|ok)$/i);
    // Enabled, so the "lane is switched off" banner is not the explanation here.
    expect(screen.queryByText(/资本通道未启用/)).not.toBeInTheDocument();
  });

  it("names the case that stopped, and the rule it stopped on", async () => {
    renderTrading();

    // The rejected population has no order to join through and is where the capital floors actually bite.
    expect(await screen.findByText("whale_profit_below_floor")).toBeInTheDocument();
    // Twice on purpose: once as a funnel bar with its count, once as the named row underneath it. The
    // funnel is what happened; the row is which case and why.
    expect(screen.getAllByText("地板拒绝")).toHaveLength(2);
    // The floors are beside it: a rejection reason means nothing without the number it failed.
    await waitFor(() => expect(screen.getByText(/鲸鱼盈利 ≥ 95%/)).toBeInTheDocument());
  });

  it("shows real liquidation shadow cohorts without presenting them as orders", async () => {
    renderTrading();

    const continuation = await screen.findByText(/清算延续（影子） 2（完成 1） · 1h 均值 0.25%/);
    const cohort = continuation.closest(".trading-floors") as HTMLElement;
    expect(cohort).toHaveTextContent("清算衰竭（影子） 2（完成 1） · 1h 均值 0.25%");
    expect(cohort).toHaveTextContent("来源契约不完整 4");
    expect(cohort).toHaveTextContent("不可晋级：source_contract_incomplete");

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

const TRADING_CLOSED_MS = 1_779_000_000_000 - 60_000;

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
