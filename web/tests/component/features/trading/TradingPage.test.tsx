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

    expect(await screen.findByRole("heading", { name: "Case → Intent → Outcome" })).toBeVisible();
    expect(screen.getByText(/Nautilus · Binance USD-M Demo/)).toBeVisible();
    expect(await screen.findByText("OPEN_PROTECTED")).toBeVisible();
    expect(screen.queryByText(/paper|OpenTrade|订单/i)).toBeNull();
  });

  it("renders terminal outcome from the intent ledger", async () => {
    server.use(
      http.get(/.*\/api\/trading\/intents$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingIntentsFixture({
            cases_without_intents: [],
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

    expect(await screen.findByText("CLOSED_FLAT")).toBeVisible();
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
