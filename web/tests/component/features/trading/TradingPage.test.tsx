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
 * Six blocks over three reads, and a Case drawer that opens on demand (#604 T4).
 *
 * The tests below are mostly about the page not inventing anything: every stage word, disposition, count
 * and figure is a field the server already folded, and the two computations the browser is allowed are
 * `Date.now()` against the expiry instant `/status` publishes and the holding interval between the two
 * clocks the execution ledger stores. The other subject is failure: three reads, three failures, and no
 * one of them may blank a block another read answers.
 */
describe("TradingPage", () => {
  beforeEach(() => {
    vi.spyOn(Date, "now").mockReturnValue(TRADING_NOW_MS);
    server.use(
      http.get(/.*\/api\/trading\/status$/, () =>
        HttpResponse.json({ ok: true, data: tradingStatusFixture() }),
      ),
      http.get(/.*\/api\/trading\/cases$/, ({ request }) =>
        HttpResponse.json({ ok: true, data: casesFor(request.url) }),
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

  it("names the blocking reason in Chinese, in three safety words rather than four", async () => {
    /*
     * `FLAT` was the fourth. `ExecutionRuntimeState.account_flat` stays false with zero positions, so it
     * read `NOT PROVEN` around the clock — an always-amber quarter of the strip nobody could act on. The
     * proof is a sentence in the exposure block now, qualifying the empty position list it belongs to.
     */
    renderTrading();

    expect(await screen.findByRole("heading", { name: "交易执行" })).toBeVisible();
    const safety = screen.getByLabelText("执行安全状态");
    expect(within(safety).getAllByText("否")).toHaveLength(3);
    expect(within(safety).queryByText("NOT PROVEN")).toBeNull();
    expect(within(safety).getByText("执行通道未启用")).toBeVisible();
    expect(screen.getByText(/可执行市场 0 个/)).toBeVisible();
  });

  it("degrades every safety word once the server's own expiry instant has passed", async () => {
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
    expect(within(safety).getAllByText("过期")).toHaveLength(3);
    expect(within(safety).queryByText("是")).toBeNull();
    expect(screen.getByText(/本次读取的事实已过期/)).toBeVisible();
  });

  it("keeps the ledger readable when the readiness projection is the read that failed", async () => {
    /*
     * The one structural repair. `/api/trading/status` was read first and a cold error returned a single
     * error panel for the whole route, so a 5xx on the readiness projection took a perfectly readable
     * execution ledger with it. Three reads, three failures.
     */
    server.use(
      http.get(/.*\/api\/trading\/status$/, () =>
        HttpResponse.json({ ok: false, error: "status_unavailable" }, { status: 500 }),
      ),
    );
    renderTrading("/trading?tab=executions");

    expect(await screen.findByText("crypto:perp:BTC:USDT")).toBeVisible();
    expect(screen.getByRole("heading", { name: "执行记录 · 最近 24 小时" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "策略判定" }));
    expect(await screen.findByRole("heading", { name: "最近 24 小时 · 判定分布" })).toBeVisible();
    expect(screen.getByText(/执行状态账本读取失败；保留其余已验证事实。/)).toBeVisible();
    // The blocks that read `/status` say so in the same vocabulary rather than rendering a false answer.
    expect(screen.getByText("执行状态账本读取失败，不能据此断言为空。")).toBeVisible();
    expect(screen.queryByLabelText("执行安全状态")).toBeNull();
  });

  it("asks for a Case only once a reader opens the drawer", async () => {
    /*
     * The polled read carries three count dictionaries and no Cases (#604 T3). One `case_id` request is
     * made, once, when a reader clicks a Signal row — not up to 100 frozen Cases every 15 s to render at
     * most one of them.
     */
    const asked: string[] = [];
    server.use(
      http.get(/.*\/api\/trading\/cases$/, ({ request }) => {
        const caseId = new URL(request.url).searchParams.get("case_id");
        if (caseId) asked.push(caseId);
        return HttpResponse.json({ ok: true, data: casesFor(request.url) });
      }),
    );
    renderTrading("/trading?tab=executions");

    const row = (await screen.findByText("crypto:perp:BTC:USDT")).closest(
      ".trading-ledger-row",
    ) as HTMLElement;
    expect(asked).toEqual([]);

    fireEvent.click(within(row).getByRole("button", { name: "crypto:perp:BTC:USDT" }));
    await waitFor(() => expect(asked).toEqual(["case-btc"]));
  });

  it("opens the Case a Signal row authored in the drawer, keyed on the URL", async () => {
    const { router } = renderTrading("/trading?tab=executions");

    const row = (await screen.findByText("crypto:perp:BTC:USDT")).closest(
      ".trading-ledger-row",
    ) as HTMLElement;
    fireEvent.click(within(row).getByRole("button", { name: "crypto:perp:BTC:USDT" }));

    expect(await screen.findByRole("region", { name: "案例 HYPE" })).toBeVisible();
    expect(screen.getByRole("dialog", { name: "策略判定依据" })).toHaveTextContent("case-btc");
    expect(screen.getByText("whale_oi_ratio_bps")).toBeVisible();
    expect(screen.getByText("未通过")).toBeVisible();
    // #604 T3 removed `policy_config`: the evidence table's 阈值 column already prints those numbers.
    expect(screen.queryByRole("heading", { name: "冻结策略配置" })).toBeNull();
    expect(router.search).toBe("?tab=executions&case=case-btc");

    fireEvent.click(screen.getByRole("button", { name: "关闭" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "策略判定依据" })).toBeNull());
    expect(router.search).toBe("?tab=executions");
  });

  it("says a deep-linked Case is outside the window rather than showing nothing", async () => {
    renderTrading("/trading?case=case-gone");

    expect(await screen.findByRole("dialog", { name: "策略判定依据" })).toHaveTextContent(
      "未找到保留的策略判定。",
    );
    expect(screen.queryByRole("region", { name: /^案例 / })).toBeNull();
  });

  it("colours a realized result on the market axis and times the position from two clocks", async () => {
    renderTrading("/trading?tab=executions");

    const closed = await screen.findByText("crypto:perp:BTC:USDT");
    const row = closed.closest(".trading-ledger-row") as HTMLElement;
    expect(within(row).getByText("已平仓")).toBeVisible();
    expect(within(row).getByText("已受理")).toBeVisible();
    expect(within(row).getByText("0.049")).toBeVisible();
    expect(within(row).getByText("9699.0")).toBeVisible();
    expect(within(row).getByText("flatten 退出")).toBeVisible();
    // A loss is green and a profit red, exactly as `tokens.css` reads the two market directions.
    expect(within(row).getByText("−$14.92")).toHaveAttribute("data-tone", "loss");
    expect(within(row).getByText("持仓 1m33s")).toBeVisible();

    const manual = screen.getByText("crypto:perp:ETH:USDT").closest(".trading-ledger-row")!;
    expect(within(manual as HTMLElement).getByText("$1.12")).toHaveAttribute("data-tone", "profit");
    expect(within(manual as HTMLElement).getByText("持仓 57s")).toBeVisible();
    // The manual entry has no Case, so its market cell is a word rather than the button a Signal carries.
    expect(within(manual as HTMLElement).queryByRole("button")).toBeNull();
    expect(within(manual as HTMLElement).getByText(/SHORT · 手工/)).toBeVisible();
  });

  it("prints a dash for an entry that never filled, and the venue's own rejection words", async () => {
    renderTrading("/trading?tab=executions");

    const unmapped = (await screen.findByText("crypto:perp:NVDA:USDT")).closest(
      ".trading-ledger-row",
    ) as HTMLElement;
    expect(within(unmapped).getByText("运行时目录里没有这个市场")).toBeVisible();
    expect(within(unmapped).getByText("已拒绝")).toBeVisible();
    // Verbatim: it is the exchange talking, and translating it would put words in the venue's mouth.
    expect(within(unmapped).getByText("Order would immediately trigger.")).toBeVisible();
    expect(within(unmapped).getByText("持仓 —")).toBeVisible();

    // A Signal whose TTL ran out before the Runtime could act carries the server's own `expired` stage.
    const stale = screen.getByText("crypto:perp:SOL:USDT").closest(".trading-ledger-row")!;
    expect(within(stale as HTMLElement).getByText("已过期")).toBeVisible();
    expect(within(stale as HTMLElement).getByText("Signal 已过期")).toBeVisible();
  });

  it("states the realized totals the server summed, not the rows the desk happens to hold", async () => {
    renderTrading();

    const tally = (await screen.findByRole("heading", { name: "今日战况" })).closest(
      "section",
    ) as HTMLElement;
    expect(within(tally).getByText("今日已实现").nextSibling).toHaveTextContent("−$13.80");
    expect(within(tally).getByText("累计已实现").nextSibling).toHaveTextContent("$56.40");
    expect(within(tally).getByText("累计平仓 9 笔")).toBeVisible();
    // Four entries in the window, two of which the Runtime refused before any order reached the venue.
    expect(within(tally).getByText("今日入场").nextSibling).toHaveTextContent("4");
    expect(within(tally).getByText("受理 2 · 拒绝 2")).toBeVisible();
  });

  it("renders the funnel's reasons in Chinese rather than the keys the writer stores", async () => {
    /*
     * The Case card printed `smart_money_ratio_below_or_equal_floor`, so the seven translations in
     * `POLICY_RULE_ZH` could never reach a reader. The funnel's own top is `admission_counts_24h`, which is
     * the only account the desk can give of a frame that never became a Case at all.
     */
    renderTrading("/trading?tab=decisions");

    const funnel = (
      await screen.findByRole("heading", { name: "最近 24 小时 · 判定分布" })
    ).closest("section") as HTMLElement;
    const strip = within(funnel).getByLabelText("策略判定分布");
    expect(within(strip).getByText("不交易").nextSibling).toHaveTextContent("5");
    expect(within(strip).getByText("已发出信号").nextSibling).toHaveTextContent("1");
    expect(within(strip).getByText("判定受阻").nextSibling).toHaveTextContent("1");
    expect(within(funnel).getByText("鲸鱼占比未超过地板")).toBeVisible();
    fireEvent.click(within(funnel).getByText("来源准入分布 · 未成案的来源记录"));
    expect(within(funnel).getByText("准入拒绝 · 持仓价值低于地板")).toBeVisible();
    expect(within(funnel).getByText("过期 · 触发已陈旧")).toBeVisible();
    expect(within(funnel).queryByText("smart_money_ratio_below_or_equal_floor")).toBeNull();
  });

  it("opens the exposure block only when the account holds something", async () => {
    renderTrading();

    const closed = (await screen.findByRole("heading", { name: "当前仓位与保护" })).closest(
      "section",
    ) as HTMLElement;
    expect(closed.querySelector("details")).not.toHaveAttribute("open");
    // The summary is the whole block until a reader opens it; the facts are present and not rendered.
    expect(within(closed).getByText(/仓位 0 · 挂单 — · 保护 保护状态未知/)).toBeVisible();
    expect(within(closed).getByText("未见当前仓位；这本身不能证明账户为空。")).not.toBeVisible();

    cleanup();
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

    const open = (await screen.findByRole("heading", { name: "当前仓位与保护" })).closest(
      "section",
    ) as HTMLElement;
    expect(open.querySelector("details")).toHaveAttribute("open");
    expect(within(open).getByText("$997.50")).toBeVisible();
    expect(within(open).getByText("1,000 ms")).toBeVisible();
    // The audit tile was a constant `HEALTHY`; only the state a reader acts on renders now, as an alert.
    expect(within(open).getByText(/账户事实写入审计失败 · audit_append_failed/)).toBeVisible();
    expect(within(open).queryByText("HEALTHY")).toBeNull();
    expect(within(open).getAllByText("已受保护").length).toBeGreaterThan(0);
    expect(within(open).getAllByText("Trigger 9800")).toHaveLength(2);
  });

  it("writes a Command with no second confirmation and reads back the Runtime's own answer", async () => {
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
      http.get(/.*\/api\/trading\/executions$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingExecutionsFixture({
            commands: [
              tradingCommandRowFixture({
                action: "resume_entries",
                command_id: "d".repeat(64),
                reason: "daily_loss_limit",
                stage: "rejected",
              }),
              tradingCommandRowFixture({
                action: "flatten",
                command_id: "b".repeat(64),
                stage: "completed",
              }),
            ],
          }),
        }),
      ),
      http.post(/.*\/api\/trading\/execution\/commands$/, async ({ request }) => {
        authorization = request.headers.get("authorization");
        posted = await request.json();
        return HttpResponse.json({ ok: true, data: commandReceipt("a".repeat(64)) });
      }),
    );
    renderTrading();

    // A refusal with a reason on it: `disposition_reason` was selected and then discarded (#604 T3).
    const rejected = (await screen.findByText("Runtime 拒绝")).closest(
      ".trading-command-row",
    ) as HTMLElement;
    expect(within(rejected).getByText("daily_loss_limit")).toBeVisible();
    expect(screen.getByText("已完成 · 私有对账证明")).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "恢复新入场" }));
    expect(screen.queryByRole("alertdialog")).toBeNull();
    await waitFor(() => expect(posted).toBeDefined());
    expect(authorization).toBe("Bearer test-token");
    expect(posted).toMatchObject({
      request_id: "11111111-1111-4111-8111-111111111111",
      text: "/resume operator console",
    });
    expect(await screen.findByText(/操作已记录/)).toHaveTextContent(
      "这不代表执行器已受理、订单已完成或已经成交",
    );
  });

  it("reads no admission ledger and no Signal list", async () => {
    /*
     * #537 PR-5. The desk downloaded up to 400 `decisions[]` from `/api/trading/gate` every 15 s and
     * rendered none of the rows; `/api/trading/signals` is deleted outright. The funnel's frame count is a
     * server aggregate over the same admission ledger, which is a count read and not that row read.
     */
    const unexpected: string[] = [];
    server.use(
      http.get(/.*\/api\/trading\/(gate|signals).*/, ({ request }) => {
        unexpected.push(new URL(request.url).pathname);
        return HttpResponse.json({ ok: false, error: "unexpected" }, { status: 500 });
      }),
    );
    renderTrading("/trading?tab=executions");

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
    renderTrading("/trading?tab=executions");

    expect(await screen.findByText("执行账本读取失败，不能据此断言为空。")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "持仓与订单" }));
    expect(screen.getByText("Command账本读取失败，不能据此断言为空。")).toBeVisible();
    expect(screen.getByText(/执行账本读取失败；保留其余已验证事实。/)).toBeVisible();
    // The safety strip is a different read and keeps answering.
    expect(screen.getByLabelText("执行安全状态")).toBeVisible();
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
        return HttpResponse.json({ ok: true, data: commandReceipt("c".repeat(64)) });
      }),
    );
    renderTrading();

    fireEvent.click(await screen.findByRole("button", { name: "恢复新入场" }));
    expect(await screen.findByText(/提交结果未知/)).toHaveTextContent(
      "复用同一 request ID、时钟和文本",
    );

    fireEvent.click(screen.getByRole("button", { name: "恢复新入场" }));
    await waitFor(() => expect(bodies).toHaveLength(2));

    expect(uuidCalls).toBe(1);
    expect(bodies[1]).toEqual(bodies[0]);
  });

  it("locks every control while execution.mode is disabled", async () => {
    renderTrading();

    expect(await screen.findByText(/控制已锁定/)).toBeVisible();
    expect(screen.getByRole("button", { name: "暂停新入场" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "恢复新入场" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "平掉账户仓位" })).toBeDisabled();
  });

  it("says the window is empty rather than failed when the ledgers answer with nothing", async () => {
    server.use(
      http.get(/.*\/api\/trading\/cases$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingCasesFixture({
            admission_counts_24h: [],
            reason_counts_24h: {},
            state_counts_24h: {},
          }),
        }),
      ),
      http.get(/.*\/api\/trading\/executions$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingExecutionsFixture({ commands: [], executions: [] }),
        }),
      ),
    );
    renderTrading("/trading?tab=executions");

    // One vocabulary for every ledger on the page (#537 PR-5): one subject word, three sentences.
    expect(await screen.findByText("当前 24 小时窗口没有执行。")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "持仓与订单" }));
    expect(screen.getByText("当前 24 小时窗口没有Command。")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "策略判定" }));
    expect(await screen.findByText("当前筛选下没有策略判定。")).toBeVisible();
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
    renderTrading("/trading?tab=executions");

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
                reason: "runtime_stopped",
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
    expect(within(commands).getByText("runtime_stopped")).toBeVisible();
    // A Command the Runtime accepted has no refusal to explain.
    expect(within(commands).getAllByText("—")).toHaveLength(3);
  });
});

function casesFor(requestUrl: string) {
  const caseId = new URL(requestUrl).searchParams.get("case_id");
  const counts = tradingCasesFixture();
  if (!caseId) return counts;
  return {
    ...counts,
    cases: caseId === "case-gone" ? [] : [tradingCaseFixture({ case_id: caseId })],
  };
}

function commandReceipt(commandId: string) {
  return {
    command_id: commandId,
    disposition: "awaiting_runtime",
    reason: null,
    requested_at_ns: 1,
    seq: 7,
    truth: "intent_recorded_not_runtime_or_venue",
  };
}

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
