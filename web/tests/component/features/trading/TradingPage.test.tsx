import { TradingPage } from "@features/trading";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import {
  tradingCaseFixture,
  tradingCasesFixture,
  tradingObservationFixture,
  tradingObservationsFixture,
  tradingSignalFixture,
  tradingSignalsFixture,
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
      http.get(/.*\/api\/trading\/cases$/, () =>
        HttpResponse.json({ ok: true, data: tradingCasesFixture() }),
      ),
      http.get(/.*\/api\/trading\/signals$/, () =>
        HttpResponse.json({ ok: true, data: tradingSignalsFixture() }),
      ),
      http.get(/.*\/api\/trading\/execution\/observations$/, () =>
        HttpResponse.json({ ok: true, data: tradingObservationsFixture() }),
      ),
    );
  });

  afterEach(cleanup);

  it("renders the explicit C boundary without claiming execution readiness", async () => {
    renderTrading();

    expect(await screen.findByRole("heading", { name: "Alpha / Execution" })).toBeVisible();
    expect(await screen.findByText(/当前边界只输出 engine-neutral/)).toBeVisible();
    expect(screen.getAllByText("disabled").length).toBeGreaterThan(0);
    expect(screen.getByText("当前 24 小时窗口没有 Observation。")).toBeVisible();
    expect(screen.queryByText(/Capital|Intent|capability partition/i)).toBeNull();
  });

  it("shows a Case and its separately read Signal with the same identity", async () => {
    server.use(
      http.get(/.*\/api\/trading\/cases$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingCasesFixture({
            cases: [
              tradingCaseFixture({
                base_symbol: "SOL",
                case_id: "case-sol",
                market_key: "crypto:perp:SOL:USDT",
                policy_decision: "long",
                policy_reason: "smart_money_momentum_long",
                state: "SIGNAL_EMITTED",
                underlying_key: "crypto:SOL",
              }),
            ],
          }),
        }),
      ),
      http.get(/.*\/api\/trading\/signals$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingSignalsFixture({ signals: [tradingSignalFixture({ case_id: "case-sol" })] }),
        }),
      ),
    );

    renderTrading();

    expect((await screen.findAllByText("crypto:perp:SOL:USDT")).length).toBeGreaterThan(0);
    expect(screen.getByText("已发出 Signal")).toBeVisible();
    expect(screen.getByText("VALID")).toBeVisible();
  });

  it("does not turn a Signal read failure into an empty ledger", async () => {
    server.use(
      http.get(/.*\/api\/trading\/signals$/, () =>
        HttpResponse.json({ ok: false, error: "signal_unavailable" }, { status: 503 }),
      ),
    );

    renderTrading();

    expect(await screen.findByText("Signal 账本读取失败，不能据此断言为空。")).toBeVisible();
    expect(screen.queryByText("当前 24 小时窗口没有 Signal。")).toBeNull();
  });

  it("renders durable execution observations without inferring an order state", async () => {
    server.use(
      http.get(/.*\/api\/trading\/execution\/observations$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingObservationsFixture({ observations: [tradingObservationFixture()] }),
        }),
      ),
    );

    renderTrading();

    expect(await screen.findByText("signal_disposition")).toBeVisible();
    expect(screen.getAllByText("demo-v1").length).toBeGreaterThan(0);
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
