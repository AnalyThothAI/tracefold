import { TradingPage } from "@features/trading";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import {
  tradingIntentFixture,
  tradingIntentsFixture,
  tradingStatusFixture,
} from "@tests/fixtures/tradingFixture";
import { server } from "@tests/msw/server";
import { HttpResponse, http } from "msw";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

describe("TradingPage", () => {
  beforeEach(() => {
    server.use(
      http.get(/.*\/api\/trading\/status$/, () =>
        HttpResponse.json({ ok: true, data: tradingStatusFixture() }),
      ),
      http.get(/.*\/api\/trading\/intents$/, () =>
        HttpResponse.json({ ok: true, data: tradingIntentsFixture() }),
      ),
    );
  });

  afterEach(cleanup);

  it("renders the frozen execution authority and native intent state", async () => {
    renderTrading();

    expect(await screen.findByRole("heading", { name: "执行与持仓" })).toBeVisible();
    expect(screen.getByText(/Nautilus · Binance USD-M Demo/)).toBeVisible();
    expect(await screen.findByText("OPEN_PROTECTED")).toBeVisible();
    expect(screen.queryByText(/paper|OpenTrade|订单/i)).toBeNull();
  });

  it("carries no Case list, and names the upstream totals when there is no Intent", async () => {
    /*
     * #331: `/intents` no longer returns `cases_without_intents`, so the page cannot restate decisions.
     * `0 Intent` is a truthful empty that says what the lane *did* do, with a link to where it did it —
     * never a blank panel that reads as "the system had no data".
     */
    server.use(
      http.get(/.*\/api\/trading\/intents$/, () =>
        HttpResponse.json({ ok: true, data: tradingIntentsFixture({ intents: [] }) }),
      ),
    );

    renderTrading();

    expect(await screen.findByText(/Nautilus 不持有待执行工作/)).toBeVisible();
    expect(screen.getByRole("link", { name: "资本判定" })).toBeVisible();
    expect(screen.queryByText("Cases without Intent")).toBeNull();
  });

  it("keeps the readiness answer and says the Intent ledger failed", async () => {
    server.use(
      http.get(/.*\/api\/trading\/intents$/, () => HttpResponse.json({ ok: false }, { status: 503 })),
    );

    renderTrading();

    expect(await screen.findByRole("heading", { name: "执行与持仓" })).toBeVisible();
    expect(await screen.findByText(/Intent 账本读取失败/)).toBeVisible();
  });

  it("renders terminal outcome from the intent ledger", async () => {
    server.use(
      http.get(/.*\/api\/trading\/intents$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingIntentsFixture({
            intents: [
              tradingIntentFixture({
                closed_at_ms: Date.now(),
                execution_phase: "EXIT",
                execution_state: "TERMINAL",
                realized_pnl_amount: "0.25",
                realized_pnl_currency: "USDT",
                terminal_outcome: "CLOSED_FLAT",
              }),
            ],
          }),
        }),
      ),
    );

    renderTrading();

    // The row and the 24 h outcome distribution both name it; the realised amount appears once.
    expect((await screen.findAllByText("CLOSED_FLAT")).length).toBeGreaterThan(0);
    expect(screen.getByText("0.25 USDT")).toBeVisible();
  });
});

function renderTrading() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <TradingPage token="test-token" />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}
