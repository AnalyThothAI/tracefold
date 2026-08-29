import { NewsPage } from "@features/news";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import {
  NEWS_NOW_MS,
  newsFeedFixture,
  newsOiFrameFixture,
  newsQuoteFixture,
  newsReactionFixture,
  newsStatusFixture,
} from "@tests/fixtures/newsFixture";
import {
  gateEvidence,
  tradingGateDecisionFixture,
  tradingGateFixture,
  tradingIntentsFixture,
  tradingStatusFixture,
} from "@tests/fixtures/tradingFixture";
import { server } from "@tests/msw/server";
import { HttpResponse, http } from "msw";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

/**
 * OI 来源与准入审计 (#207/#137/#256). The lane is judged by rule rather than by the model, and the whole point of this
 * page is that the reader can see *which* rule — so the tests here are mostly about the page not inventing
 * anything the server did not say.
 */
describe("NewsOiPage", () => {
  beforeEach(() => {
    server.use(
      http.get(/.*\/api\/news\/status$/, () =>
        HttpResponse.json({ ok: true, data: newsStatusFixture() }),
      ),
      http.get(/.*\/api\/news\/feed$/, () =>
        HttpResponse.json({ ok: true, data: newsFeedFixture({ events: [newsOiFrameFixture()] }) }),
      ),
      http.get(/.*\/api\/news\/quotes$/, () =>
        HttpResponse.json({ ok: true, data: { measured_at_ms: NEWS_NOW_MS, quotes: [] } }),
      ),
      http.get(/.*\/api\/trading\/intents$/, () =>
        HttpResponse.json({ ok: true, data: tradingIntentsFixture() }),
      ),
      // #269: the admission ledger the capital column reads for the frames that authored no case, and
      // the rules those rows are filed under — the Candidate Gate's own, not the settings document.
      http.get(/.*\/api\/trading\/gate$/, () =>
        HttpResponse.json({ ok: true, data: tradingGateFixture() }),
      ),
      http.get(/.*\/api\/trading\/status$/, () =>
        HttpResponse.json({ ok: true, data: tradingStatusFixture() }),
      ),
    );
  });

  afterEach(cleanup);

  it("asks only for the deterministic telemetry lane over a 24 h window", async () => {
    const observed: Record<string, string | null> = {};
    server.use(
      http.get(/.*\/api\/news\/feed$/, ({ request }) => {
        const params = new URL(request.url).searchParams;
        for (const name of ["admission", "hours", "limit", "oi"]) observed[name] = params.get(name);
        return HttpResponse.json({
          ok: true,
          data: newsFeedFixture({ events: [newsOiFrameFixture()] }),
        });
      }),
    );

    renderOi();
    await screen.findByRole("heading", { name: "OI 来源与准入审计" });

    await waitFor(() => expect(observed.admission).toBe("telemetry_deterministic"));
    expect(observed.hours).toBe("24");
    // `all` is sent even though it narrows nothing: it is how the request identifies itself as the monitor,
    // which is what lets the server skip the outcome-group aggregate this page never reads.
    expect(observed.oi).toBe("all");
  });

  it("takes every tab count from the server's 24 h aggregate, never from the page", async () => {
    // The tab filters the whole window server-side while the table shows one page of it. A count derived
    // from the rows below would make an empty page read as an empty window.
    renderOi();
    await screen.findByRole("heading", { name: "OI 来源与准入审计" });

    const tabs = await screen.findByRole("tablist", { name: "按判定筛选" });
    expect(within(tabs).getByRole("tab", { name: /已推送/ })).toHaveTextContent("3");
    // 未达阈值 is the three threshold rules summed: 106 + 30 + 0.
    expect(within(tabs).getByRole("tab", { name: /未达阈值/ })).toHaveTextContent("136");
    expect(within(tabs).getByRole("tab", { name: /解析失败/ })).toHaveTextContent("1");
    /*
     * 全部 is `telemetry_events_24h` (141) — Events on this admission, which is exactly the row universe.
     * It is neither of the numbers beside it: `telemetry_received_24h` (142) counts provider items *before*
     * the Gate, so it names frames no row can show; the judged buckets sum to 140, so they miss the frame
     * still awaiting a verdict that the table renders with no `oi` block.
     */
    expect(within(tabs).getByRole("tab", { name: /全部/ })).toHaveTextContent("141");
    expect(screen.getByText("遥测帧 · 24h").closest(".ui-metric")).toHaveTextContent("142");
    expect(screen.getByText("解析成功 97.9%")).toBeInTheDocument();
  });

  it("names the admission version it read and never an Alpha threshold", async () => {
    /*
     * #331: the panel shows the *admission* rules the ledger's rows are filed under. It shows no Alpha
     * threshold at all, because those are frozen onto each Case and belong beside the Case that executed
     * them — a panel printing today's configuration invited a reader to measure last week's Case with it.
     */
    renderOi();
    expect(
      await screen.findByText(/SOURCE_NATIVE · trading_admission_v4 · Alpha 阈值随案例冻结/),
    ).toBeInTheDocument();
    expect(screen.getByText(/binance\.usdm → BINANCE_USDM/)).toBeInTheDocument();
    expect(screen.queryByText(/min_whale_long_profit/)).toBeNull();
  });

  it("says the admission rules were unread rather than printing them as absent", async () => {
    cleanup();
    server.use(
      http.get(/.*\/api\/trading\/gate$/, () => HttpResponse.json({ ok: false }, { status: 503 })),
    );
    renderOi();
    expect(await screen.findByText("SOURCE_NATIVE · 准入规则未读到")).toBeInTheDocument();
    expect(await screen.findByText(/准入台账读取失败/)).toBeInTheDocument();
  });

  it("marks a frame no threshold let through as having spent no window slot", async () => {
    // `evaluate_oi` computes a rank for every frame and the trace records it, so a whale-ratio rejection
    // still carries `eligible_rank_in_window: 1`. That is the rank it *would* have taken; printing "1 / 2"
    // beside it would say the window is fuller than it is.
    server.use(
      http.get(/.*\/api\/news\/feed$/, () =>
        HttpResponse.json({
          ok: true,
          data: newsFeedFixture({
            events: [
              newsOiFrameFixture({
                oi: {
                  ...newsOiFrameFixture().oi!,
                  eligible_rank_in_window: 1,
                  rule: "whale_ratio_below_threshold",
                  whale_oi_ratio_bps: 5_420,
                },
              }),
            ],
          }),
        }),
      ),
    );

    renderOi();
    const row = await screen.findByRole("button", { name: /WIF/ });
    expect(row).toHaveTextContent("whale_ratio_below_threshold");
    expect(row).not.toHaveTextContent("1 / 2");
  });

  it("stamps the research bucket from the measurement, not from a lane threshold", async () => {
    /*
     * #331: the 95% boundary is `oi-agent-design-2026-08-22.md` §1.5's measured bucket, not the capital
     * lane's floor. It used to be read off the lane's republished settings, so a console newer than the
     * API got the schema's zero and badged every frame as 研究里唯一均值为正的分桶.
     */
    server.use(
      http.get(/.*\/api\/news\/feed$/, () =>
        HttpResponse.json({
          ok: true,
          data: newsFeedFixture({
            events: [
              newsOiFrameFixture({
                oi: { ...newsOiFrameFixture().oi!, whale_long_profit_bps: 9_600 },
              }),
            ],
          }),
        }),
      ),
    );

    renderOi();
    expect(await screen.findByText("盈利正桶")).toBeInTheDocument();
  });

  it("keeps the tabs reachable when the frame request fails", async () => {
    // The rows are what failed, not the navigation. A reader stuck on a 5xx'd tab must be able to click
    // back to 全部 without editing the URL.
    server.use(
      http.get(/.*\/api\/news\/feed$/, () => HttpResponse.json({ ok: false }, { status: 500 })),
    );

    renderOi("/news/oi?oi=withheld");
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    const tabs = screen.getByRole("tablist", { name: "按判定筛选" });
    expect(within(tabs).getByRole("tab", { name: /全部/ })).toBeInTheDocument();
    expect(within(tabs).getByRole("tab", { name: /未达阈值/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("offers an explicit page action while the window holds more frames than one page", async () => {
    server.use(
      http.get(/.*\/api\/news\/feed$/, ({ request }) => {
        const cursor = new URL(request.url).searchParams.get("cursor");
        return HttpResponse.json({
          ok: true,
          data: newsFeedFixture({
            events: [
              newsOiFrameFixture(
                cursor ? { event_id: "evt-oi-page2", leader_title: "S OI Rise 1%" } : {},
              ),
            ],
            next_cursor: cursor ? null : "cursor-2",
          }),
        });
      }),
    );

    renderOi();
    const more = await screen.findByRole("button", { name: "加载更多帧" });
    // The tab count is the whole 24 h window; the note says how much of it is on screen.
    expect(screen.getByText(/已加载 1 条/)).toBeInTheDocument();

    fireEvent.click(more);
    await waitFor(() =>
      expect(screen.getAllByRole("button", { name: /OI|WIF|S/ }).length).toBeGreaterThan(1),
    );
    expect(screen.queryByRole("button", { name: "加载更多帧" })).not.toBeInTheDocument();
  });

  it("puts the chosen tab in the URL and forwards it as the server's own rule group", async () => {
    const observed: string[] = [];
    server.use(
      http.get(/.*\/api\/news\/feed$/, ({ request }) => {
        observed.push(new URL(request.url).searchParams.get("oi") ?? "");
        return HttpResponse.json({ ok: true, data: newsFeedFixture({ events: [] }) });
      }),
    );

    renderOi();
    fireEvent.click(await screen.findByRole("tab", { name: /解析失败/ }));

    await waitFor(() => expect(observed).toContain("parse_failed"));
    expect(screen.getByTestId("location").textContent).toBe("/news/oi?oi=parse_failed");
  });

  it("reads the row subject from the judge's trace rather than reparsing the title", async () => {
    /*
     * `oi.symbol` is the structured parser result. Public `assets` normally carries the later durable market
     * projection, but it is not the parser contract; even when that projection is absent the page must use the
     * stored OI subject and must never infer a replacement from `leader_title`.
     */
    server.use(
      http.get(/.*\/api\/news\/feed$/, () =>
        HttpResponse.json({
          ok: true,
          data: newsFeedFixture({
            events: [
              newsOiFrameFixture({
                assets: [],
                grounded_assets: [],
                oi: { ...newsOiFrameFixture().oi!, symbol: "HOLO" },
              }),
            ],
          }),
        }),
      ),
    );

    renderOi();
    await screen.findByRole("heading", { name: "OI 来源与准入审计" });
    await waitFor(() =>
      expect(document.querySelector(".news-oi-symbol b")?.textContent).toBe("HOLO"),
    );
  });

  it("says nothing for the symbol of a frame that never parsed", async () => {
    // `oi_parse_failure` records no symbol, because the template never matched and nothing was read out of
    // the title. `—` is the honest cell; the provider's own line is in the expansion.
    server.use(
      http.get(/.*\/api\/news\/feed$/, () =>
        HttpResponse.json({
          ok: true,
          data: newsFeedFixture({
            events: [
              newsOiFrameFixture({
                assets: [],
                grounded_assets: [],
                oi: {
                  ...newsOiFrameFixture().oi!,
                  parsed: false,
                  rule: "oi_parse_failed",
                  symbol: null,
                },
              }),
            ],
          }),
        }),
      ),
    );

    renderOi();
    await screen.findByRole("heading", { name: "OI 来源与准入审计" });
    await waitFor(() => expect(document.querySelector(".news-oi-symbol b")?.textContent).toBe("—"));
  });

  it("renders the judged measurements from the server's own trace", async () => {
    renderOi();
    const row = await screen.findByRole("button", { name: /WIF/ });

    // The four measurements, the rank and the gate — all `oi_judgment_trace()` fields read back.
    expect(row).toHaveTextContent("+6.71%");
    expect(row).toHaveTextContent("1103 万");
    expect(row).toHaveTextContent("143.90%");
    expect(row).toHaveTextContent("88.40%");
    expect(row).toHaveTextContent("1 / 2");
    expect(row).toHaveTextContent("opening_move_with_whale_concentration");
    // 1H/4H is the fixed post-Event measurement, signed and in percent.
    expect(row).toHaveTextContent("+2.03%");
    expect(row).toHaveTextContent("+1.45%");
    // The price is the fixed Event mark, never a current quote.
    expect(row).toHaveTextContent("0.8412");
    // The one batched trading read joins this row by its published Event identity.
    expect(row).toHaveTextContent("未评估");
  });

  it("shows one batched current quote beside the fixed Event anchor price", async () => {
    const quoteRequests: string[] = [];
    server.use(
      http.get(/.*\/api\/news\/feed$/, () =>
        HttpResponse.json({
          ok: true,
          data: newsFeedFixture({
            events: [
              newsOiFrameFixture({
                assets: [
                  { base_symbol: "WIF", listed: true, symbol: "WIF", venue: "binance.perp" },
                ],
              }),
              newsOiFrameFixture({
                assets: [
                  { base_symbol: "BTR", listed: true, symbol: "BTR", venue: "binance.perp" },
                ],
                event_id: "evt-oi-btr",
                oi: { ...newsOiFrameFixture().oi!, symbol: "BTR" },
                reaction: newsReactionFixture({ p0: "0.1600" }),
              }),
            ],
          }),
        }),
      ),
      http.get(/.*\/api\/news\/quotes$/, ({ request }) => {
        quoteRequests.push(new URL(request.url).searchParams.get("symbols") ?? "");
        return HttpResponse.json({
          ok: true,
          data: {
            measured_at_ms: NEWS_NOW_MS,
            quotes: [
              newsQuoteFixture({
                base_symbol: "BTR",
                price: "0.1700",
                requested_symbol: "BTR",
                symbol: "BTR",
                venue_symbol: "BTRUSDT",
              }),
              newsQuoteFixture({
                base_symbol: "WIF",
                price: "0.9245",
                requested_symbol: "WIF",
                symbol: "WIF",
                venue_symbol: "WIFUSDT",
              }),
            ],
          },
        });
      }),
    );

    renderOi();
    const row = await screen.findByRole("button", { name: /WIF/ });
    const btr = await screen.findByRole("button", { name: /BTR/ });
    await waitFor(() => expect(quoteRequests).toEqual(["BTR,WIF"]));
    expect(screen.getByText("现价")).toBeInTheDocument();
    expect(screen.getByText("帧时价")).toBeInTheDocument();
    expect(row).toHaveTextContent("0.9245");
    expect(row).toHaveTextContent("0.8412");
    expect(row.querySelector(".news-oi-price")).toHaveAttribute(
      "title",
      "Event anchor 的 5 分钟 K 线收盘价（p0），不是现价",
    );
    expect(btr).toHaveTextContent("0.17");
    expect(btr).toHaveTextContent("0.16");
  });

  it("keeps current price visible while an immature p0 says it awaits the 1H fill", async () => {
    server.use(
      http.get(/.*\/api\/news\/feed$/, () =>
        HttpResponse.json({
          ok: true,
          data: newsFeedFixture({
            events: [
              newsOiFrameFixture({
                assets: [
                  { base_symbol: "WIF", listed: true, symbol: "WIF", venue: "binance.perp" },
                ],
                reaction: newsReactionFixture({
                  p0: null,
                  priced_n: 0,
                  return_1h_bps: null,
                  return_4h_bps: null,
                  state: "pending",
                }),
              }),
            ],
          }),
        }),
      ),
      http.get(/.*\/api\/news\/quotes$/, () =>
        HttpResponse.json({
          ok: true,
          data: {
            measured_at_ms: NEWS_NOW_MS,
            quotes: [
              newsQuoteFixture({
                base_symbol: "WIF",
                price: "0.9245",
                requested_symbol: "WIF",
                symbol: "WIF",
                venue_symbol: "WIFUSDT",
              }),
            ],
          },
        }),
      ),
    );

    renderOi();
    const row = await screen.findByRole("button", { name: /WIF/ });
    expect(await within(row).findByText("0.9245")).toBeInTheDocument();
    expect(row).toHaveTextContent("待 1H 回填");
    expect(row).not.toHaveTextContent("0.00%");
  });

  it("keeps the stored Reaction visible when the independent current-quote read fails", async () => {
    let quoteRequests = 0;
    server.use(
      http.get(/.*\/api\/news\/quotes$/, () => {
        quoteRequests += 1;
        return HttpResponse.json({ ok: false, error: "quote unavailable" }, { status: 503 });
      }),
    );

    renderOi();
    const row = await screen.findByRole("button", { name: /WIF/ });
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("行情读取失败");
    await waitFor(() =>
      expect(document.querySelector(".news-oi-current-price")).toHaveTextContent("—"),
    );
    expect(row).toHaveTextContent("0.8412");
    expect(row).toHaveTextContent("+2.03%");
    expect(row).toHaveTextContent("+1.45%");
    fireEvent.click(within(alert).getByRole("button", { name: "重试" }));
    await waitFor(() => expect(quoteRequests).toBeGreaterThanOrEqual(2));
  });

  it("marks the research bucket a frame falls in against the capital lane's own floor", async () => {
    renderOi();
    // 88.40% is below the 95% whale-profit floor, and 11.03M lands in the worst open-interest bucket.
    const row = await screen.findByRole("button", { name: /WIF/ });
    expect(row).toHaveTextContent("持仓最差桶");
    expect(row).not.toHaveTextContent("盈利正桶");

    cleanup();
    server.use(
      http.get(/.*\/api\/news\/feed$/, () =>
        HttpResponse.json({
          ok: true,
          data: newsFeedFixture({
            events: [
              newsOiFrameFixture({
                oi: {
                  ...newsOiFrameFixture().oi!,
                  oi_value_usd: 240_000_000,
                  whale_long_profit_bps: 9_600,
                },
              }),
            ],
          }),
        }),
      ),
    );
    renderOi();
    const better = await screen.findByRole("button", { name: /WIF/ });
    expect(better).toHaveTextContent("盈利正桶");
    expect(better).toHaveTextContent("持仓最优桶");
  });

  it("says a pending horizon is 未到期 and never draws it as 0.00%", async () => {
    server.use(
      http.get(/.*\/api\/news\/feed$/, () =>
        HttpResponse.json({
          ok: true,
          data: newsFeedFixture({
            events: [
              newsOiFrameFixture({
                reaction: newsReactionFixture({
                  return_1h_bps: 112,
                  return_4h_bps: null,
                  state: "partial",
                }),
              }),
            ],
          }),
        }),
      ),
    );

    renderOi();
    const row = await screen.findByRole("button", { name: /WIF/ });
    expect(row).toHaveTextContent("+1.12%");
    expect(row).toHaveTextContent("未到期");
    expect(row).not.toHaveTextContent("0.00%");
  });

  it("degrades a row with no oi block instead of re-parsing the wire line", async () => {
    // Re-deriving the numbers from `leader_title` would be `oi_signal_parser_v1` running a second time in
    // the browser. The row keeps every column it can still answer and says what is missing.
    server.use(
      http.get(/.*\/api\/news\/feed$/, () =>
        HttpResponse.json({
          ok: true,
          data: newsFeedFixture({ events: [newsOiFrameFixture({ oi: null })] }),
        }),
      ),
    );

    renderOi();
    await screen.findByRole("heading", { name: "OI 来源与准入审计" });
    // By structure, not by rendered text: with no `oi` block the row has no symbol and no measurement to
    // name it by, and its clock is local time — which is a different hour in CI than on this machine.
    await waitFor(() => expect(document.querySelector(".news-oi-row-main")).toBeInTheDocument());
    const row = document.querySelector(".news-oi-row-main") as HTMLElement;
    // Every column the block would have answered says nothing — including the symbol, which for this lane
    // lives only in the block. The wire line is right there in the expansion; the page does not read it.
    expect(row).not.toHaveTextContent("6.71%");
    expect(row).not.toHaveTextContent("WIF");
    expect(document.querySelector(".news-oi-symbol b")?.textContent).toBe("—");

    fireEvent.click(row);
    expect(await screen.findByText(/这条事件没有/)).toBeInTheDocument();
    // The provider's own line is still shown; the page just does not read numbers out of it.
    expect(screen.getByText(/WIF OI Rise 6.71%/)).toBeInTheDocument();
  });

  it("shows the two compact policy sets side by side and never merges them", async () => {
    renderOi();
    await screen.findByRole("heading", { name: "OI 来源与准入审计" });

    const gates = await screen.findByRole("heading", { name: "推送闸门 · NEWS.OI" });
    const gatesCard = gates.closest("article") as HTMLElement;
    expect(within(gatesCard).getByText("> 80%")).toBeInTheDocument();
    expect(within(gatesCard).getByText("≤ 2 / 4h")).toBeInTheDocument();
    expect(gatesCard).toHaveTextContent("106");
    expect(gatesCard).toHaveTextContent("30");
    expect(gatesCard).toHaveTextContent("拦下 0");

    /*
     * The capital half is Admission's own configuration, read from the same batch as the answers it
     * filed (#269/#331). It used to read the operator's `trading` settings document that News
     * republishes — printing 持仓规模 ≥2000 万 while admission ran at 500 万 — and to show an Alpha
     * threshold as though it were the lane's.
     */
    const admission = screen.getByRole("heading", { name: "准入闸 · TRADING" });
    const admissionCard = admission.closest("article") as HTMLElement;
    expect(within(admissionCard).getByText("≥500 万")).toBeInTheDocument(); // 5_000_000
    // #348 retired the capital rank ceiling and the per-symbol cooldown. The push gate keeps its own
    // rank (`≤ 2 / 4h`, asserted above), and the two must not be confused for one another again.
    expect(within(admissionCard).queryByText("≤ 2")).toBeNull();
    expect(admissionCard).not.toHaveTextContent("冷却");
    // #376: both closed source-native bindings are visible and there is no venue fallback priority.
    expect(admissionCard).toHaveTextContent("binance.usdm → BINANCE_USDM");
    expect(admissionCard).toHaveTextContent("hyperliquid.perp → HYPERLIQUID_PERP");
    expect(admissionCard).toHaveTextContent("禁止跨场所回退");
    expect(admissionCard).toHaveTextContent("5m");
    expect(admissionCard).toHaveTextContent("Alpha 阈值随案例冻结");
    expect(admissionCard).not.toHaveTextContent("≥95%");
    expect(screen.queryByRole("heading", { name: "交易地板 · TRADING" })).toBeNull();
  });

  it("expands to Event, token, OI and exact Trading traces", async () => {
    renderOi();
    fireEvent.click(await screen.findByRole("button", { name: /WIF/ }));

    expect(screen.getByRole("link", { name: /打开事件详情/ })).toHaveAttribute(
      "href",
      "/news/events/evt-oi-wif",
    );
    expect(screen.getByRole("link", { name: /代币页 WIF/ })).toHaveAttribute(
      "href",
      "/news/symbols/WIF",
    );
    expect(screen.getByText("判定痕迹 · OI_JUDGMENT_TRACE")).toBeInTheDocument();
    expect(screen.getByText("资本准入 · ADMISSION")).toBeInTheDocument();
    expect(screen.getAllByText("未评估").length).toBeGreaterThan(0);
    // The trace is admission's alone: no Case state, no execution state (#331).
    expect(screen.queryByText(/order-wif|OPEN_PROTECTED/)).toBeNull();
  });

  it("names the failed capital read, and offers a retry, even on a cold failure", async () => {
    /*
     * A cold admission failure leaves the panel with nothing to print, and four `—` tiles read as "no
     * admission rule is configured" rather than "we could not ask" — so it has to be named above the
     * fold and retryable, not only reported inside the panel.
     */
    server.use(
      http.get(/.*\/api\/trading\/gate$/, () =>
        HttpResponse.json({ ok: false, error: "gate unavailable" }, { status: 503 }),
      ),
    );
    renderOi();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("准入台账读取失败");
    expect(within(alert).getByRole("button", { name: "重试" })).toBeInTheDocument();
    // And the panel says the same thing where the missing numbers are.
    expect(screen.getByText("SOURCE_NATIVE · 准入规则未读到")).toBeInTheDocument();
  });

  it("names why a frame has no case, instead of one 未成案 for four different facts", async () => {
    /*
     * #269. The admission ledger has held a named reason per source since #264, and this column could
     * not ask for it: `/trading/events/{id}` answers one Event, and a page of frames is a hundred round
     * trips. So 未成案 was the same cell for "below the liquidity floor", "no perp at the venue whose OI
     * moved" and "the lane never evaluated it" — three different operational answers.
     */
    server.use(
      http.get(/.*\/api\/news\/feed$/, () =>
        HttpResponse.json({
          ok: true,
          data: newsFeedFixture({
            events: [
              newsOiFrameFixture({ event_id: "evt-oi-storj", leader_title: "STORJ OI Rise 3.10%" }),
              newsOiFrameFixture({ event_id: "evt-oi-nvda", leader_title: "NVDA OI Rise 4.55%" }),
              newsOiFrameFixture({ event_id: "evt-unseen", leader_title: "ZETA OI Rise 1.10%" }),
            ],
          }),
        }),
      ),
      http.get(/.*\/api\/trading\/gate$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingGateFixture({
            decisions: [
              tradingGateDecisionFixture(),
              tradingGateDecisionFixture({
                base_symbol: "NVDA",
                event_id: "evt-oi-nvda",
                gate_evidence: gateEvidence({ venue: "binance" }),
                gate_reason: "research_only_venue",
                gate_stage: "venue",
                gate_status: "RESEARCH_ONLY",
                research_only: true,
                source_key: "oi:evt-oi-nvda:oi_signal_v1",
                underlying_key: "stock:NVDA",
              }),
            ],
          }),
        }),
      ),
    );
    renderOi();

    const rows = await screen.findAllByRole("button", { expanded: false });
    // The Chinese is the title; the cell carries the status and the raw `stage:reason` an operator greps.
    expect(rows[0]).toHaveTextContent("eligibility:oi_value_below_floor已拒绝");
    // A real market fact from a venue this lane may study and never trade (#331). Not a refusal, and
    // not drawn as one: the ledger's own word for it is `RESEARCH_ONLY`.
    expect(rows[1]).toHaveTextContent("venue:research_only_venue仅研究");
    // And a frame with no ledger row at all is an absence, not a refusal.
    expect(rows[2]).toHaveTextContent("未评估");

    fireEvent.click(rows[0]);
    // The expanded trace carries the row the ledger wrote, evidence included.
    expect(screen.getByText("资本准入 · ADMISSION")).toBeInTheDocument();
    expect(
      screen.getByText(/持仓额低于流动性地板（eligibility:oi_value_below_floor）/),
    ).toBeInTheDocument();
    expect(screen.getByText("5000000")).toBeInTheDocument();
  });

  it("says so plainly when the window holds no eligible frame", async () => {
    server.use(
      http.get(/.*\/api\/news\/status$/, () =>
        HttpResponse.json({
          ok: true,
          data: newsStatusFixture({
            oi: { ...newsStatusFixture().oi, window_occupancy: [] },
          }),
        }),
      ),
      http.get(/.*\/api\/news\/feed$/, () =>
        HttpResponse.json({ ok: true, data: newsFeedFixture({ events: [] }) }),
      ),
    );

    renderOi();
    expect(await screen.findByText("这个窗口里没有符合当前判定的遥测帧。")).toBeInTheDocument();
  });
});

function renderOi(path = "/news/oi") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <div className="center-column">
          <NewsPage token="test-token" view="oi" />
          <LocationProbe />
        </div>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function LocationProbe() {
  const location = useLocation();
  return <span data-testid="location">{`${location.pathname}${location.search}`}</span>;
}
