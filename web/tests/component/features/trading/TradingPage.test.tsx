import { TradingPage } from "@features/trading";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import {
  TRADING_NOW_MS,
  tradingCaseFixture,
  tradingCasesFixture,
  tradingCommandRowFixture,
  tradingCurrentAccountFixture,
  tradingExecutionRowFixture,
  tradingExecutionsFixture,
  tradingGateFixture,
  tradingLiveExecutionFixture,
  tradingStatusFixture,
} from "@tests/fixtures/tradingFixture";
import { server } from "@tests/msw/server";
import { HttpResponse, http } from "msw";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * One page, six blocks, one endpoint each (#528 PR-2).
 *
 * The tests below are mostly about the page not inventing anything: every stage word, disposition and
 * figure here is a field the server already folded, and the one comparison the browser is allowed to make
 * is `Date.now()` against the expiry instant `/status` publishes.
 */
describe("TradingPage", () => {
  beforeEach(() => {
    vi.spyOn(Date, "now").mockReturnValue(TRADING_NOW_MS);
    server.use(
      http.get(/.*\/api\/trading\/status$/, () =>
        HttpResponse.json({ ok: true, data: tradingStatusFixture() }),
      ),
      http.get(/.*\/api\/trading\/cases$/, () =>
        HttpResponse.json({ ok: true, data: tradingCasesFixture() }),
      ),
      http.get(/.*\/api\/trading\/executions$/, () =>
        HttpResponse.json({ ok: true, data: tradingExecutionsFixture() }),
      ),
      http.get(/.*\/api\/trading\/gate$/, () =>
        HttpResponse.json({ ok: true, data: tradingGateFixture() }),
      ),
    );
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("block 1 names the blocking reason in Chinese and the Runtime's route count", async () => {
    renderTrading();

    expect(await screen.findByRole("heading", { name: "Trading Desk" })).toBeVisible();
    const safety = screen.getByLabelText("执行安全状态");
    expect(within(safety).getAllByText("NO")).toHaveLength(3);
    expect(within(safety).getByText("NOT PROVEN")).toBeVisible();
    expect(within(safety).getByText("执行通道未启用")).toBeVisible();
    expect(screen.getByText(/Runtime 可执行市场 0 个/)).toBeVisible();
  });

  it("block 1 degrades every safety word once the server's own expiry instant has passed", async () => {
    /*
     * One comparison, against the instant `/status` published as the end of its own budget. The page
     * keeps no timer and re-derives no heartbeat age — the two clocks that used to do that disagreed
     * with the server about ages it had already measured.
     */
    server.use(
      http.get(/.*\/api\/trading\/status$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingStatusFixture({
            execution: tradingLiveExecutionFixture({
              account_flat_proven: true,
              entries_armed: true,
              entries_paused: false,
              entry_block_reason: null,
              facts_expire_at_ms: TRADING_NOW_MS - 1,
            }),
          }),
        }),
      ),
    );
    renderTrading();

    const safety = await screen.findByLabelText("执行安全状态");
    expect(within(safety).getAllByText("过期")).toHaveLength(4);
    expect(within(safety).queryByText("YES")).toBeNull();
    expect(within(safety).queryByText("PROVEN")).toBeNull();
    expect(screen.getByText(/本次读取的事实已过期/)).toBeVisible();
  });

  it("block 2 shows equity, protection, the order trigger price, and an unhealthy audit's reason", async () => {
    server.use(
      http.get(/.*\/api\/trading\/status$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingStatusFixture({
            execution: tradingLiveExecutionFixture({
              current_account: tradingCurrentAccountFixture({
                audit_failure_reason: "audit_append_failed",
                audit_healthy: false,
              }),
            }),
          }),
        }),
      ),
    );
    renderTrading();

    expect(await screen.findByText("$997.50")).toBeVisible();
    expect(screen.getByText("1,000 ms")).toBeVisible();
    expect(screen.getByText("audit_append_failed")).toBeVisible();
    expect(screen.getAllByText("FULL COVERAGE").length).toBeGreaterThan(0);
    // The stop's trigger price is on the open-order row, not only on the position's protection strip.
    expect(screen.getAllByText("Trigger 9800")).toHaveLength(2);
    expect(screen.getByText("−$0.03")).toBeVisible();
  });

  it("block 3 writes a Command with no second confirmation and renders the server's stage word", async () => {
    vi.stubGlobal("crypto", {
      randomUUID: () => "11111111-1111-4111-8111-111111111111",
    });
    let posted: unknown;
    let authorization: string | null = null;
    server.use(
      http.get(/.*\/api\/trading\/status$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingStatusFixture({ execution: tradingLiveExecutionFixture() }),
        }),
      ),
      http.post(/.*\/api\/trading\/execution\/commands$/, async ({ request }) => {
        authorization = request.headers.get("authorization");
        posted = await request.json();
        return HttpResponse.json({
          ok: true,
          data: {
            command_id: "a".repeat(64),
            disposition: "awaiting_runtime",
            reason: null,
            requested_at_ns: 1,
            seq: 7,
            truth: "intent_recorded_not_runtime_or_venue",
          },
        });
      }),
    );
    renderTrading();

    // The flatten Command already in the window is `completed`: its private reconciliation proved flat.
    expect(await screen.findByText("已完成 · 私有对账证明")).toBeVisible();
    expect(screen.getByText("已持久化")).toBeVisible();
    expect(screen.getAllByText("console:operator")).toHaveLength(2);

    fireEvent.click(screen.getByRole("button", { name: "Resume / Arm" }));
    expect(screen.queryByRole("alertdialog")).toBeNull();
    await waitFor(() => expect(posted).toBeDefined());
    expect(authorization).toBe("Bearer test-token");
    expect(posted).toMatchObject({
      request_id: "11111111-1111-4111-8111-111111111111",
      text: "/resume operator console",
    });
    expect(await screen.findByText(/Command 已持久化/)).toHaveTextContent(
      "这不代表 Runtime 受理、订单或成交",
    );
  });

  it("block 4 renders one row per entry from the server's own fields and totals the realized PnL", async () => {
    renderTrading();

    const closed = await screen.findByText("crypto:perp:BTC:USDT");
    const row = closed.closest(".trading-execution-row") as HTMLElement;
    expect(within(row).getByText("Signal")).toBeVisible();
    expect(within(row).getByText("已平仓")).toBeVisible();
    expect(within(row).getByText("已受理")).toBeVisible();
    expect(within(row).getByText("0.049")).toBeVisible();
    expect(within(row).getByText("9699.0")).toBeVisible();
    expect(within(row).getByText("−$14.92")).toBeVisible();
    expect(within(row).getByText("flatten 退出")).toBeVisible();

    // A refusal keeps its named reason and shows no venue column at all.
    const unmapped = screen.getByText("crypto:perp:NVDA:USDT").closest(".trading-execution-row")!;
    expect(within(unmapped as HTMLElement).getByText("运行时目录里没有这个市场")).toBeVisible();
    expect(within(unmapped as HTMLElement).getByText("已拒绝")).toBeVisible();
    expect(screen.getByText("Signal 已过期")).toBeVisible();

    /*
     * #528 PR-3. The CLI manual entry is a row of its own, keyed on its Command and holding the same
     * venue facts — before this it existed only as `manual_entry accepted` in block 3, with the fills,
     * the exit and the realized result it produced nowhere on the desk.
     */
    const manual = screen.getByText("crypto:perp:ETH:USDT").closest(".trading-execution-row")!;
    expect(within(manual as HTMLElement).getByText("手工")).toBeVisible();
    expect(within(manual as HTMLElement).getByText("已平仓")).toBeVisible();
    expect(within(manual as HTMLElement).getByText("0.0122")).toBeVisible();
    expect(within(manual as HTMLElement).getByText("81100.0")).toBeVisible();
    expect(within(manual as HTMLElement).getByText("−$1.12")).toBeVisible();
    expect(within(manual as HTMLElement).queryByRole("link")).toBeNull();

    expect(
      screen.getByText(/入场 4（Signal 3 · 手工 1）· 执行 2 · 已实现 −\$16\.04/),
    ).toBeVisible();
  });

  it("block 5 shows both durable distributions and the admission configuration that filed them", async () => {
    renderTrading();

    expect(await screen.findByRole("heading", { name: "准入闸 · TRADING" })).toBeVisible();
    const admission = screen
      .getByRole("heading", { name: "来源准入 · 24h" })
      .closest("section") as HTMLElement;
    expect(within(admission).getByText("已拒绝")).toBeVisible();
    expect(within(admission).getByText("87")).toBeVisible();
    expect(
      within(admission).getByText("持仓额低于流动性地板 · eligibility:oi_value_below_floor"),
    ).toBeVisible();
    expect(admission).toHaveTextContent("最新来源 08-25 11:59");

    const alpha = screen
      .getByRole("heading", { name: "Alpha 成案 · 24h" })
      .closest("section") as HTMLElement;
    expect(within(alpha).getByText("24h 成案").nextSibling).toHaveTextContent("7");
    expect(within(alpha).getByText("smart_money_ratio_below_or_equal_floor")).toBeVisible();

    const gate = screen
      .getByRole("heading", { name: "准入闸 · TRADING" })
      .closest("section") as HTMLElement;
    expect(within(gate).getByText("≥500 万")).toBeVisible();
    expect(within(gate).getByText("5m")).toBeVisible();
    expect(gate).toHaveTextContent("binance.usdm · hyperliquid.perp · hyperliquid.xyz");
    expect(gate).toHaveTextContent("trading_admission_v6");
  });

  it("block 6 opens one Case's frozen evidence and says when the window itself was truncated", async () => {
    server.use(
      http.get(/.*\/api\/trading\/cases$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingCasesFixture({
            cases: [tradingCaseFixture({ base_symbol: "SOL", case_id: "case-sol" })],
            complete: false,
          }),
        }),
      ),
    );
    renderTrading();

    expect(
      await screen.findByText("本窗口已截断；未列出的 Case 不能解释为没有发生。"),
    ).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: /SOL/ }));
    expect(await screen.findByRole("region", { name: "案例 SOL" })).toBeVisible();
    expect(screen.getByText("whale_oi_ratio_bps")).toBeVisible();
    expect(screen.getByText("未通过")).toBeVisible();
  });

  it("does not turn a failed execution read into an empty ledger", async () => {
    server.use(
      http.get(/.*\/api\/trading\/executions$/, () =>
        HttpResponse.json({ ok: false, error: "executions_unavailable" }, { status: 503 }),
      ),
    );
    renderTrading();

    expect(await screen.findByText("执行账本读取失败，不能据此断言为空。")).toBeVisible();
    expect(screen.getByText("Command 账本读取失败，不能据此断言为空。")).toBeVisible();
    expect(screen.getByText(/执行 读取失败/)).toBeVisible();
  });

  it("reuses the exact command envelope after an unknown submission result", async () => {
    let uuidCalls = 0;
    vi.stubGlobal("crypto", {
      randomUUID: () => {
        uuidCalls += 1;
        return "33333333-3333-4333-8333-333333333333";
      },
    });
    const bodies: unknown[] = [];
    let attempts = 0;
    server.use(
      http.get(/.*\/api\/trading\/status$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingStatusFixture({ execution: tradingLiveExecutionFixture() }),
        }),
      ),
      http.post(/.*\/api\/trading\/execution\/commands$/, async ({ request }) => {
        bodies.push(await request.json());
        attempts += 1;
        if (attempts === 1) {
          return HttpResponse.json({ ok: false, error: "service_busy" }, { status: 503 });
        }
        return HttpResponse.json({
          ok: true,
          data: {
            command_id: "c".repeat(64),
            disposition: "awaiting_runtime",
            reason: null,
            requested_at_ns: 1,
            seq: 9,
            truth: "intent_recorded_not_runtime_or_venue",
          },
        });
      }),
    );
    renderTrading();

    fireEvent.click(await screen.findByRole("button", { name: "Resume / Arm" }));
    expect(await screen.findByText(/提交结果未知/)).toHaveTextContent(
      "复用同一 request ID、时钟和文本",
    );

    fireEvent.click(screen.getByRole("button", { name: "Resume / Arm" }));
    await waitFor(() => expect(bodies).toHaveLength(2));

    expect(uuidCalls).toBe(1);
    expect(bodies[1]).toEqual(bodies[0]);
  });

  it("locks every control while execution.mode is disabled", async () => {
    renderTrading();

    expect(await screen.findByText(/控制已锁定/)).toBeVisible();
    expect(screen.getByRole("button", { name: "Pause entries" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Resume / Arm" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Flatten account" })).toBeDisabled();
  });

  it("says the window is empty rather than failed when the ledgers answer with nothing", async () => {
    server.use(
      http.get(/.*\/api\/trading\/cases$/, () =>
        HttpResponse.json({ ok: true, data: tradingCasesFixture({ cases: [] }) }),
      ),
      http.get(/.*\/api\/trading\/executions$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingExecutionsFixture({ commands: [], executions: [] }),
        }),
      ),
    );
    renderTrading();

    expect(await screen.findByText("当前 24 小时窗口没有入场。")).toBeVisible();
    expect(screen.getByText("当前 24 小时窗口没有 Command。")).toBeVisible();
    expect(screen.getByText("当前 24 小时窗口没有 Case。")).toBeVisible();
  });

  it("names a truncated execution window without dropping the rows it did read", async () => {
    server.use(
      http.get(/.*\/api\/trading\/executions$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingExecutionsFixture({
            complete: false,
            executions: [tradingExecutionRowFixture()],
          }),
        }),
      ),
    );
    renderTrading();

    expect(await screen.findByText("本窗口已截断；未列出的入场不能解释为没有发生。")).toBeVisible();
    expect(screen.getByText("crypto:perp:BTC:USDT")).toBeVisible();
  });

  it("renders every Command stage the server can derive, including a rejection", async () => {
    server.use(
      http.get(/.*\/api\/trading\/executions$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingExecutionsFixture({
            commands: [
              tradingCommandRowFixture({ stage: "accepted" }),
              tradingCommandRowFixture({
                action: "resume_entries",
                command_id: "d".repeat(64),
                stage: "rejected",
              }),
              tradingCommandRowFixture({
                action: "flatten",
                command_id: "e".repeat(64),
                stage: "expired",
              }),
            ],
          }),
        }),
      ),
    );
    renderTrading();

    const commands = (await screen.findByText("Runtime 受理")).closest(
      ".trading-command-list",
    ) as HTMLElement;
    expect(within(commands).getByText("Runtime 拒绝")).toBeVisible();
    expect(within(commands).getByText("已过期")).toBeVisible();
  });
});

function renderTrading() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return {
    client,
    ...render(
      <MemoryRouter>
        <QueryClientProvider client={client}>
          <TradingPage token="test-token" />
        </QueryClientProvider>
      </MemoryRouter>,
    ),
  };
}
