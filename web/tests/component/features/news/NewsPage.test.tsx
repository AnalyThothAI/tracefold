import { NewsPage } from "@features/news";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import {
  NEWS_NOW_MS,
  newsDeliveryFixture,
  newsEventDetailFixture,
  newsFeedEventFixture,
  newsFeedFixture,
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

  it("defaults to the latest Event feed with 25 rows and no hidden filters", async () => {
    const observed: Record<string, string | null> = {};
    server.use(
      http.get(/.*\/api\/news\/feed$/, ({ request }) => {
        const params = new URL(request.url).searchParams;
        for (const name of ["limit", "sort", "priority", "decision", "family", "admission"]) {
          observed[name] = params.get(name);
        }
        return HttpResponse.json({ ok: true, data: newsFeedFixture() });
      }),
    );

    renderNews(<NewsPage token="test-token" view="feed" />);

    expect(await screen.findByRole("heading", { name: "新闻事件流" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "事件流" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "状态" })).not.toHaveAttribute("aria-current");
    expect(screen.queryByRole("link", { name: "公共全球简报" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "来源" })).not.toBeInTheDocument();
    expect(observed.sort).toBe("latest");
    expect(observed.limit).toBe("25");
    expect(observed.priority).toBeNull();
    expect(observed.decision).toBeNull();
    expect(observed.family).toBeNull();
    expect(observed.admission).toBeNull();
  });

  it("renders one Event row with server-owned triage, delivery, admission, and asset facts", async () => {
    renderNews(<NewsPage token="test-token" view="feed" />);

    const title = await screen.findByRole("heading", { name: "央行应对新的全球政策冲击" });
    const row = title.closest("article");
    expect(row).not.toBeNull();
    expect(row).toHaveAttribute("data-priority", "high");
    expect(row).toHaveAttribute("data-decision", "push");
    expect(row).toHaveAttribute("data-direction", "bearish");
    expect(title.closest(".news-event-title")).toHaveAttribute(
      "href",
      "/news/events/evt-global-policy",
    );
    const scoped = within(row as HTMLElement);
    expect(scoped.getByText("高优先")).toBeInTheDocument();
    expect(scoped.getByText("推送")).toBeInTheDocument();
    expect(scoped.getByText("候选")).toBeInTheDocument();
    expect(scoped.getByText("综合")).toBeInTheDocument();
    expect(scoped.getByText("加密")).toBeInTheDocument();
    expect(scoped.getByLabelText("OpenNews 分数 88")).toHaveTextContent("OpenNews88");
    expect(scoped.getByText("Reuters World")).toBeInTheDocument();
    expect(scoped.getByText("4 条报道")).toBeInTheDocument();
    expect(scoped.getByText("原标题").parentElement).toHaveTextContent(
      "Central banks respond to a new global policy shock",
    );
    expect(scoped.getByText("看空 · M2 · macro")).toBeInTheDocument();
    expect(scoped.getByText("央行政策转向，风险资产承压")).toBeInTheDocument();
    expect(scoped.getByLabelText("推送状态 已发送")).toBeInTheDocument();
    const assets = scoped.getByText("落地资产").parentElement;
    expect(
      within(assets as HTMLElement)
        .getAllByRole("listitem")
        .map((item) => item.textContent),
    ).toEqual(["BTC", "ETH"]);
    expect(within(assets as HTMLElement).getByText("BTC")).toHaveAttribute("data-watch", "hit");
    expect(scoped.getByRole("link", { name: /查看原文/ })).toHaveAttribute(
      "href",
      "https://www.reuters.com/world/story",
    );
  });

  it("marks untriaged Events as pending and omits absent delivery, assets, and links", async () => {
    server.use(
      http.get(/.*\/api\/news\/feed$/, () =>
        HttpResponse.json({
          ok: true,
          data: newsFeedFixture({
            events: [
              newsFeedEventFixture({
                delivery: null,
                display_title: "",
                grounded_assets: [],
                leader_url: null,
                priority: "normal",
                provider_score_max: null,
                triage: null,
                watchlist_hits: [],
              }),
            ],
          }),
        }),
      ),
    );

    renderNews(<NewsPage token="test-token" view="feed" />);

    const title = await screen.findByRole("heading", {
      name: "Central banks respond to a new global policy shock",
    });
    const row = title.closest("article") as HTMLElement;
    expect(row).toHaveAttribute("data-decision", "none");
    expect(within(row).getByText("待判定")).toBeInTheDocument();
    expect(within(row).getByText("未落地")).toBeInTheDocument();
    expect(within(row).queryByLabelText(/OpenNews 分数/)).not.toBeInTheDocument();
    expect(within(row).queryByLabelText(/推送状态/)).not.toBeInTheDocument();
    expect(within(row).queryByRole("link", { name: /查看原文/ })).not.toBeInTheDocument();
  });

  it("keeps a 668-character feed headline accessible inside the compact density surface", async () => {
    const longTitle = "长".repeat(668);
    server.use(
      http.get(/.*\/api\/news\/feed$/, () =>
        HttpResponse.json({
          ok: true,
          data: newsFeedFixture({
            events: [newsFeedEventFixture({ display_title: longTitle, leader_title: longTitle })],
          }),
        }),
      ),
    );

    renderNews(<NewsPage token="test-token" view="feed" />);

    const surface = await screen.findByRole("region", { name: "新闻事件流" });
    expect(surface).toHaveAttribute("data-feed-density", "compact");
    const heading = await within(surface).findByRole("heading", { name: longTitle });
    expect(heading).toHaveTextContent(longTitle);
  });

  it("keeps search, family, admission, priority, decision, symbol, and sort in URL-owned server state", async () => {
    const observed: Record<string, string | null> = {};
    server.use(
      http.get(/.*\/api\/news\/feed$/, ({ request }) => {
        const params = new URL(request.url).searchParams;
        for (const name of ["q", "family", "admission", "priority", "decision", "symbol", "sort"]) {
          observed[name] = params.get(name);
        }
        return HttpResponse.json({ ok: true, data: newsFeedFixture() });
      }),
    );

    renderNews(
      <NewsPage token="test-token" view="feed" />,
      "/news?q=bitcoin&family=general&admission=candidate&priority=high&decision=push&symbol=btc&sort=priority",
    );

    await screen.findByRole("heading", { name: "央行应对新的全球政策冲击" });
    await waitFor(() => expect(observed.symbol).toBe("BTC"));
    expect(observed).toEqual({
      admission: "candidate",
      decision: "push",
      family: "general",
      priority: "high",
      q: "bitcoin",
      sort: "priority",
      symbol: "BTC",
    });
    expect(screen.getByRole("combobox", { name: "事件排序" })).toHaveValue("priority");
    expect(screen.getByRole("combobox", { name: "事件家族" })).toHaveValue("general");
    expect(screen.getByRole("combobox", { name: "事件准入" })).toHaveValue("candidate");
    expect(screen.getByRole("combobox", { name: "事件优先级" })).toHaveValue("high");
    expect(screen.getByRole("combobox", { name: "Triage 判定" })).toHaveValue("push");
    expect(screen.getByRole("textbox", { name: "落地资产" })).toHaveValue("BTC");
    const chips = screen.getByRole("group", { name: "已启用筛选" });
    expect(
      within(chips)
        .getAllByRole("button")
        .map((chip) => chip.textContent),
    ).toEqual([
      "搜索：bitcoin×",
      "家族：综合×",
      "准入：候选×",
      "优先级：高优先×",
      "判定：推送×",
      "资产：BTC×",
    ]);

    fireEvent.click(within(chips).getByRole("button", { name: "移除判定：推送" }));
    await waitFor(() => expect(observed.decision).toBeNull());
    expect(screen.getByTestId("location")).toHaveTextContent("priority=high");
    expect(screen.getByTestId("location")).not.toHaveTextContent("decision=");

    fireEvent.change(screen.getByRole("combobox", { name: "事件优先级" }), {
      target: { value: "" },
    });
    await waitFor(() => expect(observed.priority).toBeNull());
    fireEvent.change(screen.getByRole("combobox", { name: "事件排序" }), {
      target: { value: "latest" },
    });
    await waitFor(() => expect(observed.sort).toBe("latest"));
  });

  it("ignores unknown priority and decision values instead of forwarding them", async () => {
    const observed: Record<string, string | null> = {};
    server.use(
      http.get(/.*\/api\/news\/feed$/, ({ request }) => {
        const params = new URL(request.url).searchParams;
        observed.priority = params.get("priority");
        observed.decision = params.get("decision");
        return HttpResponse.json({ ok: true, data: newsFeedFixture() });
      }),
    );

    renderNews(<NewsPage token="test-token" view="feed" />, "/news?priority=urgent&decision=maybe");

    await screen.findByRole("heading", { name: "央行应对新的全球政策冲击" });
    expect(observed.priority).toBeNull();
    expect(observed.decision).toBeNull();
    expect(screen.queryByRole("group", { name: "已启用筛选" })).not.toBeInTheDocument();
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
                display_title: "Second page Event",
                event_id: "evt-second-page",
              }),
            ],
            next_cursor: null,
          }),
        });
      }),
    );
    renderNews(<NewsPage token="test-token" view="feed" />);
    await screen.findByText("央行应对新的全球政策冲击");

    fireEvent.click(screen.getByRole("button", { name: "加载更多事件" }));

    expect(await screen.findByText("Second page Event")).toBeInTheDocument();
    expect(screen.getAllByText("央行应对新的全球政策冲击")).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "加载更多事件" })).not.toBeInTheDocument();
  });

  it("shows new first-page events immediately when the reader is at the top", async () => {
    let feed = newsFeedFixture();
    server.use(http.get(/.*\/api\/news\/feed$/, () => HttpResponse.json({ ok: true, data: feed })));
    const rendered = renderNews(<NewsPage token="test-token" view="feed" />);
    await screen.findByText(feed.events[0].display_title);

    feed = newsFeedFixture({
      events: [
        newsFeedEventFixture({
          display_title: "New high-signal event at the top",
          event_id: "evt-new-at-top",
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
                display_title: "Loaded non-deferred tail event",
                event_id: "evt-loaded-tail",
              }),
            ],
            next_cursor: null,
          }),
        });
      }),
    );
    const rendered = renderNews(<NewsPage token="test-token" view="feed" />);
    const currentTitle = await screen.findByText(feed.events[0].display_title);
    const currentRow = currentTitle.closest("article");
    const scrollContainer = rendered.container.querySelector<HTMLElement>(".center-column");
    expect(scrollContainer).not.toBeNull();
    scrollContainer!.scrollTop = 320;
    fireEvent.scroll(scrollContainer!);

    feed = newsFeedFixture({
      events: [
        newsFeedEventFixture({
          display_title: "Deferred high-signal event",
          event_id: "evt-deferred-at-top",
        }),
        ...newsFeedFixture().events,
      ],
      next_cursor: "page-2",
    });
    await rendered.queryClient.invalidateQueries({ queryKey: ["news-feed"] });

    const notice = await screen.findByRole("button", { name: "1 条新事件 · 回到顶部" });
    expect(screen.queryByText("Deferred high-signal event")).not.toBeInTheDocument();
    expect(screen.getByText(newsFeedFixture().events[0].display_title).closest("article")).toBe(
      currentRow,
    );

    fireEvent.click(screen.getByRole("button", { name: "加载更多事件" }));

    expect(await screen.findByText("Loaded non-deferred tail event")).toBeInTheDocument();
    expect(screen.queryByText("Deferred high-signal event")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "1 条新事件 · 回到顶部" })).toBeInTheDocument();

    fireEvent.click(notice);

    expect(await screen.findByText("Deferred high-signal event")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /条新事件/ })).not.toBeInTheDocument();
  });

  it("keeps compact reader-facing pipeline health inline on the Feed header", async () => {
    renderNews(<NewsPage token="test-token" view="feed" />);

    const status = await screen.findByRole("status");
    await waitFor(() => expect(status).toHaveTextContent("WSS 已连接"));
    expect(status).toHaveAttribute("data-state", "live");
    expect(status).toHaveTextContent("1h 事件 12 · Triage P95 1.9 s · 24h 推送 41");
  });

  it("reports a lost WSS connection as stalled and a degraded pipeline as recovering", async () => {
    server.use(
      http.get(/.*\/api\/news\/status$/, () =>
        HttpResponse.json({
          ok: true,
          data: newsStatusFixture({
            ingest: { ...newsStatusFixture().ingest, connected: false },
            state: "degraded",
          }),
        }),
      ),
    );
    const rendered = renderNews(<NewsPage token="test-token" view="feed" />);
    const status = await screen.findByRole("status");
    await waitFor(() => expect(status).toHaveTextContent("WSS 未连接"));
    expect(status).toHaveAttribute("data-state", "stalled");

    server.use(
      http.get(/.*\/api\/news\/status$/, () =>
        HttpResponse.json({ ok: true, data: newsStatusFixture({ state: "degraded" }) }),
      ),
    );
    await rendered.queryClient.invalidateQueries({ queryKey: ["news-status"] });
    await waitFor(() => expect(status).toHaveTextContent("流水线降级"));
    expect(status).toHaveAttribute("data-state", "recovering");
  });

  it("renders the four status layers plus read-only control and watch views", async () => {
    server.use(
      http.get(/.*\/api\/news\/status$/, () =>
        HttpResponse.json({
          ok: true,
          data: newsStatusFixture({
            control: {
              mutes: [{ symbol: "DOGE", until_ms: NEWS_NOW_MS + 3_600_000 }],
              paused: true,
            },
            ingest: {
              ...newsStatusFixture().ingest,
              open_incidents: [
                {
                  cause_class: "provider_close",
                  incident_id: 7,
                  opened_at_ms: NEWS_NOW_MS - 600_000,
                  planned: false,
                },
              ],
              strategy_warnings: ["strategy 1019 disabled upstream"],
            },
          }),
        }),
      ),
    );

    renderNews(<NewsPage token="test-token" view="status" />, "/news/status");

    expect(await screen.findByRole("heading", { name: "新闻运行状态" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "状态" })).toHaveAttribute("aria-current", "page");
    expect(await screen.findAllByText("运行正常")).toHaveLength(2);
    for (const layer of [
      "接入 · OpenNews WSS",
      "代理 · RabbitMQ",
      "流水线 · Triage / Analyst",
      "推送 · 飞书",
    ]) {
      expect(screen.getByRole("heading", { name: layer })).toBeInTheDocument();
    }
    expect(screen.getByText("队列 news.triage").nextElementSibling).toHaveTextContent(
      "0 消息 · 4 消费者",
    );
    expect(screen.getByText("Triage P95").nextElementSibling).toHaveTextContent("1.9 s");
    expect(screen.getByText("24h 已发送").nextElementSibling).toHaveTextContent("41");
    expect(screen.getByText("小时上限").nextElementSibling).toHaveTextContent("12");
    expect(screen.getByRole("list", { name: "状态原因" })).toHaveTextContent(
      "strategy 1019 disabled upstream",
    );
    expect(screen.getByText("未结 WSS 事故 · 1")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "控制（只读）" })).toBeInTheDocument();
    expect(screen.getByText("已暂停")).toBeInTheDocument();
    expect(screen.getByText(/"symbol":"DOGE"/)).toBeInTheDocument();
    const watchHeading = screen.getByRole("heading", { name: "关注名单（只读）" });
    const watchCard = watchHeading.closest("article") as HTMLElement;
    expect(
      within(watchCard)
        .getAllByRole("listitem")
        .map((item) => item.textContent),
    ).toEqual(["BTC", "ETH", "SOL"]);
    expect(screen.queryByRole("button", { name: /暂停|恢复|静音/ })).not.toBeInTheDocument();
  });

  it("renders Event detail with members, verdicts, deliveries, presentation, and marks", async () => {
    renderNews(
      <NewsPage eventId="evt-global-policy" token="test-token" view="event" />,
      "/news/events/evt-global-policy",
    );

    expect(
      await screen.findByRole("heading", { level: 1, name: "央行应对新的全球政策冲击" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "新闻事件详情" })).toHaveAttribute(
      "data-page-archetype",
      "case",
    );
    expect(screen.getByRole("link", { name: "返回新闻事件流" })).toHaveAttribute("href", "/news");
    expect(screen.getByText("原标题").parentElement).toHaveTextContent(
      "Central banks respond to a new global policy shock",
    );
    expect(screen.getByText("Storyline key").nextElementSibling).toHaveTextContent("asset:BTC");
    expect(screen.getByRole("link", { name: /阅读代表原文/ })).toHaveAttribute(
      "href",
      "https://www.reuters.com/world/story",
    );

    const members = screen.getByRole("region", { name: "4 条报道" });
    expect(
      within(members)
        .getAllByRole("heading", { level: 3 })
        .map((h) => h.textContent),
    ).toEqual([
      "Central banks respond to a new global policy shock",
      "Central banks scramble after policy shock",
    ]);
    expect(within(members).getByText("Jaccard 0.710")).toBeInTheDocument();

    const verdicts = screen.getByRole("region", { name: "判定记录" });
    expect(within(verdicts).getByText("Triage")).toBeInTheDocument();
    expect(within(verdicts).getByText("最终 推送")).toBeInTheDocument();
    expect(within(verdicts).getByText("规则基线").nextElementSibling).toHaveTextContent("升级");
    expect(within(verdicts).getByText("模型判定").nextElementSibling).toHaveTextContent("推送");
    expect(within(verdicts).getByText("方向").nextElementSibling).toHaveTextContent("看空");
    expect(within(verdicts).getByText("量级").nextElementSibling).toHaveTextContent("M2");
    expect(within(verdicts).getByText("置信度").nextElementSibling).toHaveTextContent("82%");
    expect(within(verdicts).getByText("央行政策转向，风险资产承压")).toBeInTheDocument();
    expect(within(verdicts).getByText("trace · 2")).toBeInTheDocument();
    expect(within(verdicts).getByRole("list", { name: "判定资产" })).toHaveTextContent("BTC");

    const deliveries = screen.getByRole("region", { name: "推送记录" });
    expect(within(deliveries).getByText("first")).toBeInTheDocument();
    expect(within(deliveries).getByText("已发送")).toBeInTheDocument();
    expect(within(deliveries).getByText("回执 message_id").nextElementSibling).toHaveTextContent(
      "om_123",
    );

    const presentation = screen.getByRole("region", { name: "标题呈现" });
    expect(within(presentation).getByText("呈现结果").nextElementSibling).toHaveTextContent(
      "translated",
    );
    expect(within(presentation).getByText("提供方").nextElementSibling).toHaveTextContent("deepl");

    const marks = screen.getByRole("region", { name: "市场标记" });
    const marksTable = within(marks).getByRole("table");
    expect(within(marksTable).getByText("t0")).toBeInTheDocument();
    expect(within(marksTable).getByText("64,250.5")).toBeInTheDocument();
    expect(within(marksTable).getAllByText("—")).toHaveLength(2);
  });

  it("shows degraded, throttled, and failed verdict/delivery facts without inventing state", async () => {
    server.use(
      http.get(/.*\/api\/news\/events\/evt-global-policy$/, () =>
        HttpResponse.json({
          ok: true,
          data: newsEventDetailFixture({
            deliveries: [
              newsDeliveryFixture({
                error_code: "feishu_5xx",
                receipt: null,
                settled_at_ms: NEWS_NOW_MS - 10_000,
                state: "terminal",
              }),
            ],
            marks: [],
            presentation: null,
            verdicts: [
              newsVerdictFixture({
                degraded: true,
                error_code: "model_timeout",
                final_decision: "throttled",
                model_decision: null,
                override_rule: "storyline_window_max",
                throttled_by: "evt-earlier",
                verdict: {},
              }),
              newsVerdictFixture({
                created_at_ms: NEWS_NOW_MS - 30_000,
                final_decision: "push",
                policy_version: "news_analyst_policy_v1",
                stage: "deep",
                verdict: {
                  agrees_with_triage: false,
                  confidence: 0.6,
                  context_evidence: ["prior BTC OI spike 3h ago"],
                  follow_up_needed: true,
                  novelty_assessment: "followup",
                  revised_direction: "neutral",
                  revised_magnitude: 1,
                  risks_zh: "流动性数据滞后。",
                  thesis_zh: "市场已部分定价。",
                },
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

    const verdicts = await screen.findByRole("region", { name: "判定记录" });
    const cards = within(verdicts).getAllByRole("article");
    expect(cards).toHaveLength(2);
    const triage = within(cards[0]);
    expect(triage.getByText("最终 节流")).toBeInTheDocument();
    expect(triage.getByText("降级")).toBeInTheDocument();
    expect(triage.getByText("model_timeout")).toBeInTheDocument();
    expect(triage.getByText("覆写规则").nextElementSibling).toHaveTextContent(
      "storyline_window_max",
    );
    expect(triage.getByText("节流来源").nextElementSibling).toHaveTextContent("evt-earlier");
    expect(triage.getByText("模型判定").nextElementSibling).toHaveTextContent("无");
    const analyst = within(cards[1]);
    expect(analyst.getByText("Analyst")).toBeInTheDocument();
    expect(analyst.getByText("市场已部分定价。")).toBeInTheDocument();
    expect(analyst.getByText("流动性数据滞后。")).toBeInTheDocument();
    expect(analyst.getByText("同意 Triage").nextElementSibling).toHaveTextContent("否");
    expect(analyst.getByText("修订方向").nextElementSibling).toHaveTextContent("中性");
    expect(analyst.getByText("上下文证据")).toBeInTheDocument();
    expect(analyst.getByText("prior BTC OI spike 3h ago")).toBeInTheDocument();

    const deliveries = screen.getByRole("region", { name: "推送记录" });
    expect(within(deliveries).getByText("已终结")).toBeInTheDocument();
    expect(within(deliveries).getByText("feishu_5xx")).toBeInTheDocument();
    expect(screen.getByText("尚无标题呈现；显示原标题。")).toBeInTheDocument();
    expect(screen.getByText("尚无市场标记。")).toBeInTheDocument();
    expect(
      screen.getByRole("heading", {
        level: 1,
        name: "Central banks respond to a new global policy shock",
      }),
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
