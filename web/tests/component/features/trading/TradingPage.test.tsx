import { TradingPage } from "@features/trading";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import {
  CASE_ID,
  TRADING_NOW_MS,
  tradingCasesForCaseId,
  tradingExecutionsFixture,
  tradingScoreboardFixture,
  tradingStatusFixture,
} from "@tests/fixtures/tradingFixture";
import { server } from "@tests/msw/server";
import { HttpResponse, http } from "msw";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

function renderTrading(entry = "/trading") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <QueryClientProvider client={client}>
        <TradingPage token="test-token" />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

describe("TradingPage", () => {
  beforeEach(() => {
    vi.spyOn(Date, "now").mockReturnValue(TRADING_NOW_MS);
    server.use(
      http.get(/.*\/api\/trading\/status$/, () =>
        HttpResponse.json({ ok: true, data: tradingStatusFixture() }),
      ),
      http.get(/.*\/api\/trading\/cases$/, ({ request }) =>
        HttpResponse.json({
          ok: true,
          data: tradingCasesForCaseId(new URL(request.url).searchParams.get("case_id")),
        }),
      ),
      http.get(/.*\/api\/trading\/scoreboard$/, () =>
        HttpResponse.json({ ok: true, data: tradingScoreboardFixture() }),
      ),
      http.get(/.*\/api\/trading\/executions$/, () =>
        HttpResponse.json({ ok: true, data: tradingExecutionsFixture() }),
      ),
    );
  });
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("keeps signed DEMO exposure separate from LIVE analysis", async () => {
    renderTrading();
    expect(await screen.findByRole("heading", { name: "交易执行" })).toBeVisible();
    expect(screen.getByText(/LIVE 行情评估与 DEMO 账户执行分开记录/)).toBeVisible();
    expect(await screen.findByLabelText("执行安全状态")).toBeVisible();
  });

  it("shows the same six policy scores and explicit insufficient data returned by the API", async () => {
    renderTrading("/trading?tab=scoreboard");
    expect(await screen.findByRole("heading", { name: "预测 → 决策 → 纸面 → 执行" })).toBeVisible();
    const table = screen.getAllByRole("table")[0];
    expect(table.querySelectorAll("tbody tr")).toHaveLength(6);
    expect(screen.getAllByText("数据不足").length).toBeGreaterThan(0);
    expect(screen.getByText("12")).toBeVisible();
    expect(screen.getByText("10")).toBeVisible();
  });

  it("shows observed stop-profit rates beside forecast probability bins", async () => {
    const scoreboard = tradingScoreboardFixture();
    scoreboard.programs[0].forecast = {
      matched_baseline_legs: 30,
      baseline_coverage: "0.75",
      legs: 40,
      multiclass_brier: "0.42",
      log_loss: "0.6",
      brier_skill_score: "0.1",
      reliability: [{ bin: 6, count: 20, observed_tp_rate: "0.65" }],
      status: "ok",
    };
    server.use(
      http.get(/.*\/api\/trading\/scoreboard$/, () =>
        HttpResponse.json({ ok: true, data: scoreboard }),
      ),
    );
    renderTrading("/trading?tab=scoreboard");
    const table = await screen.findByRole("table", { name: "止盈概率可靠性" });
    expect(table).toHaveTextContent("60–70%");
    expect(table).toHaveTextContent("20");
    expect(table).toHaveTextContent("65.0%");
  });

  it("opens one Case with frozen program, actions and paper legs", async () => {
    renderTrading("/trading?tab=cases");
    const row = await screen.findByRole("button", { name: /crypto:HYPE/ });
    fireEvent.click(row);
    expect(await screen.findByText(CASE_ID)).toBeVisible();
    expect(screen.getByText("同场 Policy 动作")).toBeVisible();
    expect(screen.getByText("LIVE 纸面两腿")).toBeVisible();
  });
});
