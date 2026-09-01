import { TradingPage } from "@features/trading";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import {
  tradingCaseFixture,
  tradingCasesFixture,
  tradingCommandFixture,
  tradingCommandsFixture,
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
      http.get(/.*\/api\/trading\/execution\/commands$/, () =>
        HttpResponse.json({ ok: true, data: tradingCommandsFixture() }),
      ),
    );
  });

  afterEach(cleanup);

  it("renders the execution truth boundary without claiming disabled execution readiness", async () => {
    renderTrading();

    expect(await screen.findByRole("heading", { name: "Alpha / Execution" })).toBeVisible();
    expect(await screen.findByText(/Alpha 只输出 engine-neutral/)).toBeVisible();
    expect(screen.getAllByText("disabled").length).toBeGreaterThan(0);
    expect(screen.getByText("当前 24 小时窗口没有 Observation。")).toBeVisible();
    expect(screen.getByText("当前 24 小时窗口没有 Command。")).toBeVisible();
    expect(screen.getByText(/意图已记录 → Runtime 受理 → 订单已接受 → 成交/)).toBeVisible();
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

  it("renders an intent disposition without calling it a Runtime or venue success", async () => {
    server.use(
      http.get(/.*\/api\/trading\/execution\/commands$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingCommandsFixture({ commands: [tradingCommandFixture()] }),
        }),
      ),
    );

    renderTrading();

    expect(await screen.findByText("暂停开仓")).toBeVisible();
    expect(screen.getByText("not_applied · execution_profile_inactive")).toBeVisible();
    expect(screen.getByText(/HTTP 200 或 CLI ok 只证明意图已持久化/)).toBeVisible();
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
