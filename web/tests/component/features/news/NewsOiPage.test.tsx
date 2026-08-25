import { NewsPage } from "@features/news";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import {
  newsFeedFixture,
  newsOiFrameFixture,
  newsReactionFixture,
  newsStatusFixture,
} from "@tests/fixtures/newsFixture";
import { server } from "@tests/msw/server";
import { HttpResponse, http } from "msw";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

/**
 * 持仓异动监控 (#207/#137). The lane is judged by rule rather than by the model, and the whole point of this
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
    await screen.findByRole("heading", { name: "持仓异动监控" });

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
    await screen.findByRole("heading", { name: "持仓异动监控" });

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
    expect(screen.getByText("telemetry_received_24h").closest(".ui-metric")).toHaveTextContent(
      "142",
    );
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

  it("stamps no research bucket when the capital lane sends no floor", async () => {
    // A console newer than the API gets the schema's zero defaults. `profit >= 0` would badge every frame
    // as 研究里唯一均值为正的分桶 — a claim about a measured bucket, made against no threshold at all.
    server.use(
      http.get(/.*\/api\/news\/status$/, () =>
        HttpResponse.json({
          ok: true,
          data: newsStatusFixture({
            oi: {
              ...newsStatusFixture().oi,
              trade_floors: {
                enabled: false,
                max_price_move_bps: 0,
                min_oi_value_usd: 0,
                min_price_move_bps: 0,
                min_whale_long_profit_bps: 0,
                mode: "paper",
                pre_move_lookback_ms: 0,
              },
            },
          }),
        }),
      ),
    );

    renderOi();
    const row = await screen.findByRole("button", { name: /WIF/ });
    expect(row).not.toHaveTextContent("盈利正桶");
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

  it("reads the symbol from the judge's trace, the only place this lane carries it", async () => {
    /*
     * The row's subject is on none of the three fields a reader would reach for first. `triage.assets` is
     * the Event detail's `full=True` shape and the feed sends the slim one; `assets` and `grounded_assets`
     * come from the Gate, which grounds provider coin tags at admission — and a strategy-1019 frame ships
     * none, so on a live telemetry Event both are `[]`. Two successive fixes read those instead and the
     * column stayed `—` on the deployed console both times, because the fixture filled what production
     * leaves empty. This asserts the production shape: nothing but `oi.symbol`.
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
    await screen.findByRole("heading", { name: "持仓异动监控" });
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
    await screen.findByRole("heading", { name: "持仓异动监控" });
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
    await screen.findByRole("heading", { name: "持仓异动监控" });
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

  it("shows the two threshold sets side by side and never merges them", async () => {
    renderOi();
    await screen.findByRole("heading", { name: "持仓异动监控" });

    const gates = await screen.findByRole("heading", { name: "新闻闸门与它们拦下的量" });
    const gatesCard = gates.closest("section") as HTMLElement;
    expect(within(gatesCard).getByText("> 80.00%")).toBeInTheDocument();
    expect(within(gatesCard).getByText("前 2 次")).toBeInTheDocument();
    // The gate names are the server's keys, and each carries the count that gate withheld.
    expect(gatesCard).toHaveTextContent("whale_ratio_below_threshold");
    expect(gatesCard).toHaveTextContent("106");
    expect(gatesCard).toHaveTextContent("30");

    const floors = screen.getByRole("heading", { name: "交易地板（另一套阈值）" });
    const floorsCard = floors.closest("section") as HTMLElement;
    expect(within(floorsCard).getByText("≥ 95.00%")).toBeInTheDocument();
    expect(within(floorsCard).getByText("≥ 2000 万")).toBeInTheDocument();
    // trading ships disabled, so the page says these are a published band rather than a live gate.
    expect(floorsCard).toHaveTextContent("trading 当前关闭");
    // The pre-frame move needs a price the News plane does not store, and the page says so instead of
    // approximating it.
    expect(floorsCard).toHaveTextContent("未测量");
  });

  it("reports the live window's occupancy and flags the symbols already full", async () => {
    renderOi();
    const window = (await screen.findByRole("heading", { name: "窗口占用" })).closest(
      "section",
    ) as HTMLElement;

    expect(within(window).getByText("WIF")).toBeInTheDocument();
    expect(within(window).getByText("2 / 2")).toBeInTheDocument();
    expect(within(window).getByText("已满，后续帧会被拦")).toBeInTheDocument();
    expect(within(window).getByText("1 / 2")).toBeInTheDocument();
  });

  it("names the endpoint and field behind every panel", async () => {
    // #207 principle 2: a figure whose provenance cannot be written as `GET /api/… → field` is a figure the
    // browser derived, and these lines are what make that impossible to hide.
    renderOi();
    await screen.findByRole("heading", { name: "持仓异动监控" });

    await waitFor(() =>
      expect(
        screen.getByText("GET /api/news/status → pipeline.telemetry_*_24h · oi.by_rule_24h"),
      ).toBeInTheDocument(),
    );
    expect(
      screen.getByText("GET /api/news/status → oi.policy · oi.by_rule_24h"),
    ).toBeInTheDocument();
    expect(screen.getByText("GET /api/news/status → oi.window_occupancy")).toBeInTheDocument();
    expect(
      screen.getByText("GET /api/news/feed?admission=telemetry_deterministic&hours=24"),
    ).toBeInTheDocument();
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
    expect(
      await screen.findByText("窗口内还没有合格帧，下一帧的名次是第 1 次。"),
    ).toBeInTheDocument();
    expect(screen.getByText("这个窗口里没有符合当前判定的遥测帧。")).toBeInTheDocument();
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
