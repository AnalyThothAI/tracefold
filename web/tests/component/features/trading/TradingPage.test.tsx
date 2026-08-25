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
