import { NewsPage } from "@features/news";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import {
  newsFeedFixture,
  newsOiFrameFixture,
  newsStatusFixture,
} from "@tests/fixtures/newsFixture";
import {
  tradingCaseFixture,
  tradingOrderFixture,
  tradingOrdersFixture,
  tradingStatusFixture,
} from "@tests/fixtures/tradingFixture";
import { server } from "@tests/msw/server";
import { HttpResponse, http } from "msw";
import type { ReactNode } from "react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

/**
 * 杠杆异动 (#256). The capital lane's reading of the deterministic OI frames.
 *
 * The tests here are mostly about what the page refuses to say: it lists no frame that authored no case, it
 * names the rule instead of paraphrasing a thesis nobody wrote, and it never mixes a frozen cutoff figure
 * with a live quote in one row.
 */
describe("NewsLeveragePage", () => {
  beforeEach(() => {
    server.use(
      http.get(/.*\/api\/news\/status$/, () =>
        HttpResponse.json({ ok: true, data: newsStatusFixture() }),
      ),
      http.get(/.*\/api\/news\/feed$/, () =>
        HttpResponse.json({
          ok: true,
          data: newsFeedFixture({
            events: [
              newsOiFrameFixture(),
              newsOiFrameFixture({ event_id: "evt-oi-hype", leader_title: "HYPE OI Rise 2.10%" }),
              newsOiFrameFixture({ event_id: "evt-never-judged" }),
            ],
          }),
        }),
      ),
      http.get(/.*\/api\/trading\/orders$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingOrdersFixture({ orders: [tradingOrderFixture()] }),
        }),
      ),
      // #269: the capital lane's own status carries the durable 24 h funnel and the rules the evidence
      // matrix compares against — the Candidate Gate's and each strategy's, not the settings document.
      http.get(/.*\/api\/trading\/status$/, () =>
        HttpResponse.json({ ok: true, data: tradingStatusFixture() }),
      ),
      http.get(/.*\/api\/news\/quotes$/, () =>
        HttpResponse.json({ ok: true, data: { measured_at_ms: 0, quotes: [] } }),
      ),
    );
  });

  afterEach(cleanup);

  it("lists only frames that reached the capital lane, and points at the audit for the rest", async () => {
    renderLeverage();
    await screen.findByRole("heading", { name: "市场杠杆结构" });

    const list = await screen.findByRole("region", { name: "案例列表" });
    const cards = within(list).getAllByRole("button");
    expect(cards).toHaveLength(1);
    expect(cards[0]).toHaveTextContent("WIF");
    // The third frame never authored a case and must not appear as one.
    expect(within(list).queryByText(/evt-never-judged/)).toBeNull();
    expect(screen.getByRole("link", { name: "OI 遥测审计" })).toHaveAttribute("href", "/news/oi");
  });

  it("names the rule and never invents a thesis for a pure-rule strategy", async () => {
    server.use(
      http.get(/.*\/api\/trading\/orders$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingOrdersFixture({
            cases_without_orders: [tradingCaseFixture({ event_id: "evt-oi-wif" })],
            orders: [],
          }),
        }),
      ),
    );
    renderLeverage("/news/leverage?lev=no_trade");
    await screen.findByRole("heading", { name: "市场杠杆结构" });

    const detail = await screen.findByRole("region", { name: /^案例 / });
    expect(detail).toHaveTextContent("为什么不交易 · NAMED RULE");
    expect(detail).toHaveTextContent("不交易：鲸盈利未达地板。");
    expect(detail).toHaveTextContent("whale_profit_below_floor");
    // No position, so nothing to invalidate — said outright rather than left blank.
    expect(detail).toHaveTextContent("—（no_trade 无持仓可失效）");
  });

  it("keeps the frozen plane and the live one on separate tabs", async () => {
    /*
     * The single most misleading thing this page could draw is a frozen cutoff figure beside a live quote in
     * adjacent cells: it invites subtracting one from the other and calling the difference a return.
     */
    renderLeverage();
    const detail = await screen.findByRole("region", { name: /^案例 / });

    expect(detail).toHaveTextContent("冻结 · cutoff 之前可见的事实");
    expect(detail).toHaveTextContent("冻结入场参考");
    expect(detail).not.toHaveTextContent("当前报价");

    fireEvent.click(within(detail).getByRole("tab", { name: "现在" }));
    expect(detail).toHaveTextContent("当前报价");
    expect(detail).toHaveTextContent("独立报价轮询，从不回写案例");
    expect(detail).not.toHaveTextContent("冻结入场参考");
  });

  it("carries the selected case and tab in the URL", async () => {
    renderLeverage();
    await screen.findByRole("region", { name: "案例列表" });

    fireEvent.click(screen.getByRole("tab", { name: /不交易/ }));
    expect(screen.getByTestId("location").textContent).toBe("/news/leverage?lev=no_trade");
  });

  it("shows the case a shared link names even when the tab would filter it out", async () => {
    // Substituting a different case silently is what makes a shared link point at the wrong money.
    server.use(
      http.get(/.*\/api\/trading\/orders$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingOrdersFixture({
            cases_without_orders: [tradingCaseFixture({ event_id: "evt-oi-hype" })],
            orders: [tradingOrderFixture()],
          }),
        }),
      ),
    );
    // The URL holds the ledger's own `case_id`; `event_id` is null for most of the lane.
    renderLeverage("/news/leverage?lev=live&case=case-hype");

    const detail = await screen.findByRole("region", { name: /^案例 / });
    expect(detail).toHaveTextContent("NO TRADE · 不交易");
  });

  it("never describes a stop the ledger has not proven as a working one", async () => {
    /*
     * `stopVerified` recognises exactly `OPEN` — a filled position with a read-back reduce-only stop
     * covering it. Telling an operator that crossing `stop_price` triggers a native stop on an
     * `ACKNOWLEDGED` order is false risk assurance on precisely the row that may carry exposure with no
     * working protection (#185 P0-3).
     */
    server.use(
      http.get(/.*\/api\/trading\/orders$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingOrdersFixture({
            cases_without_orders: [],
            orders: [tradingOrderFixture({ state: "ACKNOWLEDGED" })],
          }),
        }),
      ),
    );
    renderLeverage();

    const detail = await screen.findByRole("region", { name: /^案例 / });
    expect(detail).toHaveTextContent("账本尚未证明交易所持有对应的原生止损（ACKNOWLEDGED）");
    expect(detail).not.toHaveTextContent("触发原生止损");
  });

  it("reads a cold status failure as a failure, not as an unconfigured floor", async () => {
    // Without it the page would compare every case against no thresholds at all and print 未配置地板 /
    // 不适用 for rules the lane is very much applying. #269 moved this read to the capital lane's own
    // status, which is where the Candidate Gate and the strategy configs live.
    server.use(
      http.get(/.*\/api\/trading\/status$/, () =>
        HttpResponse.json({ ok: false, error: "status unavailable" }, { status: 503 }),
      ),
    );
    renderLeverage();

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText("未配置地板", { exact: false })).toBeNull();
  });

  it("lists news-triggered cases, which never carry an event id", async () => {
    /*
     * The shape production actually serves: on 2026-08-26 the ledger held nine cases, every one
     * `trigger_kind: news` with `event_id: null`, and the page rendered zero rows and told the operator
     * 「24 小时内没有成案」. `event_id` is published only for the deterministic OI trigger — a model-lane
     * source key is a content hash no Event id rebuilds — so the lane must be keyed by `case_id`.
     */
    server.use(
      http.get(/.*\/api\/trading\/orders$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingOrdersFixture({
            cases_without_orders: ["AAVE", "AVAX", "ZEC"].map((base) =>
              tradingCaseFixture({
                base_symbol: base,
                case_id: `case-${base}`,
                event_id: null,
                policy_reason: "oi_context_missing",
                state: "POLICY_REJECTED",
                strategy_id: "news_oi_alignment_v1",
                trigger_kind: "news",
                underlying_key: `crypto:${base}`,
              }),
            ),
            orders: [],
          }),
        }),
      ),
    );
    renderLeverage("/news/leverage?lev=no_trade");

    const list = await screen.findByRole("region", { name: "案例列表" });
    /*
     * Three cases agreeing on strategy and rule arrive as one counted row (#269). Production served 59 of
     * these in a day — `news_oi_alignment_v1` needs a News trigger and a fresh OI frame for one issuer to
     * meet inside a scan window, so this outcome is the lane's resting state rather than an event — and
     * listing them one card each buried the day's only OI case in the middle of them.
     */
    const group = within(list).getByRole("button", { expanded: false });
    expect(group).toHaveTextContent("3");
    expect(group).toHaveTextContent("OI 上下文缺失");
    expect(group).toHaveTextContent("AAVE · AVAX · ZEC");
    expect(screen.queryByText("24 小时内没有成案")).toBeNull();

    // Collapsed, never dropped: every case is one click away and still individually selectable.
    fireEvent.click(group);
    expect(within(list).getAllByText("非 OI 触发 · 无遥测帧")).toHaveLength(3);
  });

  it("names a non-OI trigger the same way in the card and in the detail", async () => {
    /*
     * The card and the pane must not contradict each other: saying 非 OI 触发 on the left while the kv row
     * reads `OI 帧`, the timeline says `OI 帧落库` and the raw block blames a page boundary would tell the
     * operator a frame existed and was merely not loaded.
     */
    server.use(
      http.get(/.*\/api\/trading\/orders$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingOrdersFixture({
            cases_without_orders: [
              tradingCaseFixture({
                case_id: "case-news",
                event_id: null,
                state: "POLICY_REJECTED",
                trigger_kind: "news",
              }),
            ],
            orders: [],
          }),
        }),
      ),
    );
    renderLeverage("/news/leverage?lev=no_trade");

    const detail = await screen.findByRole("region", { name: /^案例 / });
    expect(detail).toHaveTextContent("新闻 · ");
    expect(detail).not.toHaveTextContent("OI 帧落库");
    expect(detail).not.toHaveTextContent("原帧不在本页帧里");
    expect(detail).toHaveTextContent("这条通道没有遥测帧");
  });

  it("opens the case a pre-rename event-id link names, not a different one", async () => {
    // Identity moved from `event_id` to `case_id` (#262); a link shared before that must still resolve to
    // its own case rather than silently falling through to the first row.
    server.use(
      http.get(/.*\/api\/trading\/orders$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingOrdersFixture({
            cases_without_orders: [],
            orders: [
              tradingOrderFixture({
                base_symbol: "WIF",
                case_id: "case-wif",
                event_id: "evt-oi-wif",
              }),
              tradingOrderFixture({
                base_symbol: "DOGE",
                case_id: "case-doge",
                event_id: "evt-oi-doge",
                order_id: "order-doge",
                state: "AMBIGUOUS",
              }),
            ],
          }),
        }),
      ),
    );
    // AMBIGUOUS sorts first, so a silent fallback would show DOGE.
    renderLeverage("/news/leverage?case=evt-oi-wif");

    expect(await screen.findByRole("region", { name: "案例 WIF" })).toBeInTheDocument();
  });

  it("describes the 24 hours it produced nothing in, rather than drawing an empty frame", async () => {
    /*
     * #269. Production runs about 110 frames and one case a day, so this is the *ordinary* state of the
     * page, not an edge case — and four zeroes over a blank list is a true statement that reads as an
     * outage. The durable admission ledger is the only source that can describe such a day at all:
     * `funnel_today` is overwritten at UTC midnight and the case-keyed counts are all zero by definition.
     */
    server.use(
      http.get(/.*\/api\/trading\/orders$/, () =>
        HttpResponse.json({
          ok: true,
          data: tradingOrdersFixture({ cases_without_orders: [], orders: [] }),
        }),
      ),
    );
    renderLeverage();

    expect(await screen.findByText("24 小时内没有成案")).toBeInTheDocument();
    const funnel = screen.getByRole("region", { name: "资本通道 24 小时漏斗" });
    expect(within(funnel).getByText("遥测帧").previousSibling).toHaveTextContent("91");
    expect(within(funnel).getByText("过闸").previousSibling).toHaveTextContent("1");
    // And which rule is binding, by name — the number an operator would otherwise replay SQL for.
    expect(funnel).toHaveTextContent("窗口内名次超限");
  });

  it("says the funnel is unreadable rather than reporting zero frames, when status fails", async () => {
    server.use(
      http.get(/.*\/api\/trading\/status$/, () =>
        HttpResponse.json({ ok: false, error: "status unavailable" }, { status: 503 }),
      ),
    );
    renderLeverage();
    // The read is retried before it is a failure; wait for the page's own verdict on it, then read the
    // funnel. Asserting straight away caught the funnel mid-retry and passed against its loading zeroes.
    await screen.findByRole("alert");

    const funnel = screen.getByRole("region", { name: "资本通道 24 小时漏斗" });
    expect(funnel).toHaveTextContent("资本通道状态读取失败");
    // Not zero frames. "The lane saw nothing" and "we could not ask" are different days.
    expect(within(funnel).queryByText("0")).toBeNull();
  });

  it("binds no document-level keys", async () => {
    // #82 cut the console's keyboard layer whole and `keyboardLayerHardCut.test.ts` keeps it cut. The
    // artifact's `j/k/f` list bindings are deliberately not here; the cards are real buttons in tab order.
    renderLeverage();
    const list = await screen.findByRole("region", { name: "案例列表" });

    fireEvent.keyDown(document, { key: "j" });
    expect(within(list).getAllByRole("button")[0]).toHaveAttribute("aria-pressed", "true");
  });
});

function renderLeverage(path = "/news/leverage") {
  return renderWithRouter(<NewsPage token="test-token" view="leverage" />, path);
}

function renderWithRouter(node: ReactNode, path: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <div className="center-column">
          {node}
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
