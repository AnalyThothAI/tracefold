import { NewsPage } from "@features/news";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import {
  NEWS_NOW_MS,
  newsFeedFixture,
  newsGlobalBriefFixture,
  newsSourcesFixture,
  newsStatusFixture,
  newsStoryDetailFixture,
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
      http.get(/.*\/api\/news\/stories\/story-global-policy$/, () =>
        HttpResponse.json({ ok: true, data: newsStoryDetailFixture() }),
      ),
      http.get(/.*\/api\/news\/brief$/, () =>
        HttpResponse.json({ ok: true, data: newsGlobalBriefFixture() }),
      ),
      http.get(/.*\/api\/news\/status$/, () =>
        HttpResponse.json({ ok: true, data: newsStatusFixture() }),
      ),
      http.get(/.*\/api\/news\/sources$/, () =>
        HttpResponse.json({ ok: true, data: newsSourcesFixture() }),
      ),
    );
  });

  afterEach(cleanup);

  it("defaults to the latest public server feed with 25 rows", async () => {
    const observed = {
      limit: null as string | null,
      sort: null as string | null,
    };
    server.use(
      http.get(/.*\/api\/news\/feed$/, ({ request }) => {
        const params = new URL(request.url).searchParams;
        observed.limit = params.get("limit");
        observed.sort = params.get("sort");
        return HttpResponse.json({ ok: true, data: newsFeedFixture() });
      }),
    );

    renderNews(<NewsPage token="test-token" view="feed" />);

    expect(await screen.findByRole("heading", { name: "全球新闻" })).toBeInTheDocument();
    expect(screen.getByText("公共来源聚合与新闻事件")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "全球新闻" })).toHaveAttribute("aria-current", "page");
    expect(observed.sort).toBe("latest");
    expect(observed.limit).toBe("25");
  });

  it("keeps a 668-character feed headline accessible inside the compact density surface", async () => {
    const longTitle = "长".repeat(668);
    const feed = newsFeedFixture();
    feed.stories[0].title = longTitle;
    feed.stories[0].description = "这是一条用于验证超长标题布局边界的有效摘要。";
    server.use(http.get(/.*\/api\/news\/feed$/, () => HttpResponse.json({ ok: true, data: feed })));

    renderNews(<NewsPage token="test-token" view="feed" />);

    const surface = await screen.findByRole("region", { name: "全球新闻" });
    expect(surface).toHaveAttribute("data-feed-density", "compact");
    const heading = await within(surface).findByRole("heading", { name: longTitle });
    expect(heading).toHaveTextContent(longTitle);
    expect(heading.closest(".news-story-title")).toHaveAttribute(
      "href",
      "/news/stories/story-global-policy",
    );
  });

  it("keeps search, category, severity, origin, and sort in URL-owned server state", async () => {
    const observed = {
      category: null as string | null,
      level: null as string | null,
      q: null as string | null,
      reportingOrigin: null as string | null,
      sort: null as string | null,
    };
    server.use(
      http.get(/.*\/api\/news\/feed$/, ({ request }) => {
        const params = new URL(request.url).searchParams;
        observed.category = params.get("category");
        observed.level = params.get("level");
        observed.q = params.get("q");
        observed.reportingOrigin = params.get("reporting_origin");
        observed.sort = params.get("sort");
        return HttpResponse.json({ ok: true, data: newsFeedFixture() });
      }),
    );

    renderNews(
      <NewsPage token="test-token" view="feed" />,
      "/news?sort=importance&q=bitcoin&category=economic&level=high&reporting_origin=reuters",
    );

    await screen.findByText("Central banks respond to a new global policy shock");
    expect(screen.getByRole("button", { name: "移除搜索：bitcoin" })).toBeInTheDocument();
    expect(screen.getByLabelText("新闻分类")).toHaveValue("economic");
    expect(screen.getByLabelText("新闻严重度")).toHaveValue("high");
    expect(screen.getByLabelText("新闻报道来源")).toHaveValue("reuters");
    expect(observed.q).toBe("bitcoin");
    expect(observed.category).toBe("economic");
    expect(observed.level).toBe("high");
    expect(observed.reportingOrigin).toBe("reuters");
    expect(observed.sort).toBe("importance");

    const filterDisclosure = screen.getByText("筛选").closest("details");
    expect(filterDisclosure).not.toHaveAttribute("open");
    fireEvent.click(screen.getByRole("button", { name: "移除分类：经济" }));
    await waitFor(() =>
      expect(screen.getByTestId("location")).not.toHaveTextContent("category=economic"),
    );
  });

  it("renders the canonical source title without provider-score decoration", async () => {
    const feed = newsFeedFixture();
    const story = feed.stories[0];
    story.description = "多个央行正在重新评估政策路径。";
    story.url = "https://representative.example/news";
    server.use(http.get(/.*\/api\/news\/feed$/, () => HttpResponse.json({ ok: true, data: feed })));

    renderNews(<NewsPage token="test-token" view="feed" />);

    const sourceTitle = await screen.findByRole("heading", { name: story.title });
    const row = sourceTitle.closest("article");
    expect(row).not.toBeNull();
    expect(within(row!).getByText("多个央行正在重新评估政策路径。")).toBeInTheDocument();
    expect(within(row!).queryByText(/OpenNews/)).not.toBeInTheDocument();
    expect(within(row!).getByText(/为什么重要：严重度 41.3/)).toBeInTheDocument();
    expect(within(row!).getByRole("link", { name: /查看原文/ })).toHaveAttribute(
      "href",
      "https://representative.example/news",
    );
  });

  it("omits invalid summaries instead of rendering placeholder copy", async () => {
    const feed = newsFeedFixture();
    feed.stories[0].description = "N/A";
    server.use(http.get(/.*\/api\/news\/feed$/, () => HttpResponse.json({ ok: true, data: feed })));

    renderNews(<NewsPage token="test-token" view="feed" />);

    await screen.findByText(feed.stories[0].title);
    expect(screen.queryByText("N/A")).not.toBeInTheDocument();
    expect(screen.queryByText(/未提供有效摘要/)).not.toBeInTheDocument();
  });

  it("omits a missing original-link placeholder", async () => {
    const feed = newsFeedFixture();
    const story = feed.stories[0];
    story.url = null;
    server.use(http.get(/.*\/api\/news\/feed$/, () => HttpResponse.json({ ok: true, data: feed })));

    renderNews(<NewsPage token="test-token" view="feed" />);

    const title = await screen.findByRole("heading", { name: story.title });
    const row = title.closest("article");
    expect(row).not.toBeNull();
    expect(within(row!).queryByRole("link", { name: /查看原文/ })).not.toBeInTheDocument();
    expect(within(row!).queryByText(/暂无原文链接|未提供/)).not.toBeInTheDocument();
  });

  it("loads the next cursor and deduplicates repeated Story ids", async () => {
    server.use(
      http.get(/.*\/api\/news\/feed$/, ({ request }) => {
        const cursor = new URL(request.url).searchParams.get("cursor");
        const feed = newsFeedFixture();
        if (!cursor) {
          feed.has_more = true;
          feed.next_cursor = "page-2";
          return HttpResponse.json({ ok: true, data: feed });
        }
        feed.has_more = false;
        feed.next_cursor = null;
        feed.stories = [
          feed.stories[0],
          { ...feed.stories[0], story_id: "story-second-page", title: "Second page Story" },
        ];
        return HttpResponse.json({ ok: true, data: feed });
      }),
    );
    renderNews(<NewsPage token="test-token" view="feed" />);
    await screen.findByText("Central banks respond to a new global policy shock");

    fireEvent.click(screen.getByRole("button", { name: "加载更多新闻" }));

    expect(await screen.findByText("Second page Story")).toBeInTheDocument();
    expect(screen.getAllByText("Central banks respond to a new global policy shock")).toHaveLength(
      1,
    );
  });

  it("shows new first-page stories immediately when the reader is at the top", async () => {
    let feed = newsFeedFixture();
    server.use(http.get(/.*\/api\/news\/feed$/, () => HttpResponse.json({ ok: true, data: feed })));
    const rendered = renderNews(<NewsPage token="test-token" view="feed" />);
    await screen.findByText(feed.stories[0].title);

    feed = {
      ...newsFeedFixture(),
      stories: [
        {
          ...newsFeedFixture().stories[0],
          story_id: "story-new-at-top",
          title: "New high-signal event at the top",
        },
        ...newsFeedFixture().stories,
      ],
    };
    await rendered.queryClient.invalidateQueries({ queryKey: ["news-feed"] });

    expect(await screen.findByText("New high-signal event at the top")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /条新新闻/ })).not.toBeInTheDocument();
  });

  it("preserves the viewed event, defers new top ids, and still appends a loaded tail", async () => {
    let feed = newsFeedFixture();
    feed.has_more = true;
    feed.next_cursor = "page-2";
    server.use(
      http.get(/.*\/api\/news\/feed$/, ({ request }) => {
        const cursor = new URL(request.url).searchParams.get("cursor");
        if (!cursor) return HttpResponse.json({ ok: true, data: feed });
        const tail = newsFeedFixture();
        tail.stories = [
          {
            ...tail.stories[0],
            story_id: "story-loaded-tail",
            title: "Loaded non-deferred tail event",
          },
        ];
        tail.has_more = false;
        tail.next_cursor = null;
        return HttpResponse.json({ ok: true, data: tail });
      }),
    );
    const rendered = renderNews(<NewsPage token="test-token" view="feed" />);
    const currentTitle = await screen.findByText(feed.stories[0].title);
    const currentRow = currentTitle.closest("article");
    const scrollContainer = rendered.container.querySelector<HTMLElement>(".center-column");
    expect(scrollContainer).not.toBeNull();
    scrollContainer!.scrollTop = 320;
    fireEvent.scroll(scrollContainer!);

    feed = {
      ...newsFeedFixture(),
      has_more: true,
      next_cursor: "page-2",
      stories: [
        {
          ...newsFeedFixture().stories[0],
          story_id: "story-deferred-at-top",
          title: "Deferred high-signal event",
        },
        ...newsFeedFixture().stories,
      ],
    };
    await rendered.queryClient.invalidateQueries({ queryKey: ["news-feed"] });

    const notice = await screen.findByRole("button", {
      name: "1 条新新闻 · 回到顶部",
    });
    expect(screen.queryByText("Deferred high-signal event")).not.toBeInTheDocument();
    expect(screen.getByText(newsFeedFixture().stories[0].title).closest("article")).toBe(
      currentRow,
    );

    fireEvent.click(screen.getByRole("button", { name: "加载更多新闻" }));

    expect(await screen.findByText("Loaded non-deferred tail event")).toBeInTheDocument();
    expect(screen.queryByText("Deferred high-signal event")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "1 条新新闻 · 回到顶部" })).toBeInTheDocument();
    expect(screen.getByText(newsFeedFixture().stories[0].title).closest("article")).toBe(
      currentRow,
    );

    fireEvent.click(notice);

    expect(await screen.findByText("Deferred high-signal event")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /条新新闻/ })).not.toBeInTheDocument();
  });

  it("keeps fact health inline without a retired title-translation layer", async () => {
    renderNews(<NewsPage token="test-token" view="feed" />);

    const state = await screen.findByText("新闻已同步");
    const summary = state.closest("summary");
    expect(summary).not.toBeNull();
    fireEvent.click(summary!);
    expect(screen.getByText("OpenNews 低延迟主采集链")).toBeInTheDocument();
    expect(screen.getByText("公共 RSS 覆盖与交叉印证")).toBeInTheDocument();
    expect(screen.queryByText("中文标题翻译")).not.toBeInTheDocument();
    expect(screen.queryByText("暂无译文")).not.toBeInTheDocument();
    expect(screen.queryByText(/WSS|REST/)).not.toBeInTheDocument();
  });

  it("ignores Brief or Push overall state when reader-facing News layers are ready", async () => {
    server.use(
      http.get(/.*\/api\/news\/status$/, () =>
        HttpResponse.json({
          ok: true,
          data: newsStatusFixture({ operating_state: "stalled" }),
        }),
      ),
    );

    renderNews(<NewsPage token="test-token" view="feed" />);

    expect(await screen.findByText("新闻已同步")).toBeInTheDocument();
  });

  it("distinguishes News fact recovery from a degraded fact layer", async () => {
    const status = newsStatusFixture();
    status.layers.ingest.status = "warming";
    server.use(
      http.get(/.*\/api\/news\/status$/, () => HttpResponse.json({ ok: true, data: status })),
    );
    const rendered = renderNews(<NewsPage token="test-token" view="feed" />);
    expect(await screen.findByText("新闻数据恢复中")).toBeInTheDocument();

    status.layers.ingest.status = "ready";
    status.layers.story.status = "degraded";
    await rendered.queryClient.invalidateQueries({ queryKey: ["news-status"] });
    const degraded = await screen.findByText("新闻更新异常");
    expect(degraded.closest("details")).toHaveAttribute("data-state", "stalled");
  });

  it("presents an OpenNews primary failure as a degraded fact layer", async () => {
    const status = newsStatusFixture();
    status.layers.ingest.status = "degraded";
    status.layers.ingest.opennews!.last_error = "opennews_live_disconnected";
    status.layers.ingest.reasons = ["opennews_primary_error"];
    server.use(
      http.get(/.*\/api\/news\/status$/, () => HttpResponse.json({ ok: true, data: status })),
    );

    renderNews(<NewsPage token="test-token" view="feed" />);

    const degraded = await screen.findByText("新闻更新异常");
    expect(degraded.closest("details")).toHaveAttribute("data-state", "stalled");
    expect(screen.getByText(/数据更新遇到异常/)).toBeInTheDocument();
  });

  it("keeps a healthy OpenNews primary readable while RSS corroboration warms", async () => {
    const status = newsStatusFixture();
    status.layers.ingest.rss.successful_source_count = 0;
    status.layers.ingest.reasons = ["public_rss_corroboration_warming"];
    server.use(
      http.get(/.*\/api\/news\/status$/, () => HttpResponse.json({ ok: true, data: status })),
    );

    renderNews(<NewsPage token="test-token" view="feed" />);

    const ready = await screen.findByText("新闻已同步");
    fireEvent.click(ready.closest("summary")!);
    expect(screen.queryByText(/数据更新遇到异常/)).not.toBeInTheDocument();
  });

  it("shows intentionally disabled RSS without treating it as an ingest failure", async () => {
    const status = newsStatusFixture();
    status.layers.ingest.rss = {
      ...status.layers.ingest.rss,
      enabled: false,
      latest_success_at_ms: null,
      next_due_at_ms: null,
      source_count: 0,
      successful_source_count: 0,
    };
    server.use(
      http.get(/.*\/api\/news\/status$/, () => HttpResponse.json({ ok: true, data: status })),
    );

    renderNews(<NewsPage token="test-token" view="feed" />);

    const ready = await screen.findByText("新闻已同步");
    fireEvent.click(ready.closest("summary")!);
    expect(screen.getByRole("heading", { name: "公共 RSS 覆盖与交叉印证" })).toBeInTheDocument();
    expect(screen.getByText("未启用")).toBeInTheDocument();
    expect(screen.queryByText(/数据更新遇到异常/)).not.toBeInTheDocument();
  });

  it("renders the four public News layers on the standalone Status view", async () => {
    renderNews(<NewsPage token="test-token" view="status" />, "/news/status");

    expect(await screen.findByRole("heading", { name: "新闻运行状态" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "状态" })).toHaveAttribute("aria-current", "page");
    const ingest = (await screen.findByRole("heading", { name: "公开采集" })).closest("article");
    const story = screen.getByRole("heading", { name: "新闻事件" }).closest("article");
    const brief = screen.getByRole("heading", { name: "公共简报" }).closest("article");
    const push = screen.getByRole("heading", { name: "新闻推送" }).closest("article");
    expect(ingest).not.toBeNull();
    expect(story).not.toBeNull();
    expect(brief).not.toBeNull();
    expect(push).not.toBeNull();
    const primaryFact = within(ingest!).getByText("OpenNews 主链实时连接");
    const corroborationFact = within(ingest!).getByText("RSS 印证成功来源");
    expect(
      primaryFact.compareDocumentPosition(corroborationFact) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(within(ingest!).getByText("179/179")).toBeInTheDocument();
    expect(within(story!).getByText("当前事件").parentElement).toHaveTextContent("1");
    expect(within(brief!).getByText("公开状态").parentElement).toHaveTextContent("当前");
    expect(within(push!).getByText("24 小时翻译成功").parentElement).toHaveTextContent("1/1");
    expect(within(push!).queryByRole("button")).not.toBeInTheDocument();
  });

  it("renders Sources in exact server page order and loads the next cursor explicitly", async () => {
    const cursors: Array<string | null> = [];
    server.use(
      http.get(/.*\/api\/news\/sources$/, ({ request }) => {
        const cursor = new URL(request.url).searchParams.get("cursor");
        cursors.push(cursor);
        return HttpResponse.json({
          ok: true,
          data: cursor
            ? newsSourcesFixture({
                items: [
                  {
                    ...newsSourcesFixture().items[0],
                    name: "Third Source",
                    source_id: "source-third",
                  },
                ],
                page: { has_more: false, next_cursor: null, returned_count: 1 },
              })
            : newsSourcesFixture({
                items: [
                  {
                    ...newsSourcesFixture().items[0],
                    name: "First Source",
                    source_id: "source-first",
                  },
                  {
                    ...newsSourcesFixture().items[0],
                    name: "Second Source",
                    source_id: "source-second",
                  },
                ],
                page: { has_more: true, next_cursor: "sources-2", returned_count: 2 },
              }),
        });
      }),
    );
    renderNews(<NewsPage token="test-token" view="sources" />, "/news/sources");

    expect(await screen.findByRole("heading", { name: "公开新闻来源" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "来源" })).toHaveAttribute("aria-current", "page");
    const first = await screen.findByRole("heading", { name: "First Source" });
    const second = screen.getByRole("heading", { name: "Second Source" });
    expect(first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "加载更多来源" }));

    const third = await screen.findByRole("heading", { name: "Third Source" });
    expect(second.compareDocumentPosition(third) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(cursors).toEqual([null, "sources-2"]);
  });

  it("labels OpenNews as the primary lane and reports a lost live connection", async () => {
    const primary = newsSourcesFixture().items.find((source) => source.source_kind === "opennews");
    if (!primary) throw new Error("OpenNews source fixture required");
    server.use(
      http.get(/.*\/api\/news\/sources$/, () =>
        HttpResponse.json({
          ok: true,
          data: newsSourcesFixture({
            items: [{ ...primary, live_connected: false }],
            page: { has_more: false, next_cursor: null, returned_count: 1 },
          }),
        }),
      ),
    );

    renderNews(<NewsPage token="test-token" view="sources" />, "/news/sources");

    const heading = await screen.findByRole("heading", { name: "OpenNews" });
    const card = heading.closest("article");
    expect(card).not.toBeNull();
    expect(within(card!).getByText("OPENNEWS · 主采集链")).toBeInTheDocument();
    expect(within(card!).getByText("实时连接异常")).toBeInTheDocument();
    expect(within(card!).queryByText(/TIER 4/)).not.toBeInTheDocument();
  });

  it("makes Story detail reading-first and keeps scoring internals in a disclosure", async () => {
    const detail = newsStoryDetailFixture();
    detail.url = "https://representative.example/detail";
    detail.members[0].url = "https://scoring.example/detail";
    server.use(
      http.get(/.*\/api\/news\/stories\/story-global-policy$/, () =>
        HttpResponse.json({ ok: true, data: detail }),
      ),
    );
    renderNews(
      <NewsPage storyId="story-global-policy" token="test-token" view="story" />,
      "/news/stories/story-global-policy",
    );

    const title = await screen.findByRole("heading", {
      level: 1,
      name: "Central banks respond to a new global policy shock",
    });
    expect(screen.getByRole("link", { name: "状态" })).toHaveAttribute("href", "/news/status");
    expect(screen.getByRole("link", { name: "来源" })).toHaveAttribute("href", "/news/sources");
    const evidence = screen.getByRole("heading", { name: "4 家独立来源" });
    const hero = title.closest(".news-story-hero") as HTMLElement | null;
    expect(hero).not.toBeNull();
    expect(within(hero!).getByText(detail.source_name)).toBeInTheDocument();
    expect(within(hero!).queryByText(/OpenNews/)).not.toBeInTheDocument();
    const auditSummary = screen.getByText("查看 Tracefold 评分与新闻事件审计");
    expect(title.compareDocumentPosition(evidence) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(
      evidence.compareDocumentPosition(auditSummary) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(auditSummary.closest("details")).not.toHaveAttribute("open");
    expect(screen.getByRole("link", { name: "阅读代表原文" })).toHaveAttribute(
      "href",
      "https://representative.example/detail",
    );
    fireEvent.click(auditSummary);
    expect(screen.getByText("聚合身份")).toBeInTheDocument();
    expect(screen.getByText("总重要度").parentElement).toHaveTextContent("83");
    expect(screen.getByRole("link", { name: /评分报道原文/ })).toHaveAttribute(
      "href",
      "https://scoring.example/detail",
    );
  });

  it("keeps an overlong detail title readable behind an explicit expansion", async () => {
    const detail = newsStoryDetailFixture({
      title: "中等长度标题".repeat(12),
    });
    server.use(
      http.get(/.*\/api\/news\/stories\/story-global-policy$/, () =>
        HttpResponse.json({ ok: true, data: detail }),
      ),
    );
    renderNews(
      <NewsPage storyId="story-global-policy" token="test-token" view="story" />,
      "/news/stories/story-global-policy",
    );

    const title = await screen.findByRole("heading", { level: 1, name: detail.title });
    expect(title).toHaveClass("is-clamped");
    const expansion = screen.getByText("展开完整标题").closest("details");
    expect(expansion).not.toHaveAttribute("open");
    fireEvent.click(screen.getByText("展开完整标题"));
    expect(expansion).toHaveAttribute("open");
    expect(screen.getByText("收起完整标题")).toBeInTheDocument();
  });

  it("loads additional related reports from the member cursor", async () => {
    const detail = newsStoryDetailFixture();
    const secondMember = {
      ...detail.members[0],
      item_id: "news-item-second",
      provider_record_id: "wire-second-2",
      reporting_origin: "ap",
      source_id: "wm-politics-ap",
      source_name: "Associated Press",
      title: "Markets < 10 &amp; <strong>policy</strong> update https://example.com/raw",
      url: "https://apnews.com/example",
    };
    const observedCursors: Array<string | null> = [];
    server.use(
      http.get(/.*\/api\/news\/stories\/story-global-policy$/, ({ request }) => {
        const cursor = new URL(request.url).searchParams.get("members_cursor");
        observedCursors.push(cursor);
        return HttpResponse.json({
          ok: true,
          data: cursor
            ? newsStoryDetailFixture({
                members: [secondMember],
                members_page: { has_more: false, next_cursor: null, returned_count: 1 },
              })
            : newsStoryDetailFixture({
                members_page: { has_more: true, next_cursor: "members-2", returned_count: 1 },
              }),
        });
      }),
    );
    renderNews(
      <NewsPage storyId="story-global-policy" token="test-token" view="story" />,
      "/news/stories/story-global-policy",
    );
    const loadMore = await screen.findByRole("button", { name: "加载更多相关报道" });

    fireEvent.click(loadMore);

    expect(await screen.findByText("Markets < 10 & policy update")).toBeInTheDocument();
    expect(observedCursors).toContain("members-2");
    expect(screen.queryByRole("button", { name: "加载更多相关报道" })).not.toBeInTheDocument();
  });

  it("renders immutable top stories first in exact server order", async () => {
    renderNews(<NewsPage token="test-token" view="brief" />, "/news/brief");

    const brief = await screen.findByRole("region", { name: "公共全球简报" });
    expect(screen.getByRole("link", { name: "公共全球简报" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    const stories = await within(brief).findAllByTestId("brief-top-story");
    expect(stories).toHaveLength(2);
    expect(within(stories[0]).getByRole("heading", { level: 2 })).toHaveTextContent(
      "Ceasefire talks resume as delegations return",
    );
    expect(within(stories[1]).getByRole("heading", { level: 2 })).toHaveTextContent(
      "Typhoon makes landfall near a major port",
    );
    expect(screen.queryByText(/历史发布|候选新闻|综合得分/)).not.toBeInTheDocument();
  });

  it("shows the sealed public selection funnel", async () => {
    renderNews(<NewsPage token="test-token" view="brief" />, "/news/brief");

    const funnel = await screen.findByLabelText("公开精选漏斗");
    expect(within(funnel).getByText("候选 4")).toBeInTheDocument();
    expect(within(funnel).getByText("可作主线 1")).toBeInTheDocument();
    expect(within(funnel).getByText("主线补位 未触发")).toBeInTheDocument();
    expect(within(funnel).getByText("准入剔除 2")).toBeInTheDocument();
    expect(within(funnel).getByText("来源上限剔除 0")).toBeInTheDocument();
    expect(within(funnel).getByText("名额溢出剔除 0")).toBeInTheDocument();
  });

  it("shows primary and member evidence, counts, source age, and linkless state", async () => {
    const brief = newsGlobalBriefFixture();
    if (!brief.publication) throw new Error("brief publication fixture required");
    server.use(
      http.get(/.*\/api\/news\/brief$/, () => HttpResponse.json({ ok: true, data: brief })),
    );

    renderNews(<NewsPage token="test-token" view="brief" />, "/news/brief");

    const first = (await screen.findAllByTestId("brief-top-story"))[0];
    expect(within(first).getByText("Reuters")).toBeInTheDocument();
    expect(within(first).getByText("3 条报道")).toBeInTheDocument();
    expect(within(first).getByText("2 家独立来源")).toBeInTheDocument();
    expect(within(first).getByText("Reuters、Associated Press")).toBeInTheDocument();
    expect(
      within(first).getByText("Delegations return to the negotiating table"),
    ).toBeInTheDocument();
    expect(within(first).getByRole("link", { name: "阅读主要来源" })).toHaveAttribute(
      "href",
      "https://www.reuters.com/world/ceasefire",
    );
    expect(within(first).getByText(/来源更新/)).toBeInTheDocument();

    const second = (await screen.findAllByTestId("brief-top-story"))[1];
    expect(within(second).getByText("无主要来源链接")).toBeInTheDocument();
    expect(within(second).queryByRole("link", { name: "阅读主要来源" })).not.toBeInTheDocument();
  });

  it("keeps L1 output in a separately labelled AI enhancement", async () => {
    renderNews(<NewsPage token="test-token" view="brief" />, "/news/brief");

    const topStories = await screen.findByRole("heading", { name: "公开重点新闻" });
    const enhancement = screen.getByRole("heading", { name: "AI 增强概览" });
    expect(
      topStories.compareDocumentPosition(enhancement) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(screen.getByText(/Ceasefire talks and severe weather/)).toBeInTheDocument();
    const summaries = screen.getByRole("list", { name: "AI 新闻事件摘要" });
    expect(within(summaries).getAllByRole("listitem")[0]).toHaveTextContent(
      "Ceasefire talks resume as delegations return [1]",
    );
    expect(screen.getByText("L1 · 完整校验")).toBeInTheDocument();
  });

  it("links valid lead citations to their immutable Story slots with native keyboard access", async () => {
    const brief = newsGlobalBriefFixture();
    if (!brief.publication) throw new Error("brief publication fixture required");
    brief.publication.world_brief =
      "Severe weather follows renewed diplomacy [2], while talks continue [1]; sealed unknown [9].";
    server.use(
      http.get(/.*\/api\/news\/brief$/, () => HttpResponse.json({ ok: true, data: brief })),
    );

    renderNews(<NewsPage token="test-token" view="brief" />, "/news/brief");

    const lead = await screen.findByText(
      (_, element) =>
        element?.tagName === "P" && element.textContent === brief.publication?.world_brief,
    );
    expect(lead.textContent).toBe(brief.publication.world_brief);
    const citations = within(lead).getAllByRole("link");
    expect(citations).toHaveLength(2);
    expect(citations[0]).toHaveAccessibleName("引用 2：Typhoon makes landfall near a major port");
    expect(citations[0]).toHaveTextContent("[2]");
    expect(citations[0]).toHaveAttribute("href", "/news/stories/story-typhoon");
    expect(citations[1]).toHaveAccessibleName(
      "引用 1：Ceasefire talks resume as delegations return",
    );
    expect(citations[1]).toHaveTextContent("[1]");
    expect(citations[1]).toHaveAttribute("href", "/news/stories/story-ceasefire");
    expect(within(lead).queryByRole("link", { name: /引用 9/ })).not.toBeInTheDocument();

    citations[0].focus();
    expect(citations[0]).toHaveFocus();
    expect(citations[0]).toHaveProperty("tabIndex", 0);
    fireEvent.click(citations[0]);
    await waitFor(() =>
      expect(screen.getByTestId("location")).toHaveTextContent("/news/stories/story-typhoon"),
    );
  });

  it("links each L1 Story-line citation marker to its immutable Story slot", async () => {
    const brief = newsGlobalBriefFixture();
    if (!brief.publication) throw new Error("brief publication fixture required");
    brief.publication.brief_story_lines[1].text = "A typhoon reaches the coast [002]";
    server.use(
      http.get(/.*\/api\/news\/brief$/, () => HttpResponse.json({ ok: true, data: brief })),
    );
    renderNews(<NewsPage token="test-token" view="brief" />, "/news/brief");

    const summaries = await screen.findByRole("list", { name: "AI 新闻事件摘要" });
    const items = within(summaries).getAllByRole("listitem");
    expect(within(items[0]).getByRole("link", { name: /引用 1/ })).toHaveAttribute(
      "href",
      "/news/stories/story-ceasefire",
    );
    expect(within(items[1]).getByRole("link", { name: /引用 2/ })).toHaveAttribute(
      "href",
      "/news/stories/story-typhoon",
    );
  });

  it("renders L2 prose without inventing Story lines and renders none with no prose", async () => {
    const brief = newsGlobalBriefFixture();
    if (!brief.publication) throw new Error("brief publication fixture required");
    brief.state = "degraded";
    brief.publication.brief_kind = "l2";
    brief.publication.quality = "degraded";
    brief.publication.world_brief =
      "Ceasefire negotiations resume while a typhoon reaches the coast.";
    brief.publication.brief_story_lines = [];
    server.use(
      http.get(/.*\/api\/news\/brief$/, () => HttpResponse.json({ ok: true, data: brief })),
    );
    const rendered = renderNews(<NewsPage token="test-token" view="brief" />, "/news/brief");

    expect(await screen.findByText("L2 · 降级概览")).toBeInTheDocument();
    expect(screen.getByText(brief.publication.world_brief)).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "AI 新闻事件摘要" })).not.toBeInTheDocument();

    brief.publication.brief_kind = "none";
    brief.publication.world_brief = "";
    await rendered.queryClient.invalidateQueries({ queryKey: ["news-brief"] });
    expect(await screen.findByText("本版没有 AI 增强")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "AI 增强概览" })).not.toBeInTheDocument();
  });

  it.each([
    ["current", "当前公开快报"],
    ["degraded", "当前快报 · AI 增强降级"],
    ["last_known_good", "上一份完整公开快报"],
  ] as const)("renders the %s publication state from one sealed payload", async (state, label) => {
    const brief = newsGlobalBriefFixture({ state });
    server.use(
      http.get(/.*\/api\/news\/brief$/, () => HttpResponse.json({ ok: true, data: brief })),
    );

    renderNews(<NewsPage token="test-token" view="brief" />, "/news/brief");

    expect(await screen.findByText(label)).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Ceasefire talks resume as delegations return" }),
    ).toBeInTheDocument();
  });

  it("renders unavailable without a synthetic publication", async () => {
    const brief = newsGlobalBriefFixture({
      latest_run: null,
      next_due_at_ms: NEWS_NOW_MS + 1_800_000,
      publication: null,
      slot_at_ms: null,
      state: "unavailable",
    });
    server.use(
      http.get(/.*\/api\/news\/brief$/, () => HttpResponse.json({ ok: true, data: brief })),
    );

    renderNews(<NewsPage token="test-token" view="brief" />, "/news/brief");

    expect(await screen.findByText("尚无公共全球简报")).toBeInTheDocument();
    expect(screen.queryByTestId("brief-top-story")).not.toBeInTheDocument();
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
