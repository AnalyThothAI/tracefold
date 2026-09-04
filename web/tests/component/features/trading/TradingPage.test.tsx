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
  tradingLiveExecutionFixture,
  tradingStatusFixture,
} from "@tests/fixtures/tradingFixture";
import { server } from "@tests/msw/server";
import { HttpResponse, http } from "msw";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * Three blocks, and a Case drawer that opens on demand (#537 PR-5).
 *
 * RISK is `/api/trading/status`, ACT and CONFIRM are the two halves of `/api/trading/executions`, and
 * `/api/trading/cases` answers the drawer behind `?case=<id>` plus one durable 24 h card. The tests
 * below are mostly about the page not inventing anything: every stage word, disposition and figure is
 * a field the server already folded, and the one comparison the browser is allowed to make is
 * `Date.now()` against the expiry instant `/status` publishes.
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
    );
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("RISK names the blocking reason in Chinese and the Runtime's route count", async () => {
    renderTrading();

    expect(await screen.findByRole("heading", { name: "Trading Desk" })).toBeVisible();
    const safety = screen.getByLabelText("执行安全状态");
    expect(within(safety).getAllByText("NO")).toHaveLength(3);
    expect(within(safety).getByText("NOT PROVEN")).toBeVisible();
    expect(within(safety).getByText("执行通道未启用")).toBeVisible();
    expect(screen.getByText(/Runtime 可执行市场 0 个/)).toBeVisible();
  });

  it("RISK degrades every safety word once the server's own expiry instant has passed", async () => {
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

  it("RISK folds the order counts into the positions table it describes", async () => {
    /*
     * #537 PR-5. Open / inflight / unknown were a card of their own between the equity figures and the
     * positions, which read as a fifth safety answer. They are three integers about the same account.
     */
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
    const positions = screen
      .getByRole("heading", { name: "当前仓位与挂单" })
      .closest("section") as HTMLElement;
    expect(within(positions).getByText("Open").nextSibling).toHaveTextContent("1");
    expect(within(positions).getByText("Unknown").nextSibling).toHaveTextContent("0");
    expect(within(positions).getAllByText("FULL COVERAGE").length).toBeGreaterThan(0);
    // The stop's trigger price is on the open-order row, not only on the position's protection strip.
    expect(within(positions).getAllByText("Trigger 9800")).toHaveLength(2);
    expect(screen.getByText("−$0.03")).toBeVisible();
  });

  it("ACT writes a Command with no second confirmation and renders action, stage and clock only", async () => {
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
    // #537 PR-5: the reason repeated the field above the ledger and the identity was a constant.
    expect(screen.queryByText("maintenance")).toBeNull();
    expect(screen.queryByText("console:operator")).toBeNull();

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

  it("CONFIRM renders one row per entry from the server's own fields and totals the realized PnL", async () => {
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
     * venue facts — before this it existed only as `manual_entry accepted` in ACT, with the fills,
     * the exit and the realized result it produced nowhere on the desk. It has no Case, so its 来源
     * cell is a word rather than the button a Signal row carries (#537 PR-5).
     */
    const manual = screen.getByText("crypto:perp:ETH:USDT").closest(".trading-execution-row")!;
    expect(within(manual as HTMLElement).getByText("手工")).toBeVisible();
    expect(within(manual as HTMLElement).getByText("已平仓")).toBeVisible();
    expect(within(manual as HTMLElement).getByText("0.0122")).toBeVisible();
    expect(within(manual as HTMLElement).getByText("81100.0")).toBeVisible();
    expect(within(manual as HTMLElement).getByText("−$1.12")).toBeVisible();
    expect(within(manual as HTMLElement).queryByRole("button")).toBeNull();

    expect(
      screen.getByText(/入场 4（Signal 3 · 手工 1）· 执行 2 · 已实现 −\$16\.04/),
    ).toBeVisible();
  });

  it("CONFIRM opens the Case a Signal row authored in the drawer, keyed on the URL", async () => {
    server.use(
      http.get(/.*\/api\/trading\/cases$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingCasesFixture({ cases: [tradingCaseFixture({ case_id: "case-btc" })] }),
        }),
      ),
    );
    const { router } = renderTrading();

    const row = (await screen.findByText("crypto:perp:BTC:USDT")).closest(
      ".trading-execution-row",
    ) as HTMLElement;
    fireEvent.click(within(row).getByRole("button", { name: "Signal" }));

    expect(await screen.findByRole("region", { name: "案例 HYPE" })).toBeVisible();
    expect(screen.getByLabelText("案例抽屉")).toHaveTextContent("case-btc");
    expect(screen.getByText("whale_oi_ratio_bps")).toBeVisible();
    expect(screen.getByText("未通过")).toBeVisible();
    expect(router.search).toBe("?case=case-btc");

    fireEvent.click(screen.getByRole("button", { name: "关闭" }));
    await waitFor(() => expect(screen.queryByLabelText("案例抽屉")).toBeNull());
  });

  it("says a deep-linked Case is outside the window rather than showing nothing", async () => {
    renderTrading("/trading?case=case-gone");

    expect(await screen.findByLabelText("案例抽屉")).toHaveTextContent(
      "这个案例不在当前 24 小时窗口。",
    );
    expect(screen.queryByRole("region", { name: /^案例 / })).toBeNull();
  });

  it("keeps the durable 24 h Case card beside the ledgers rather than counting the rows on screen", async () => {
    renderTrading();

    const card = (await screen.findByRole("heading", { name: "Alpha 成案 · 24h" })).closest(
      "section",
    ) as HTMLElement;
    // 7 = 1 + 5 + 1 from `state_counts_24h`, which is not the one Case row the page rendered.
    expect(within(card).getByText("24h 成案").nextSibling).toHaveTextContent("7");
    expect(within(card).getByText("smart_money_ratio_below_or_equal_floor")).toBeVisible();
  });

  it("reads no admission ledger and no Signal list", async () => {
    /*
     * #537 PR-5. The desk downloaded up to 400 `decisions[]` from `/api/trading/gate` every 15 s and
     * rendered none of the rows; `/api/trading/signals` is deleted outright. `/news/oi` still joins
     * each frame to its own admission answer, which is the one surface that renders one.
     */
    const unexpected: string[] = [];
    server.use(
      http.get(/.*\/api\/trading\/(gate|signals).*/, ({ request }) => {
        unexpected.push(new URL(request.url).pathname);
        return HttpResponse.json({ ok: false, error: "unexpected" }, { status: 500 });
      }),
    );
    renderTrading();

    await screen.findByText("crypto:perp:BTC:USDT");
    expect(unexpected).toEqual([]);
    expect(screen.queryByRole("heading", { name: "准入闸 · TRADING" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "来源准入 · 24h" })).toBeNull();
  });

  it("does not turn a failed execution read into an empty ledger", async () => {
    server.use(
      http.get(/.*\/api\/trading\/executions$/, () =>
        HttpResponse.json({ ok: false, error: "executions_unavailable" }, { status: 503 }),
      ),
    );
    renderTrading();

    expect(await screen.findByText("执行账本读取失败，不能据此断言为空。")).toBeVisible();
    expect(screen.getByText("Command账本读取失败，不能据此断言为空。")).toBeVisible();
    expect(screen.getByText(/执行账本读取失败；保留其余已验证事实。/)).toBeVisible();
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
        HttpResponse.json({
          ok: true,
          data: tradingCasesFixture({ cases: [], reason_counts_24h: {}, state_counts_24h: {} }),
        }),
      ),
      http.get(/.*\/api\/trading\/executions$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingExecutionsFixture({ commands: [], executions: [] }),
        }),
      ),
    );
    renderTrading();

    // One vocabulary for every ledger on the page (#537 PR-5): one subject word, three sentences.
    expect(await screen.findByText("当前 24 小时窗口没有执行。")).toBeVisible();
    expect(screen.getByText("当前 24 小时窗口没有Command。")).toBeVisible();
    expect(screen.getByText("当前 24 小时窗口没有Case。")).toBeVisible();
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
              // A CLI manual entry: the console cannot issue one, and the ledger still names it.
              tradingCommandRowFixture({
                action: "manual_entry",
                command_id: "f".repeat(64),
                stage: "accepted",
              }),
            ],
          }),
        }),
      ),
    );
    renderTrading();

    const commands = (await screen.findByText("Runtime 拒绝")).closest(
      ".trading-command-list",
    ) as HTMLElement;
    expect(within(commands).getAllByText("Runtime 受理")).toHaveLength(2);
    expect(within(commands).getByText("已过期")).toBeVisible();
    expect(within(commands).getByText("手动方向")).toBeVisible();
  });
});

function renderTrading(entry = "/trading") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const router = { search: "" };
  function Probe() {
    router.search = useLocation().search;
    return null;
  }
  const utils = render(
    <MemoryRouter initialEntries={[entry]}>
      <QueryClientProvider client={client}>
        <TradingPage token="test-token" />
        <Probe />
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return { client, router, ...utils };
}
