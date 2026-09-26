import { TradingPage } from "@features/trading";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import {
  TRADING_NOW_MS,
  tradingCaseFixture,
  tradingCasesFixture,
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
 * monotonic elapsed time against the server's remaining heartbeat budget and the holding interval
 * between the two clocks the execution ledger stores. The other subject is failure: three reads, three failures, and no
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

  it("names the blocking reason in Chinese, in two safety words", async () => {
    /*
     * `FLAT` went first, as an always-amber `NOT PROVEN`; `当前仓位可保护 / 退出` followed it with the
     * Runtime's private account proof it answered for (#680). Nautilus owns the execution state now, so
     * the strip asks only whether the Runtime is alive and whether it will take a new entry.
     */
    renderTrading();

    expect(await screen.findByRole("heading", { name: "交易执行" })).toBeVisible();
    const safety = screen.getByLabelText("执行安全状态");
    expect(within(safety).getAllByText("否")).toHaveLength(2);
    expect(within(safety).queryByText("NOT PROVEN")).toBeNull();
    expect(within(safety).queryByText("当前仓位可保护 / 退出")).toBeNull();
    expect(within(safety).getByText("执行通道未启用")).toBeVisible();
    expect(screen.getByText(/可执行市场 0 个/)).toBeVisible();
    expect(screen.getByText(/已配置连接：Binance USD-M · LIVE.*尚未连接/)).toBeVisible();
  });

  it("degrades every safety word once the server's own expiry instant has passed", async () => {
    server.use(
      http.get(/.*\/api\/trading\/status$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingStatusFixture({
            execution: tradingLiveExecutionFixture({
              entries_armed: true,
              entries_paused: false,
              entry_block_reason: null,
              facts_expire_at_ms: TRADING_NOW_MS - 1,
              facts_remaining_ms: 0,
            }),
          }),
        }),
      ),
    );
    renderTrading();

    const safety = await screen.findByLabelText("执行安全状态");
    expect(within(safety).getAllByText("待确认")).toHaveLength(2);
    expect(within(safety).queryByText("是")).toBeNull();
    expect(screen.getByText(/状态通道失联：未取得有效期内的新状态/)).toBeVisible();
    expect(
      screen.getByText(/Binance USD-M · DEMO.*最后报告.*状态过期，连接状态未知/),
    ).toBeVisible();
  });

  it("shows the last Runtime connection when configuration has changed", async () => {
    server.use(
      http.get(/.*\/api\/trading\/status$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingStatusFixture({
            execution: tradingLiveExecutionFixture({ configured_connection: "LIVE" }),
          }),
        }),
      ),
    );
    renderTrading();
    expect(await screen.findByText(/Binance USD-M · DEMO.*配置待重启：LIVE/)).toBeVisible();
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
    expect(
      screen.getByRole("heading", { name: "执行记录 · 近 24 小时及未结束交易" }),
    ).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "策略判定" }));
    expect(
      await screen.findByRole("heading", { name: "一条市场线索，如何走到交易" }),
    ).toBeVisible();
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
    expect(within(row).getByText("交易所已受理")).toBeVisible();
    expect(within(row).getByText("0.049")).toBeVisible();
    expect(within(row).getByText("9699.0")).toBeVisible();
    expect(within(row).getByText("止盈价 10200")).toBeVisible();
    expect(within(row).getByText("操作员平仓")).toBeVisible();
    // A loss is green and a profit red, exactly as `tokens.css` reads the two market directions.
    expect(within(row).getByText("−$14.92")).toHaveAttribute("data-tone", "loss");
    expect(within(row).getByText("持仓 1m33s")).toBeVisible();
    // The realized number is already net of commissions; the fees the fill journal charged sit under it.
    expect(within(row).getByText("手续费 $0.17")).toBeVisible();

    const manual = screen.getByText("crypto:perp:ETH:USDT").closest(".trading-ledger-row")!;
    expect(within(manual as HTMLElement).getByText("$1.12")).toHaveAttribute("data-tone", "profit");
    expect(within(manual as HTMLElement).getByText("持仓 57s")).toBeVisible();
    // The manual entry has no Case, so its market cell is a word rather than the button a Signal carries.
    expect(within(manual as HTMLElement).queryByRole("button")).toBeNull();
    expect(within(manual as HTMLElement).getByText(/SHORT · 手工/)).toBeVisible();
  });

  it("keeps all-missing PnL unknown, says how many closes it lacks, and names the frozen exit policy", async () => {
    const base = tradingExecutionsFixture();
    server.use(
      http.get(/.*\/api\/trading\/executions$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingExecutionsFixture({
            totals: {
              ...base.totals,
              realized_known_today_usd: null,
              realized_known_total_usd: null,
              closed_today: 3,
              closed_total: 3,
              pnl_known_today: 0,
              pnl_known_total: 0,
              pnl_missing_today: 3,
              pnl_missing_total: 3,
              net_known_today_usd: null,
              net_known_total_usd: null,
              net_known_today: 0,
              net_known_total: 0,
              net_missing_today: 3,
              net_missing_total: 3,
            },
            executions: [
              /*
               * Closed, but one fill carries no quote-currency commission, so the fill journal cannot
               * yield a net number and the server publishes neither the result nor the fees (#680).
               */
              tradingExecutionRowFixture({
                fees_usd: null,
                realized_pnl_usd: null,
                pnl_known: false,
              }),
            ],
          }),
        }),
      ),
    );
    renderTrading();
    const tally = await screen.findByRole("heading", { name: "今日战况" });
    const card = tally.closest("[data-block]") as HTMLElement;
    expect(within(card).getByText("今日已知净收益").nextSibling).toHaveTextContent("—");
    expect(within(card).getAllByText("平仓 3 · 已知 0 · 缺失 3")).toHaveLength(2);
    expect(
      within(card).getByText(
        /^3 笔已平仓交易缺少完整成交、手续费或资金费归因.*不能视为账户完整净利润/,
      ),
    ).toBeVisible();
    expect(within(card).getByText(/净收益需场所资金费完整覆盖/)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "执行记录" }));
    expect(await screen.findByText("净收益未知")).toBeVisible();
    expect(screen.queryByText(/^手续费 /)).toBeNull();
    expect(screen.getByText("冻结止损 200 bps")).toBeVisible();
    expect(screen.getByText(/风险预算.*10.00.*2×/)).toBeVisible();
    expect(screen.getByText(/止盈 200 bps/)).toBeVisible();
  });

  it("says nothing is missing when every closed plan has a result", async () => {
    renderTrading();

    const tally = (await screen.findByRole("heading", { name: "今日战况" })).closest(
      "[data-block]",
    ) as HTMLElement;
    expect(within(tally).getByText("平仓 9 · 已知 9 · 缺失 0")).toBeVisible();
    expect(within(tally).queryByText(/不能视为账户完整净利润/)).toBeNull();
  });

  it("prints a dash for an entry that never filled, and the venue's own rejection words", async () => {
    renderTrading("/trading?tab=executions");

    const refused = (await screen.findByText("crypto:perp:NVDA:USDT")).closest(
      ".trading-ledger-row",
    ) as HTMLElement;
    expect(within(refused).getByText("交易所拒绝入场")).toBeVisible();
    // A plan that ended as `not_submitted` is a rejection, never a closed trade (#680).
    expect(within(refused).getByText("已拒绝")).toBeVisible();
    expect(within(refused).queryByText("已平仓")).toBeNull();
    expect(within(refused).getByText("入场被拒，计划终止")).toBeVisible();
    // Verbatim: it is the exchange talking, and translating it would put words in the venue's mouth.
    expect(within(refused).getByText("Order would immediately trigger.")).toBeVisible();
    // The plan has a terminal clock but no fill, so there is no holding interval and no result.
    expect(within(refused).getByText("持仓 —")).toBeVisible();
    expect(within(refused).queryByText("盈亏未知")).toBeNull();

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
    expect(within(tally).getByText("今日已知净收益").nextSibling).toHaveTextContent("−$13.80");
    expect(within(tally).getByText("累计已知净收益").nextSibling).toHaveTextContent("$56.40");
    expect(within(tally).getByText("平仓 9 · 已知 9 · 缺失 0")).toBeVisible();
    // Four entries in the window, two of which never opened anything: one the venue refused, one expired.
    expect(within(tally).getByText("所列入场").nextSibling).toHaveTextContent("4");
    expect(within(tally).getByText("受理 2 · 拒绝 2")).toBeVisible();
  });

  it("shows signed venue net separately from the fee-only fill result", async () => {
    const base = tradingExecutionsFixture();
    server.use(
      http.get(/.*\/api\/trading\/executions$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingExecutionsFixture({
            executions: [
              tradingExecutionRowFixture({
                funding_usd: "0.11",
                net_pnl_usd: "-14.81274518",
              }),
            ],
            totals: {
              ...base.totals,
              net_known_today: 1,
              net_known_total: 1,
              net_known_today_usd: "-14.81274518",
              net_known_total_usd: "-14.81274518",
            },
          }),
        }),
      ),
    );
    renderTrading();
    const tally = (await screen.findByRole("heading", { name: "今日战况" })).closest("section")!;
    expect(within(tally as HTMLElement).getByText("今日已知净收益")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "执行记录" }));
    const row = (await screen.findByText("crypto:perp:BTC:USDT")).closest(".trading-ledger-row")!;
    expect(within(row as HTMLElement).getByText("手续费后 −$14.92")).toBeVisible();
    expect(within(row as HTMLElement).getByText("资金费 $0.11")).toBeVisible();
    expect(within(row as HTMLElement).getByText("−$14.81")).toBeVisible();
  });

  it("shows Agent outcomes and distinguishes unpublished judgments from published Signals", async () => {
    renderTrading("/trading?tab=decisions");

    const funnel = (
      await screen.findByRole("heading", { name: "一条市场线索，如何走到交易" })
    ).closest("section") as HTMLElement;
    expect(within(funnel).getByText("交易 3 · 观察 1 · 不交易 3")).toBeVisible();
    expect(within(funnel).getByText("未发布 2 · 阻断或失效 0")).toBeVisible();
    expect(within(funnel).getByText("TRADE 判断已发布").previousSibling).toHaveTextContent("1");
    expect(within(funnel).queryByText("smart_money_ratio_below_or_equal_floor")).toBeNull();
  });

  it("opens on completed Agent Cases and labels unpublished decisions without claiming a Signal", async () => {
    const listStates: string[] = [];
    server.use(
      http.get(/.*\/api\/trading\/cases$/, ({ request }) => {
        const url = new URL(request.url);
        if (url.searchParams.get("view") === "list") {
          listStates.push(url.searchParams.get("state") ?? "ALL");
          return HttpResponse.json({
            ok: true,
            data: tradingCasesFixture({
              cases: [
                tradingCaseFixture({
                  analysis_action: "TRADE",
                  analysis_publish_status: "unpublished",
                  analysis_side: "short",
                  base_symbol: "SOL",
                  case_id: "case-agent",
                  state: "DONE",
                  trigger_id: "a".repeat(64),
                  trigger_kind: "oi",
                }),
              ],
              total: 1,
            }),
          });
        }
        return HttpResponse.json({ ok: true, data: tradingCasesFixture() });
      }),
    );
    renderTrading("/trading?tab=decisions");

    expect(await screen.findByText("未发布判断 · 做空")).toBeVisible();
    expect(screen.getByText("OI 触发")).toBeVisible();
    expect(screen.queryByText("Signal 已发布")).toBeNull();
    expect(listStates).toContain("DONE");
  });

  it("opens the exposure block only when the account holds something", async () => {
    renderTrading();

    const closed = (await screen.findByRole("heading", { name: "当前仓位与保护" })).closest(
      "section",
    ) as HTMLElement;
    expect(closed.querySelector("details")).not.toHaveAttribute("open");
    // The summary is the whole block until a reader opens it; the facts are present and not rendered.
    expect(within(closed).getByText(/仓位 — · 挂单 — · 保护 无需保护/)).toBeVisible();
    expect(within(closed).getByText("未取得 Runtime 账户快照")).toBeVisible();
    expect(
      within(closed).getByText("未取得 Runtime 账户快照，不能据此断言没有仓位。"),
    ).not.toBeVisible();

    cleanup();
    server.use(
      http.get(/.*\/api\/trading\/status$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingStatusFixture({ execution: tradingLiveExecutionFixture() }),
        }),
      ),
    );
    renderTrading();

    const open = (await screen.findByRole("heading", { name: "当前仓位与保护" })).closest(
      "section",
    ) as HTMLElement;
    expect(open.querySelector("details")).toHaveAttribute("open");
    expect(within(open).getByText(/仓位 1 · 挂单 2 · 保护 已受保护/)).toBeVisible();
    expect(within(open).getByText("$997.50")).toBeVisible();
    expect(within(open).getByText("在途订单").nextSibling).toHaveTextContent("0");
    // The Runtime's private proof went with #680: no reconciliation age, no aggregate risk, no audit.
    for (const gone of ["私有对账距今", "总风险金额", "处理中 / 未知订单"]) {
      expect(within(open).queryByText(gone)).toBeNull();
    }
    expect(within(open).queryByText(/审计/)).toBeNull();
    // A position is protected when its stop and its take-profit both rest on the venue.
    const position = within(open).getByText("BTCUSDT-PERP.BINANCE", {
      selector: ".trading-position-identity b",
    });
    const strip = position
      .closest(".trading-position-row")!
      .querySelector(".trading-protection-strip") as HTMLElement;
    expect(strip).toHaveAttribute("data-tone", "protected");
    expect(within(strip).getByText("已受保护")).toBeVisible();
    expect(within(strip).getByText("止损 9800")).toBeVisible();
    expect(within(strip).getByText("止盈 10200")).toBeVisible();
    // Each resting order names its leg in the desk's own words.
    expect(within(open).getByText("止损 · Qty 0.05")).toBeVisible();
    expect(within(open).getByText("止盈 · Qty 0.05")).toBeVisible();
    expect(within(open).getByText("Trigger 9800")).toBeVisible();
    expect(within(open).getByText("Trigger 10200")).toBeVisible();
    expect(within(open).queryByText("无计划认领")).toBeNull();
  });

  it("keeps the last account and risk evidence historical when checks fail under a fresh heartbeat", async () => {
    server.use(
      http.get(/.*\/api\/trading\/status$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingStatusFixture({
            execution: tradingLiveExecutionFixture({
              account_projection_failure: "ValueError",
              convergence_failure: "RuntimeError",
              entry_block_reason: "convergence_unverified",
              unexpected_exposure: true,
            }),
          }),
        }),
      ),
    );
    renderTrading();

    const block = (await screen.findByRole("heading", { name: "当前仓位与保护" })).closest(
      "section",
    ) as HTMLElement;
    expect(within(block).getByText(/仓位 1 · 挂单 2 · 保护 待确认/)).toBeVisible();
    expect(within(block).getByText("上次观察的保护；当前未确认")).toBeVisible();
    expect(within(block).getByText("上次采样字段完整")).toBeVisible();
    expect(within(block).getByText("上次检查发现异常；最新检查未取得。")).toBeVisible();
    expect(block.querySelector(".trading-protection-strip")).toHaveAttribute(
      "data-tone",
      "caution",
    );
  });

  it("names a position without a take-profit unprotected and exposure no plan claims", async () => {
    server.use(
      http.get(/.*\/api\/trading\/status$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingStatusFixture({
            execution: tradingLiveExecutionFixture({
              current_account: tradingCurrentAccountFixture({
                open_orders_count: 1,
                orders: [
                  {
                    client_order_id: "stop-order-1",
                    instrument_id: "BTCUSDT-PERP.BINANCE",
                    leg: "stop",
                    owned: true,
                    quantity: "0.05",
                    reduce_only: true,
                    state: "open",
                    trigger_price: "9800",
                  },
                ],
                findings: [
                  {
                    kind: "unclaimed_position",
                    object_id: "position-2",
                    instrument_id: "SOLUSDT-PERP.BINANCE",
                    plan_entry_id: null,
                    cache_quantity: "-1",
                    venue_quantity: null,
                    observed_at_ms: TRADING_NOW_MS,
                  },
                ],
                positions: [
                  {
                    ...tradingCurrentAccountFixture().positions![0]!,
                    protection_status: "unprotected",
                    take_profit_trigger_price: null,
                  },
                  {
                    entry_price: "150",
                    instrument_id: "SOLUSDT-PERP.BINANCE",
                    mark_price: "151",
                    owned: false,
                    plan_entry_id: null,
                    protection_status: "unprotected",
                    source: "cache",
                    position_id: "position-2",
                    quantity: "1",
                    side: "short",
                    stop_trigger_price: null,
                    take_profit_trigger_price: null,
                    unrealized_pnl_usd: "-1",
                  },
                ],
              }),
              entry_block_reason: "unexpected_exposure",
              protection_status: "unprotected",
              unexpected_exposure: true,
            }),
          }),
        }),
      ),
    );
    renderTrading();

    const block = (await screen.findByRole("heading", { name: "当前仓位与保护" })).closest(
      "section",
    ) as HTMLElement;
    expect(within(block).getByText(/仓位 2 · 挂单 1 · 保护 未受保护/)).toBeVisible();
    expect(within(block).getByText(/最近检查发现异常；新增仓位受阻。/)).toBeVisible();
    expect(
      within(screen.getByLabelText("执行安全状态")).getByText("账户检查发现异常"),
    ).toBeVisible();

    const strips = Array.from(block.querySelectorAll<HTMLElement>(".trading-protection-strip"));
    expect(strips).toHaveLength(2);
    // A stop alone is not protection: the take-profit is half of what the plan rests on the venue.
    expect(strips[0]).toHaveAttribute("data-tone", "caution");
    expect(within(strips[0]!).getByText("未受保护")).toBeVisible();
    expect(within(strips[0]!).getByText("止损 9800")).toBeVisible();
    expect(within(strips[0]!).getByText("止盈 未挂")).toBeVisible();
    expect(within(strips[1]!).getByText("止损 未挂")).toBeVisible();
    const unclaimed = within(block)
      .getByText("SOLUSDT-PERP.BINANCE", { selector: ".trading-position-identity b" })
      .closest(".trading-position-row") as HTMLElement;
    expect(within(unclaimed).getByText("计划关联待核实")).toBeVisible();
    expect(within(unclaimed).getByText("空仓")).toBeVisible();
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
    expect(screen.getByText(/执行账本读取失败；保留其余已验证事实。/)).toBeVisible();
    // The safety strip is a different read and keeps answering.
    expect(screen.getByLabelText("执行安全状态")).toBeVisible();
  });

  it("says the window is empty rather than failed when the ledgers answer with nothing", async () => {
    server.use(
      http.get(/.*\/api\/trading\/cases$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingCasesFixture({
            admission_counts_24h: [],
            state_counts_24h: {},
          }),
        }),
      ),
      http.get(/.*\/api\/trading\/executions$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingExecutionsFixture({ executions: [] }),
        }),
      ),
    );
    renderTrading("/trading?tab=executions");

    // One vocabulary for every ledger on the page (#537 PR-5): one subject word, three sentences.
    expect(await screen.findByText("当前 24 小时窗口没有执行。")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "持仓与订单" }));
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
