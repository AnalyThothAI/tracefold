import { NewsPage } from "@features/news";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import {
  NEWS_NOW_MS,
  newsDeliveryFixture,
  newsEventDetailFixture,
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
  it("defaults to the latest 24 h, every outcome, 25 rows, and no advanced filters", async () => {
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
        ]) {
          observed[name] = params.get(name);
        }
        return HttpResponse.json({ ok: true, data: newsFeedFixture() });
      }),
    );

    renderNews(<NewsPage token="test-token" view="feed" />);

    expect(await screen.findByRole("heading", { name: "新闻事件流" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "事件流" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "状态" })).not.toHaveAttribute("aria-current");
    const tabs = screen.getByRole("tablist", { name: "按结局筛选" });
    expect(
      within(tabs)
        .getAllByRole("tab")
        .map((tab) => tab.textContent),
    ).toEqual(["全部", "已推送", "被拦截", "处理中"]);
    expect(within(tabs).getByRole("tab", { name: "全部" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("combobox", { name: "时间范围" })).toHaveValue("24");
    await waitFor(() => expect(observed.hours).toBe("24"));
    expect(observed.sort).toBe("latest");
    expect(observed.limit).toBe("25");
    expect(observed.outcome).toBeNull();
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
    expect(inRow.getByText("4 条报道")).toBeInTheDocument();
    expect(inRow.getByText("利空 · 影响明显 · 宏观")).toBeInTheDocument();
    expect(inRow.getByLabelText("关联资产")).toHaveTextContent("BTCETH");
    expect(inRow.getByRole("link", { name: "打开原文" })).toHaveAttribute(
      "href",
      "https://www.reuters.com/world/story",
    );
    // Exactly one outcome badge; its reason travels as the title, not as a second label.
    const badges = row!.querySelectorAll(".news-outcome");
    expect(badges).toHaveLength(1);
    expect(badges[0]).toHaveTextContent("已推送");
    expect(badges[0]).toHaveAttribute("title", "模型判断值得推送");
    expect(inRow.getByText(/推送于/)).toBeInTheDocument();
    for (const raw of ["model_push_actionable", "candidate", "asset:BTC", "escalate", "general"]) {
      expect(inRow.queryByText(raw)).not.toBeInTheDocument();
    }
  });

  it("shows why a held Event was held, in Chinese, next to its single badge", async () => {
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
                  text_zh: "未推送（限流）",
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

    const throttled = (await screen.findByText("未推送（限流）")).closest("article")!;
    expect(throttled).toHaveAttribute("data-outcome-group", "held");
    expect(
      within(throttled).getByText("BTC 同一话题 2 小时内已推过同等或更重要的消息"),
    ).toBeInTheDocument();
    expect(within(throttled).queryByText("storyline:asset:BTC")).not.toBeInTheDocument();
    const recovery = screen.getByText("补抄件，不推送").closest("article")!;
    expect(within(recovery).getByText("断线期间补抄的旧闻，仅用于去重与历史")).toBeInTheDocument();
    expect(within(recovery).getByRole("heading", { name: "补抄回来的旧闻" })).toBeInTheDocument();
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
    expect(heading.closest("[data-feed-density='compact']")).not.toBeNull();
  });

  it("keeps outcome tab, time window, search, family, admission, priority, decision, symbol, and sort in URL-owned server state", async () => {
    const observed: Record<string, string | null> = {};
    server.use(
      http.get(/.*\/api\/news\/feed$/, ({ request }) => {
        const params = new URL(request.url).searchParams;
        for (const name of [
          "q",
          "family",
          "admission",
          "priority",
          "decision",
          "symbol",
          "sort",
          "outcome",
          "hours",
        ]) {
          observed[name] = params.get(name);
        }
        return HttpResponse.json({ ok: true, data: newsFeedFixture() });
      }),
    );

    renderNews(
      <NewsPage token="test-token" view="feed" />,
      "/news?q=bitcoin&family=general&admission=candidate&priority=high&decision=push&symbol=btc&sort=priority&outcome=held&hours=6",
    );

    await screen.findByRole("heading", { name: /央行政策转向，风险资产承压/ });
    await waitFor(() => expect(observed.symbol).toBe("BTC"));
    expect(observed).toEqual({
      admission: "candidate",
      decision: "push",
      family: "general",
      hours: "6",
      outcome: "held",
      priority: "high",
      q: "bitcoin",
      sort: "priority",
      symbol: "BTC",
    });
    expect(screen.getByRole("tab", { name: "被拦截" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("combobox", { name: "时间范围" })).toHaveValue("6");
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
      "搜索：bitcoin",
      "来源类别：综合",
      "门禁：已送审",
      "优先级：高优先级",
      "决策：推送",
      "资产：BTC",
    ]);

    fireEvent.click(within(chips).getByRole("button", { name: "移除决策：推送" }));
    await waitFor(() => expect(observed.decision).toBeNull());
    expect(screen.getByTestId("location")).toHaveTextContent("priority=high");
    expect(screen.getByTestId("location")).toHaveTextContent("outcome=held");
    expect(screen.getByTestId("location")).not.toHaveTextContent("decision=");

    fireEvent.click(screen.getByRole("tab", { name: "已推送" }));
    await waitFor(() => expect(observed.outcome).toBe("pushed"));
    fireEvent.click(screen.getByRole("tab", { name: "全部" }));
    await waitFor(() => expect(observed.outcome).toBeNull());
    fireEvent.change(screen.getByRole("combobox", { name: "时间范围" }), {
      target: { value: "all" },
    });
    await waitFor(() => expect(observed.hours).toBeNull());
    expect(screen.getByTestId("location")).toHaveTextContent("hours=all");
    fireEvent.change(screen.getByRole("combobox", { name: "事件优先级" }), {
      target: { value: "" },
    });
    await waitFor(() => expect(observed.priority).toBeNull());
    fireEvent.change(screen.getByRole("combobox", { name: "事件排序" }), {
      target: { value: "latest" },
    });
    await waitFor(() => expect(observed.sort).toBe("latest"));
  });

  it("ignores unknown priority, decision, outcome, and hours values instead of forwarding them", async () => {
    const observed: Record<string, string | null> = {};
    server.use(
      http.get(/.*\/api\/news\/feed$/, ({ request }) => {
        const params = new URL(request.url).searchParams;
        for (const name of ["priority", "decision", "outcome", "hours"])
          observed[name] = params.get(name);
        return HttpResponse.json({ ok: true, data: newsFeedFixture() });
      }),
    );

    renderNews(
      <NewsPage token="test-token" view="feed" />,
      "/news?priority=urgent&decision=maybe&outcome=whatever&hours=999",
    );

    await screen.findByRole("heading", { name: /央行政策转向，风险资产承压/ });
    expect(observed.priority).toBeNull();
    expect(observed.decision).toBeNull();
    expect(observed.outcome).toBeNull();
    expect(observed.hours).toBe("24");
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

  it("keeps a thresholded health pill and the 24 h funnel on the Feed header", async () => {
    renderNews(<NewsPage token="test-token" view="feed" />);

    const pill = await screen.findByRole("link", { name: "查看流水线状态" });
    expect(pill).toHaveTextContent("流水线正常");
    expect(pill).toHaveAttribute("data-tone", "positive");
    expect(pill).toHaveAttribute("href", "/news/status");
    const funnel = screen.getByRole("list", { name: "过去 24 小时漏斗" });
    expect(
      within(funnel)
        .getAllByRole("listitem")
        .map((item) => item.textContent),
    ).toEqual(["收到320最近 1 小时 12", "送审175候选 180", "决定推送40", "已送达41最近 1 小时 2"]);
  });

  it("turns the health pill red when the model degrades and names the failing item", async () => {
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
    const pill = await screen.findByRole("link", { name: "查看流水线状态" });
    await waitFor(() => expect(pill).toHaveTextContent("流水线异常"));
    expect(pill).toHaveAttribute("data-tone", "negative");
    expect(pill).toHaveTextContent("24 小时降级率 20%（30/150）");
  });

  // ------------------------------------------------------------------ status
  it("renders four thresholded health cards, the funnel, named reasons, and control state", async () => {
    server.use(
      http.get(/.*\/api\/news\/status$/, () =>
        HttpResponse.json({
          ok: true,
          data: newsStatusFixture({
            control: {
              mutes: [{ kind: "symbol", key: "DOGE", until_ms: NEWS_NOW_MS + 3_600_000 }],
              paused: true,
            },
            health: {
              ...newsStatusFixture().health,
              delivery: {
                detail_zh: "暂停期间应推的事件直接丢弃，不会补发",
                level: "warn",
                summary_zh: "推送已暂停",
              },
              overall: "warn",
            },
          }),
        }),
      ),
    );

    renderNews(<NewsPage token="test-token" view="status" />, "/news/status");

    expect(await screen.findByRole("heading", { name: "流水线状态" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "状态" })).toHaveAttribute("aria-current", "page");
    expect(await screen.findByText("总体注意")).toBeInTheDocument();
    const cards = document.querySelectorAll(".news-health-card");
    expect(cards).toHaveLength(4);
    expect(Array.from(cards).map((card) => card.getAttribute("data-level"))).toEqual([
      "ok",
      "ok",
      "ok",
      "warn",
    ]);
    expect(cards[3]).toHaveTextContent("推送已暂停");
    expect(cards[3]).toHaveTextContent("暂停期间应推的事件直接丢弃，不会补发");
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

    const control = screen.getByRole("region", { name: "控制状态" });
    expect(within(control).getByText("已暂停")).toBeInTheDocument();
    expect(within(control).getByRole("cell", { name: "DOGE" })).toBeInTheDocument();
    expect(within(control).queryByText(/until_ms/)).not.toBeInTheDocument();
    const watch = screen.getByRole("region", { name: "关注列表与策略" });
    expect(within(watch).getByText("BTC")).toBeInTheDocument();
    // Strategy IDs are private: the console shows counts only.
    expect(within(watch).getByText(/已配置 2 个/)).toBeInTheDocument();
    expect(within(watch).queryByText("1018")).not.toBeInTheDocument();
    expect(document.body.textContent).not.toContain("1018");
    // Raw metrics stay available, but folded away.
    const technical = screen.getByText(/技术指标/).closest("details")!;
    expect(technical).not.toHaveAttribute("open");
    expect(within(technical).getByText("triage_p95_ms")).toBeInTheDocument();
  });

  // ------------------------------------------------------------------ detail
  it("renders Event detail as one conclusion, a timeline, members, labels, and folded technical details", async () => {
    renderNews(
      <NewsPage eventId="evt-global-policy" token="test-token" view="event" />,
      "/news/events/evt-global-policy",
    );

    const region = await screen.findByRole("region", { name: "新闻事件详情" });
    expect(screen.getByRole("link", { name: "返回新闻事件流" })).toHaveAttribute("href", "/news");
    await screen.findByRole("heading", { level: 1, name: "央行政策转向，风险资产承压" });
    const badge = region.querySelector(".news-outcome")!;
    expect(badge).toHaveTextContent("已推送");
    expect(badge).toHaveTextContent("模型判断值得推送");
    expect(
      screen.getByRole("heading", { level: 1, name: "央行政策转向，风险资产承压" }),
    ).toBeInTheDocument();
    expect(screen.getByText("利率指引与市场预期背离，风险资产定价需要重估")).toBeInTheDocument();
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

    const labels = screen.getByRole("region", { name: "运营标注" });
    expect(within(labels).getByText("good")).toBeInTheDocument();
    expect(within(labels).getByText(/tracefold news label evt-global-policy/)).toBeInTheDocument();

    const technical = screen.getByText(/技术详情/).closest("details")!;
    expect(technical).not.toHaveAttribute("open");
    expect(within(technical).getByText("storyline_key")).toBeInTheDocument();
    expect(within(technical).getByText("asset:BTC")).toBeInTheDocument();
    expect(within(technical).getByText("news_triage_policy_v1")).toBeInTheDocument();
    // Internal identifiers do not leak into the first screen.
    expect(within(hero).queryByText(/asset:BTC|evt-global-policy|jaccard/)).toBeNull();
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
    expect(badge).toHaveTextContent("飞书发送失败（FeishuServerError）");
    expect(badge).toHaveAttribute("data-tone", "negative");
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
