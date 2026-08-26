import { NewsPage } from "@features/news";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import {
  NEWS_NOW_MS,
  newsDeliveryFixture,
  newsEventDetailFixture,
  newsEventReactionFixture,
  newsFeedEventFixture,
  newsFeedFixture,
  newsOutcomeFixture,
  newsStatusFixture,
  newsVerdictFixture,
} from "@tests/fixtures/newsFixture";
import { server } from "@tests/msw/server";
import { HttpResponse, http } from "msw";
import type { ReactNode } from "react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

describe("NewsPage", () => {
  beforeEach(() => {
    server.use(
      http.get(/.*\/api\/news\/feed$/, () =>
        HttpResponse.json({ ok: true, data: newsFeedFixture() }),
      ),
      http.get(/.*\/api\/news\/events\/evt-global-policy$/, () =>
        HttpResponse.json({ ok: true, data: newsEventDetailFixture() }),
      ),
      http.get(/.*\/api\/news\/status$/, () =>
        HttpResponse.json({ ok: true, data: newsStatusFixture() }),
      ),
    );
  });

  afterEach(cleanup);

  // ------------------------------------------------------------------ feed
  it("defaults to pushed Events for the latest 24 h, with the approved tab and time controls", async () => {
    const observed: Record<string, string | null> = {};
    server.use(
      http.get(/.*\/api\/news\/feed$/, ({ request }) => {
        const params = new URL(request.url).searchParams;
        for (const name of [
          "limit",
          "sort",
          "priority",
          "decision",
          "family",
          "admission",
          "outcome",
          "hours",
          "direction",
          "channel",
        ]) {
          observed[name] = params.get(name);
        }
        return HttpResponse.json({ ok: true, data: newsFeedFixture() });
      }),
    );

    renderNews(<NewsPage token="test-token" view="feed" />);

    expect(await screen.findByRole("heading", { name: "新闻事件流" })).toBeInTheDocument();
    // Section navigation lives in the shell sidebar now (#82); this renders the surface on its own.
    expect(screen.queryByRole("link", { name: "状态" })).not.toBeInTheDocument();
    // The counts arrive with the first page, so wait for it before reading the tabs.
    await screen.findByRole("heading", { name: /央行政策转向，风险资产承压/ });
    const tabs = screen.getByRole("tablist", { name: "按结局筛选" });
    expect(
      within(tabs)
        .getAllByRole("tab")
        .map((tab) => tab.textContent),
      // Label plus the server's count for that group under the current filter, in the approved tab name.
    ).toEqual(["已推送41", "被拦截271", "处理中8", "全部320"]);
    expect(within(tabs).getByRole("tab", { name: "已推送 41" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByText("1 / 41 条")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "时间范围，最近 1 天" })).toBeInTheDocument();
    expect(screen.getByText("TIME")).toBeInTheDocument();
    expect(screen.getByText("EVENT")).toBeInTheDocument();
    expect(screen.getByText("OUTCOME")).toBeInTheDocument();
    await waitFor(() => expect(observed.hours).toBe("24"));
    expect(observed.sort).toBeNull();
    expect(observed.limit).toBe("25");
    expect(observed.outcome).toBe("pushed");
    expect(observed.direction).toBeNull();
    expect(observed.channel).toBeNull();
    expect(observed.priority).toBeNull();
    expect(observed.decision).toBeNull();
    expect(observed.family).toBeNull();
    expect(observed.admission).toBeNull();
  });

  it("renders one Event row as when · what · one outcome, with server-labelled facts and no rule keys", async () => {
    renderNews(<NewsPage token="test-token" view="feed" />);

    const row = (
      await screen.findByRole("heading", { name: /央行政策转向，风险资产承压/ })
    ).closest("article");
    expect(row).not.toBeNull();
    const inRow = within(row!);
    expect(row).toHaveAttribute("data-outcome", "delivered");
    expect(inRow.getByRole("link", { name: /央行政策转向，风险资产承压/ })).toHaveAttribute(
      "href",
      "/news/events/evt-global-policy",
    );
    expect(
      inRow.getByText("Central banks respond to a new global policy shock"),
    ).toBeInTheDocument();
    expect(inRow.getByText("Reuters World")).toBeInTheDocument();
    expect(inRow.getByText("利空")).toBeInTheDocument();
    expect(inRow.getByText("宏观")).toBeInTheDocument();
    /*
     * The meta line is 来源 · 方向 · 类型 · 资产 and nothing else. Magnitude and the merged-report count are
     * real facts, but they belong to the Event, not to a scan: both are one click away in the drawer and on
     * the Event's own page, and a fifth and sixth item here pushed the assets onto a second line.
     */
    expect(inRow.queryByText("4 条报道")).toBeNull();
    expect(inRow.queryByText("影响明显")).toBeNull();
    // #87: a chip names the venue as well as the ticker, so the reader can tell a Binance perp from a
    // Hyperliquid builder-DEX equity without opening the Event.
    expect(inRow.getByLabelText("关联资产")).toHaveTextContent("binance.perp:BTCbinance.perp:ETH");
    // #87: the row carries no buttons of its own — copy, label and open-original all live on the Event.
    expect(inRow.queryByRole("link", { name: "打开原文" })).toBeNull();
    expect(inRow.queryByRole("button", { name: "复制标题" })).toBeNull();
    // Exactly one outcome badge; queue scheduling metadata never changes its reader-facing loudness.
    const badges = row!.querySelectorAll(".news-outcome");
    expect(badges).toHaveLength(1);
    expect(badges[0]).toHaveTextContent("已推送");
    expect(row).not.toHaveAttribute("data-priority");
    expect(badges[0]).toHaveAttribute("data-variant", "text");
    expect(badges[0]).toHaveAttribute("title", "模型判断值得推送");
    expect(inRow.getByText("模型判断值得推送")).toBeInTheDocument();
    for (const raw of ["model_push_actionable", "candidate", "asset:BTC", "escalate", "general"]) {
      expect(inRow.queryByText(raw)).not.toBeInTheDocument();
    }
  });

  it("keeps Event Reaction off the approved three-column feed", async () => {
    server.use(
      http.get(/.*\/api\/news\/feed$/, () =>
        HttpResponse.json({
          ok: true,
          data: newsFeedFixture({
            events: [
              newsFeedEventFixture({
                delivery: null,
                outcome: newsOutcomeFixture({
                  group: "held",
                  kind: "dropped",
                  reason_zh: "判为噪声",
                  text_zh: "未推送",
                }),
              }),
            ],
          }),
        }),
      ),
    );

    renderNews(<NewsPage token="test-token" view="feed" />);

    const row = (
      await screen.findByRole("heading", { name: /央行政策转向，风险资产承压/ })
    ).closest("article");
    expect(row!.querySelector(".news-event-move")).toBeNull();
    expect(row).toHaveTextContent("判为噪声");
  });

  it("marks hour runs without reordering the server's page", async () => {
    server.use(
      http.get(/.*\/api\/news\/feed$/, () =>
        HttpResponse.json({
          ok: true,
          data: newsFeedFixture({
            events: [
              newsFeedEventFixture({
                event_id: "evt-late",
                leader_title: "late",
                opened_at_ms: new Date(2026, 7, 21, 3, 50).getTime(),
              }),
              newsFeedEventFixture({
                event_id: "evt-early",
                leader_title: "early",
                opened_at_ms: new Date(2026, 7, 21, 3, 4).getTime(),
              }),
              newsFeedEventFixture({
                event_id: "evt-before",
                leader_title: "before",
                opened_at_ms: new Date(2026, 7, 21, 2, 30).getTime(),
              }),
            ],
          }),
        }),
      ),
    );

    const { container } = renderNews(<NewsPage token="test-token" view="feed" />);
    await screen.findByText("03:00 — 04:00");

    const headings = [...container.querySelectorAll(".news-event-group-heading")].map(
      (heading) => heading.textContent,
    );
    expect(headings).toEqual(["03:00 — 04:002 条", "02:00 — 03:001 条"]);
    // The rows come back in the order the server sent them; grouping does not re-sort the page.
    expect(
      [...container.querySelectorAll(".news-event-row")].map((row) =>
        row.getAttribute("data-event-id"),
      ),
    ).toEqual(["evt-late", "evt-early", "evt-before"]);
    // Every row already announces its full timestamp; a screen reader that stopped for each strip would
    // read the hour twice.
    for (const heading of container.querySelectorAll(".news-event-group-heading")) {
      expect(heading).toHaveAttribute("aria-hidden", "true");
    }
  });

  it("shapes the cold load like the page that is coming, then puts it away", async () => {
    let releaseStatus: (() => void) | null = null;
    const held = new Promise<void>((resolve) => {
      releaseStatus = resolve;
    });
    server.use(
      http.get(/.*\/api\/news\/status$/, async () => {
        await held;
        return HttpResponse.json({ ok: true, data: newsStatusFixture() });
      }),
    );

    const { container } = renderNews(<NewsPage token="test-token" view="feed" />);

    // Five tiles for the five funnel stages: the band does not change height when the figures land.
    const tiles = await screen.findByRole("status", { name: "正在读取 24 小时漏斗" });
    expect(tiles.querySelectorAll(".page-state-tile")).toHaveLength(5);
    expect(container.querySelector(".news-funnel-card")).toBeNull();

    releaseStatus!();
    await waitFor(() => expect(container.querySelector(".news-funnel-card")).not.toBeNull());
    expect(screen.queryByRole("status", { name: "正在读取 24 小时漏斗" })).toBeNull();
  });

  it("drops the badge on a held Event and leaves its Chinese reason alone", async () => {
    server.use(
      http.get(/.*\/api\/news\/feed$/, () =>
        HttpResponse.json({
          ok: true,
          data: newsFeedFixture({
            events: [
              newsFeedEventFixture({
                delivery: null,
                event_id: "evt-throttled",
                outcome: newsOutcomeFixture({
                  group: "held",
                  kind: "throttled",
                  reason_zh: "BTC 同一话题 2 小时内已推过同等或更重要的消息",
                  text_zh: "未推送（历史限流）",
                }),
                triage: {
                  ...newsFeedEventFixture().triage!,
                  final_decision: "throttled",
                  throttled_by: "storyline:asset:BTC",
                },
              }),
              newsFeedEventFixture({
                delivery: null,
                event_id: "evt-recovery",
                admission: "recovery",
                ingest_mode: "recovery",
                outcome: newsOutcomeFixture({
                  group: "held",
                  kind: "held_recovery",
                  reason_zh: "断线期间补抄的旧闻，仅用于去重与历史",
                  text_zh: "补抄件，不推送",
                }),
                title_zh: "补抄回来的旧闻",
                triage: null,
              }),
            ],
          }),
        }),
      ),
    );

    renderNews(<NewsPage token="test-token" view="feed" />);

    /*
     * Three quarters of a day's Events stop short of a card. Badging every one of them with a capsule made a
     * screenful of equals (#82), so a held row states its outcome as a quiet grey *word* beside the server's
     * `reason_zh` — nothing is dropped, only de-emphasised, and the phone card drops the word too. The rule
     * key behind the decision never appears on a row either way.
     */
    const throttled = (
      await screen.findByText("BTC 同一话题 2 小时内已推过同等或更重要的消息")
    ).closest("article")!;
    expect(throttled).toHaveAttribute("data-outcome-group", "held");
    expect(throttled).toHaveAttribute("data-outcome", "throttled");
    const heldBadge = throttled.querySelector(".news-outcome")!;
    expect(heldBadge).toHaveAttribute("data-variant", "text");
    expect(heldBadge).toHaveAttribute("data-tone", "caution");
    expect(heldBadge).toHaveTextContent("未推送（历史限流）");
    expect(within(throttled).queryByText("storyline:asset:BTC")).not.toBeInTheDocument();
    const recovery = screen.getByText("断线期间补抄的旧闻，仅用于去重与历史").closest("article")!;
    expect(recovery.querySelector(".news-outcome")).toHaveTextContent("补抄件，不推送");
    expect(within(recovery).getByRole("heading", { name: "补抄回来的旧闻" })).toBeInTheDocument();
  });

  it("keeps row-level expansion controls off the approved feed interaction", async () => {
    renderNews(<NewsPage token="test-token" view="feed" />);

    const row = (
      await screen.findByRole("heading", { name: /央行政策转向，风险资产承压/ })
    ).closest("article")!;
    expect(within(row).queryByRole("button", { name: /判定/ })).toBeNull();
    expect(within(row).getByRole("link", { name: /央行政策转向/ })).toBeInTheDocument();
  });

  it("keeps a 668-character feed headline accessible inside the compact density surface", async () => {
    const longTitle = "Very long headline ".repeat(35).trim();
    server.use(
      http.get(/.*\/api\/news\/feed$/, () =>
        HttpResponse.json({
          ok: true,
          data: newsFeedFixture({
            events: [
              newsFeedEventFixture({
                leader_title: longTitle,
                title_zh: null,
                triage: { ...newsFeedEventFixture().triage!, headline_zh: null },
              }),
            ],
          }),
        }),
      ),
    );

    renderNews(<NewsPage token="test-token" view="feed" />);

    const heading = await screen.findByRole("heading", { level: 2, name: longTitle });
    expect(heading.textContent).toHaveLength(longTitle.length);
    expect(heading.closest("[data-page-archetype='scan']")).not.toBeNull();
  });

  it("keeps outcome, time, direction, and channel in URL-owned server state", async () => {
    const observed: Record<string, string | null> = {};
    server.use(
      http.get(/.*\/api\/news\/feed$/, ({ request }) => {
        const params = new URL(request.url).searchParams;
        for (const name of ["q", "outcome", "hours", "direction", "channel"]) {
          observed[name] = params.get(name);
        }
        return HttpResponse.json({ ok: true, data: newsFeedFixture() });
      }),
    );

    renderNews(
      <NewsPage token="test-token" view="feed" />,
      "/news?q=bitcoin&outcome=held&hours=168&direction=bullish,neutral&channel=news",
    );

    await screen.findByRole("heading", { name: /央行政策转向，风险资产承压/ });
    await waitFor(() => expect(observed.direction).toBe("bullish,neutral"));
    expect(observed).toEqual({
      channel: "news",
      direction: "bullish,neutral",
      hours: "168",
      outcome: "held",
      q: "bitcoin",
    });
    expect(screen.getByRole("tab", { name: "被拦截 271" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("button", { name: "时间范围，最近 7 天" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "筛选 · 3" }));
    expect(screen.getByRole("button", { name: "▲ 利多" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "◆ 中性" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "新闻" })).toHaveAttribute("aria-pressed", "true");

    fireEvent.click(screen.getByRole("button", { name: "◆ 中性" }));
    await waitFor(() => expect(observed.direction).toBe("bullish"));
    fireEvent.click(screen.getByRole("button", { name: "OI 帧" }));
    await waitFor(() => expect(observed.channel).toBe("news,oi"));
    fireEvent.click(screen.getByRole("button", { name: "清除" }));
    await waitFor(() => {
      expect(observed.direction).toBeNull();
      expect(observed.channel).toBeNull();
    });
    expect(screen.getByRole("button", { name: "筛选" })).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent("q=bitcoin");
    expect(screen.getByTestId("location")).toHaveTextContent("hours=168");

    fireEvent.click(screen.getByRole("tab", { name: "已推送 41" }));
    await waitFor(() => expect(observed.outcome).toBe("pushed"));
    fireEvent.click(screen.getByRole("tab", { name: "全部 320" }));
    await waitFor(() => expect(observed.outcome).toBeNull());
    expect(screen.getByTestId("location")).toHaveTextContent("outcome=all");
    fireEvent.keyDown(screen.getByRole("button", { name: "时间范围，最近 7 天" }), {
      key: "ArrowDown",
    });
    fireEvent.click(await screen.findByRole("menuitemradio", { name: "最近 1 小时" }));
    await waitFor(() => expect(observed.hours).toBe("1"));
    expect(screen.getByTestId("location")).toHaveTextContent("hours=1");
  });

  it("does not forward retired priority/sort or unknown decision, outcome, and hours values", async () => {
    const observed: Record<string, string | null> = {};
    server.use(
      http.get(/.*\/api\/news\/feed$/, ({ request }) => {
        const params = new URL(request.url).searchParams;
        for (const name of ["priority", "sort", "decision", "outcome", "hours"])
          observed[name] = params.get(name);
        return HttpResponse.json({ ok: true, data: newsFeedFixture() });
      }),
    );

    renderNews(
      <NewsPage token="test-token" view="feed" />,
      "/news?priority=high&sort=priority&decision=maybe&outcome=whatever&hours=999",
    );

    await screen.findByRole("heading", { name: /央行政策转向，风险资产承压/ });
    expect(observed.priority).toBeNull();
    expect(observed.sort).toBeNull();
    expect(observed.decision).toBeNull();
    expect(observed.outcome).toBe("pushed");
    expect(observed.hours).toBe("24");
    expect(screen.queryByRole("group", { name: "已启用筛选" })).not.toBeInTheDocument();
    fireEvent.keyDown(screen.getByRole("button", { name: "时间范围，最近 1 天" }), {
      key: "ArrowDown",
    });
    fireEvent.click(await screen.findByRole("menuitemradio", { name: "最近 7 天" }));
    await waitFor(() => expect(screen.getByTestId("location")).not.toHaveTextContent("priority="));
    expect(screen.getByTestId("location")).not.toHaveTextContent("sort=");
  });

  it("loads the next cursor and deduplicates repeated Event ids", async () => {
    server.use(
      http.get(/.*\/api\/news\/feed$/, ({ request }) => {
        const cursor = new URL(request.url).searchParams.get("cursor");
        if (!cursor) {
          return HttpResponse.json({ ok: true, data: newsFeedFixture({ next_cursor: "page-2" }) });
        }
        return HttpResponse.json({
          ok: true,
          data: newsFeedFixture({
            events: [
              newsFeedEventFixture(),
              newsFeedEventFixture({
                event_id: "evt-second-page",
                triage: { ...newsFeedEventFixture().triage!, headline_zh: "Second page Event" },
              }),
            ],
            next_cursor: null,
          }),
        });
      }),
    );
    renderNews(<NewsPage token="test-token" view="feed" />);
    await screen.findByText(/央行政策转向，风险资产承压/);

    fireEvent.click(screen.getByRole("button", { name: "加载更多事件" }));

    expect(await screen.findByText("Second page Event")).toBeInTheDocument();
    expect(screen.getAllByText(/央行政策转向，风险资产承压/)).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "加载更多事件" })).not.toBeInTheDocument();
  });

  it("shows new first-page events immediately when the reader is at the top", async () => {
    let feed = newsFeedFixture();
    server.use(http.get(/.*\/api\/news\/feed$/, () => HttpResponse.json({ ok: true, data: feed })));
    const rendered = renderNews(<NewsPage token="test-token" view="feed" />);
    await screen.findByText(/央行政策转向，风险资产承压/);

    feed = newsFeedFixture({
      events: [
        newsFeedEventFixture({
          event_id: "evt-new-at-top",
          triage: {
            ...newsFeedEventFixture().triage!,
            headline_zh: "New high-signal event at the top",
          },
        }),
        ...newsFeedFixture().events,
      ],
    });
    await rendered.queryClient.invalidateQueries({ queryKey: ["news-feed"] });

    expect(await screen.findByText("New high-signal event at the top")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /条新事件/ })).not.toBeInTheDocument();
  });

  it("preserves the viewed event, defers new top ids, and still appends a loaded tail", async () => {
    let feed = newsFeedFixture({ next_cursor: "page-2" });
    server.use(
      http.get(/.*\/api\/news\/feed$/, ({ request }) => {
        const cursor = new URL(request.url).searchParams.get("cursor");
        if (!cursor) return HttpResponse.json({ ok: true, data: feed });
        return HttpResponse.json({
          ok: true,
          data: newsFeedFixture({
            events: [
              newsFeedEventFixture({
                event_id: "evt-loaded-tail",
                triage: {
                  ...newsFeedEventFixture().triage!,
                  headline_zh: "Loaded non-deferred tail event",
                },
              }),
            ],
            next_cursor: null,
          }),
        });
      }),
    );
    const rendered = renderNews(<NewsPage token="test-token" view="feed" />);
    const currentTitle = await screen.findByText(/央行政策转向，风险资产承压/);
    const currentRow = currentTitle.closest("article");
    const scrollContainer = rendered.container.querySelector<HTMLElement>(".center-column");
    expect(scrollContainer).not.toBeNull();
    scrollContainer!.scrollTop = 320;
    fireEvent.scroll(scrollContainer!);

    feed = newsFeedFixture({
      events: [
        newsFeedEventFixture({
          event_id: "evt-deferred-at-top",
          triage: { ...newsFeedEventFixture().triage!, headline_zh: "Deferred high-signal event" },
        }),
        ...newsFeedFixture().events,
      ],
      next_cursor: "page-2",
    });
    await rendered.queryClient.invalidateQueries({ queryKey: ["news-feed"] });

    const notice = await screen.findByRole("button", { name: "1 条新事件 · 回到顶部" });
    expect(screen.queryByText("Deferred high-signal event")).not.toBeInTheDocument();
    expect(screen.getByText(/央行政策转向，风险资产承压/).closest("article")).toBe(currentRow);

    fireEvent.click(screen.getByRole("button", { name: "加载更多事件" }));

    expect(await screen.findByText("Loaded non-deferred tail event")).toBeInTheDocument();
    expect(screen.queryByText("Deferred high-signal event")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "1 条新事件 · 回到顶部" })).toBeInTheDocument();

    fireEvent.click(notice);

    expect(await screen.findByText("Deferred high-signal event")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /条新事件/ })).not.toBeInTheDocument();
  });

  it("renders the approved five-stage 24 h funnel on the Feed header", async () => {
    renderNews(<NewsPage token="test-token" view="feed" />);

    const funnel = await screen.findByRole("region", { name: "过去 24 小时漏斗" });
    /*
     * A permanent green "流水线正常" beside a feed is a light the reader learns to stop seeing. The sidebar
     * carries a dot for the status destination and the status route carries the full read; the pill appears
     * here only when a level is not `ok` (see the next test).
     */
    expect(screen.queryByRole("link", { name: "查看流水线状态" })).toBeNull();
    expect(within(funnel).getByLabelText("24 小时漏斗").textContent).toBe(
      // Five counts and the share each came from. #87: 符号落表 is measured against the Events that
      // *carried* a tag, never against the tagless macro headlines that never offered a symbol.
      "RECEIVED320采集PARSED320已解析100%ADMITTED180过门禁56%JUDGED175已审稿97%PUSHED41已推送13%",
    );
  });

  it("shows no health pill of its own even when the model degrades", async () => {
    // #207: health is the topbar lamp's, in one place and on every route. Two pills on one screen saying the
    // same thing is one of them the reader learns to skip — `CockpitTopbarHealth.test.tsx` covers the lamp.
    server.use(
      http.get(/.*\/api\/news\/status$/, () =>
        HttpResponse.json({
          ok: true,
          data: newsStatusFixture({
            health: {
              ...newsStatusFixture().health,
              model: {
                detail_zh: "模型输出被截断 30",
                level: "bad",
                summary_zh: "24 小时降级率 20%（30/150）",
              },
              overall: "bad",
            },
          }),
        }),
      ),
    );
    renderNews(<NewsPage token="test-token" view="feed" />);
    await screen.findByRole("heading", { name: "新闻事件流" });
    await waitFor(() => expect(screen.getByText("采集")).toBeInTheDocument());
    expect(screen.queryByRole("link", { name: "查看流水线状态" })).not.toBeInTheDocument();
    expect(screen.queryByText("流水线异常")).not.toBeInTheDocument();
  });

  // ------------------------------------------------------------------ status
  it("renders four thresholded health cards, the funnel, and named reasons", async () => {
    server.use(
      http.get(/.*\/api\/news\/status$/, () =>
        HttpResponse.json({
          ok: true,
          data: newsStatusFixture({
            health: {
              ...newsStatusFixture().health,
              delivery: {
                detail_zh: "feishu_http_500",
                level: "warn",
                summary_zh: "24 小时 10/100 条未送达",
              },
              overall: "warn",
            },
          }),
        }),
      ),
    );

    renderNews(<NewsPage token="test-token" view="status" />, "/news/status");

    expect(await screen.findByRole("heading", { name: "流水线状态" })).toBeInTheDocument();
    expect(await screen.findByText("总体注意")).toBeInTheDocument();
    const cards = document.querySelectorAll(".news-health-card");
    expect(cards).toHaveLength(4);
    expect(Array.from(cards).map((card) => card.getAttribute("data-level"))).toEqual([
      "ok",
      "ok",
      "ok",
      "warn",
    ]);
    expect(cards[3]).toHaveTextContent("Delivery注意");
    expect(cards[3]).toHaveTextContent("24 小时 10/100 条未送达");
    expect(cards[3]).toHaveTextContent("feishu_http_500");
    expect(cards[2]).toHaveTextContent("模型正常，24 小时降级 2/175");

    const funnel = screen.getByRole("region", { name: "过去 24 小时漏斗" });
    expect(within(funnel).getByRole("link", { name: /收到/ })).toHaveAttribute("href", "/news");
    expect(within(funnel).getByRole("link", { name: /已送达/ })).toHaveAttribute(
      "href",
      "/news?outcome=pushed",
    );
    expect(within(funnel).getByText("320")).toBeInTheDocument();

    const reasons = screen.getByRole("region", { name: "拦截与推送原因" });
    expect(within(reasons).getByText("模型判定为噪音")).toBeInTheDocument();
    expect(within(reasons).getByText("「中东与能源」话题 4 小时内已推 3 条")).toBeInTheDocument();
    expect(within(reasons).getByText("律所推广模板，规则直接拦截")).toBeInTheDocument();
    expect(
      within(reasons).queryByText("storyline:theme:mideast_energy:cap3"),
    ).not.toBeInTheDocument();
    expect(within(reasons).queryByText("suppressed_pr_template")).not.toBeInTheDocument();

    // The pause/mute control panel went with `news_control_state`: it never withheld a card, so the
    // console has nothing to show and no button that would be a second writer.
    expect(screen.queryByRole("region", { name: "控制状态" })).not.toBeInTheDocument();
    const watch = screen.getByRole("region", { name: "关注列表" });
    expect(within(watch).getByText("BTC")).toBeInTheDocument();
    // #126: which Strategies feed News is provider account configuration. The console says nothing about
    // them — no IDs, no count, no section — because Tracefold neither chooses nor filters them.
    expect(screen.queryByRole("region", { name: /策略/ })).toBeNull();
    expect(document.body.textContent).not.toContain("Strategy");
    expect(document.body.textContent).not.toContain("1018");
    // Raw metrics stay available, but folded away.
    const technical = screen.getByText(/技术指标/).closest("details")!;
    expect(technical).not.toHaveAttribute("open");
    expect(within(technical).getByText("triage_p95_ms")).toBeInTheDocument();
    expect(within(technical).getByText("学习证据保留")).toBeInTheDocument();
    expect(within(technical).getByText("deleted_last_turn")).toBeInTheDocument();
  });

  // ------------------------------------------------------------------ detail
  it("renders Event detail as one conclusion, a timeline, members, and folded technical details", async () => {
    renderNews(
      <NewsPage eventId="evt-global-policy" token="test-token" view="event" />,
      "/news/events/evt-global-policy",
    );

    const region = await screen.findByRole("region", { name: "新闻事件详情" });
    expect(screen.getByRole("link", { name: "返回新闻事件流" })).toHaveAttribute("href", "/news");
    await screen.findByRole("heading", { level: 1, name: "央行政策转向，风险资产承压" });
    // The conclusion and its reason are siblings in the hero, not one capsule: the chip is the verdict and
    // the sentence beside it is the server's reason for it.
    const heroState = region.querySelector(".news-detail-hero-state")!;
    expect(heroState.querySelector(".news-outcome")).toHaveTextContent("已推送");
    expect(heroState).toHaveTextContent("模型判断值得推送");
    expect(
      screen.getByRole("heading", { level: 1, name: "央行政策转向，风险资产承压" }),
    ).toBeInTheDocument();
    expect(screen.getByText("利率指引与市场预期背离，风险资产定价需要重估")).toBeInTheDocument();
    // #256: the judgments are still served and still shown; the door into the retired ReviewDesk is not.
    expect(screen.getByRole("heading", { name: "人工复盘" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "在学习复盘中打开" })).toBeNull();
    const hero = region.querySelector<HTMLElement>(".news-detail-hero")!;
    expect(
      within(hero).getByText("Central banks respond to a new global policy shock"),
    ).toBeInTheDocument();

    const timeline = screen.getByRole("region", { name: "处理时间线" });
    const steps = within(timeline).getAllByRole("listitem");
    expect(steps.map((step) => step.getAttribute("data-stage"))).toEqual([
      "received",
      "gate",
      "triage",
      "decide",
      "delivery",
    ]);
    expect(steps[0]).toHaveTextContent("来源 Reuters World · 归并 4 条同类报道（2 个来源）");
    expect(steps[3]).toHaveTextContent("推送 · 模型判断值得推送");
    expect(within(steps[3]).queryByText("model_push_actionable")).not.toBeInTheDocument();
    fireEvent.click(within(steps[3]).getByRole("button", { name: /展开字段/ }));
    expect(within(steps[3]).getByText("override_rule")).toBeInTheDocument();
    expect(within(steps[3]).getByText("model_push_actionable")).toBeInTheDocument();

    const members = screen.getByRole("region", { name: "同类报道" });
    expect(within(members).getAllByRole("listitem")).toHaveLength(2);
    expect(
      within(members).getByText("Central banks scramble after policy shock"),
    ).toBeInTheDocument();
    expect(within(members).queryByText(/0\.71/)).not.toBeInTheDocument();

    expect(screen.queryByRole("region", { name: "运营标注" })).not.toBeInTheDocument();

    const technical = screen.getByText(/技术详情/).closest("details")!;
    expect(technical).not.toHaveAttribute("open");
    expect(within(technical).getByText("storyline_key")).toBeInTheDocument();
    expect(within(technical).getByText("asset:BTC")).toBeInTheDocument();
    expect(within(technical).getByText("news_triage_policy_v1")).toBeInTheDocument();
    // Internal identifiers do not leak into the first screen.
    expect(within(hero).queryByText(/asset:BTC|evt-global-policy|jaccard/)).toBeNull();
  });

  it("hides same-name non-primary market candidates from Event evidence", async () => {
    server.use(
      http.get(/.*\/api\/news\/events\/evt-global-policy$/, () =>
        HttpResponse.json({
          ok: true,
          data: newsEventDetailFixture({
            reaction: null,
            reactions: [
              newsEventReactionFixture({ is_primary: true, symbol: "NVDA" }),
              newsEventReactionFixture({
                is_primary: false,
                symbol: "GLM",
                venue_symbol: "GLMUSDT",
              }),
            ],
          }),
        }),
      ),
    );
    renderNews(
      <NewsPage eventId="evt-global-policy" token="test-token" view="event" />,
      "/news/events/evt-global-policy",
    );

    await screen.findByRole("heading", { level: 1, name: "央行政策转向，风险资产承压" });
    expect(screen.getByText("NVDA")).toBeInTheDocument();
    expect(screen.queryByText("GLM")).not.toBeInTheDocument();
    expect(screen.getByText(/已隐藏 1 个同名但非主标的/)).toBeInTheDocument();
  });

  it("explains a degraded, throttled, and failed-delivery Event without inventing state", async () => {
    server.use(
      http.get(/.*\/api\/news\/events\/evt-global-policy$/, () =>
        HttpResponse.json({
          ok: true,
          data: newsEventDetailFixture({
            deliveries: [
              newsDeliveryFixture({
                error_code: "news_delivery_failed:FeishuServerError",
                settled_at_ms: NEWS_NOW_MS - 10_000,
                state: "terminal",
              }),
            ],
            outcome: newsOutcomeFixture({
              group: "held",
              kind: "delivery_failed",
              reason_zh: "飞书发送失败（FeishuServerError）",
              text_zh: "未送达",
            }),
            timeline: [
              ...newsEventDetailFixture().timeline!.slice(0, 2),
              {
                at_ms: NEWS_NOW_MS - 90_000,
                facts: { degraded: true, error_code: "news_triage_output_truncated" },
                stage: "triage",
                summary_zh: "模型不可用：模型输出被截断，按规则兜底",
                title_zh: "审稿",
              },
              {
                at_ms: NEWS_NOW_MS - 90_000,
                facts: { final_decision: "push", override_rule: "fail_closed_fallback" },
                stage: "decide",
                summary_zh: "推送 · 模型不可用，按规则兜底",
                title_zh: "决策",
              },
              {
                at_ms: NEWS_NOW_MS - 10_000,
                facts: {
                  error_code: "news_delivery_failed:FeishuServerError",
                  kind: "first",
                  state: "terminal",
                },
                stage: "delivery",
                summary_zh: "未送达：飞书发送失败（FeishuServerError）",
                title_zh: "推送",
              },
            ],
            verdicts: [
              newsVerdictFixture({
                degraded: true,
                error_code: "news_triage_output_truncated",
                final_decision: "push",
                model_decision: null,
                override_rule: "fail_closed_fallback",
              }),
            ],
          }),
        }),
      ),
    );

    renderNews(
      <NewsPage eventId="evt-global-policy" token="test-token" view="event" />,
      "/news/events/evt-global-policy",
    );

    const region = await screen.findByRole("region", { name: "新闻事件详情" });
    await screen.findByText("未送达");
    const badge = region.querySelector(".news-outcome")!;
    expect(badge).toHaveTextContent("未送达");
    expect(badge.closest(".news-detail-hero-state")).toHaveTextContent(
      "飞书发送失败（FeishuServerError）",
    );
    expect(badge).toHaveAttribute("data-tone", "alert");
    const timeline = screen.getByRole("region", { name: "处理时间线" });
    expect(
      within(timeline).getByText("模型不可用：模型输出被截断，按规则兜底"),
    ).toBeInTheDocument();
    expect(
      within(timeline).getByText("未送达：飞书发送失败（FeishuServerError）"),
    ).toBeInTheDocument();
    const technical = screen.getByText(/技术详情/).closest("details")!;
    expect(within(technical).getByText("news_triage_output_truncated")).toBeInTheDocument();
    expect(
      within(technical).getByText("news_delivery_failed:FeishuServerError"),
    ).toBeInTheDocument();
  });
});

function renderNews(node: ReactNode, path = "/news") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const rendered = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <div className="center-column">
          {node}
          <LocationProbe />
        </div>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...rendered, queryClient };
}

function LocationProbe() {
  const location = useLocation();
  return <span data-testid="location">{`${location.pathname}${location.search}`}</span>;
}
