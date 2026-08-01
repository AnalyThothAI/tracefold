import { NewsPage } from "@features/news";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import {
  newsFeedFixture,
  newsGlobalBriefFixture,
  newsSourcesFixture,
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
      http.get(/.*\/api\/news\/sources$/, () =>
        HttpResponse.json({ ok: true, data: newsSourcesFixture() }),
      ),
    );
  });

  afterEach(cleanup);

  it("renders persisted Story facts and the importance breakdown", async () => {
    renderNews(<NewsPage token="test-token" />);
    expect(
      await screen.findByText("Central banks respond to a new global policy shock"),
    ).toBeInTheDocument();
    expect(screen.getByText(/严重度得分 41.3/)).toBeInTheDocument();
    expect(screen.getByText(/佐证得分 12（计分来源 4）/)).toBeInTheDocument();
    expect(screen.getAllByText("4", { selector: ".news-story-counts b" })).toHaveLength(2);
    expect(screen.getByText("Tracefold 重要度")).toBeInTheDocument();
    expect(screen.getByText("最高评分 Item")).toBeInTheDocument();
    expect(screen.getByText("提供方评分").parentElement).toHaveTextContent("88");
    expect(screen.getByText("信号").parentElement).toHaveTextContent("long");
    expect(screen.getByText("等级").parentElement).toHaveTextContent("A");
    expect(screen.getByText("关联代币").parentElement).toHaveTextContent("BTC · spot · Bitcoin");
    const storyMain = screen
      .getByText("Central banks respond to a new global policy shock")
      .closest("a");
    expect(storyMain).not.toBeNull();
    const storyCoins = within(storyMain!).getByRole("group", { name: "OpenNews 关联代币" });
    expect(storyCoins).toHaveTextContent("BTC");
    expect(storyCoins).not.toHaveTextContent("spot");
    expect(storyCoins).not.toHaveTextContent("Bitcoin");
    expect(screen.getByRole("link", { name: /最高评分 Item 原文/ })).toHaveAttribute(
      "href",
      "https://www.reuters.com/world/story",
    );
    expect(screen.queryByText(/AI 分析/)).not.toBeInTheDocument();
  });

  it("shows a linkless state for the server-selected provider evidence", async () => {
    const feed = newsFeedFixture();
    feed.stories[0].provider_evidence = {
      ...feed.stories[0].provider_evidence!,
      url: null,
    };
    server.use(http.get(/.*\/api\/news\/feed$/, () => HttpResponse.json({ ok: true, data: feed })));

    renderNews(<NewsPage token="test-token" />);

    expect(await screen.findByText("该 Item 未提供文章链接")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /最高评分 Item 原文/ })).not.toBeInTheDocument();
  });

  it("omits the prominent Story coin group when the selected Item has no coins", async () => {
    const feed = newsFeedFixture();
    feed.stories[0].provider_evidence = {
      ...feed.stories[0].provider_evidence!,
      provider_metadata: {
        ...feed.stories[0].provider_evidence!.provider_metadata,
        coins: [],
      },
    };
    server.use(http.get(/.*\/api\/news\/feed$/, () => HttpResponse.json({ ok: true, data: feed })));

    renderNews(<NewsPage token="test-token" />);

    await screen.findByText("Central banks respond to a new global policy shock");
    expect(screen.queryByRole("group", { name: "OpenNews 关联代币" })).not.toBeInTheDocument();
  });

  it("keeps category as URL state and sends it to the Feed endpoint", async () => {
    let observedCategory: string | null = null;
    server.use(
      http.get(/.*\/api\/news\/feed$/, ({ request }) => {
        observedCategory = new URL(request.url).searchParams.get("category");
        return HttpResponse.json({ ok: true, data: newsFeedFixture() });
      }),
    );
    renderNews(<NewsPage token="test-token" />);
    await screen.findByText("Central banks respond to a new global policy shock");
    fireEvent.click(screen.getByRole("button", { name: /经济/ }));
    expect(await screen.findByTestId("location")).toHaveTextContent("category=economic");
    expect(observedCategory).toBe("economic");
  });

  it("keeps latest sorting as URL state and sends it to the Feed endpoint", async () => {
    let observedSort: string | null = null;
    server.use(
      http.get(/.*\/api\/news\/feed$/, ({ request }) => {
        observedSort = new URL(request.url).searchParams.get("sort");
        const feed = newsFeedFixture();
        if (observedSort === "latest") {
          feed.stories = [
            {
              ...feed.stories[0],
              category: "protest",
              last_published_at_ms: 1_785_142_000_000,
              story_id: "story-newer-protest",
              title: "Newer protest Story",
            },
            {
              ...feed.stories[0],
              category: "conflict",
              last_published_at_ms: 1_785_141_000_000,
              story_id: "story-older-conflict",
              title: "Older conflict Story",
            },
          ];
        }
        return HttpResponse.json({ ok: true, data: feed });
      }),
    );
    const rendered = renderNews(<NewsPage token="test-token" />);
    await screen.findByText("Central banks respond to a new global policy shock");
    fireEvent.click(screen.getByRole("button", { name: "最新" }));
    expect(await screen.findByTestId("location")).toHaveTextContent("sort=latest");
    expect(observedSort).toBe("latest");
    await screen.findByText("Newer protest Story");
    const times = Array.from(
      rendered.container.querySelectorAll<HTMLTimeElement>(".news-story-row time"),
      (node) => Date.parse(node.dateTime),
    );
    expect(times).toEqual([...times].sort((left, right) => right - left));
    const titles = Array.from(
      rendered.container.querySelectorAll<HTMLElement>(".news-story-row strong"),
      (node) => node.textContent,
    );
    expect(titles).toEqual(["Newer protest Story", "Older conflict Story"]);
  });

  it("loads the next cursor and deduplicates a repeated Story id", async () => {
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
          {
            ...feed.stories[0],
            story_id: "story-second-page",
            title: "Second page Story",
          },
        ];
        return HttpResponse.json({ ok: true, data: feed });
      }),
    );
    renderNews(<NewsPage token="test-token" />);
    await screen.findByText("Central banks respond to a new global policy shock");
    fireEvent.click(screen.getByRole("button", { name: "加载更多" }));
    expect(await screen.findByText("Second page Story")).toBeInTheDocument();
    expect(screen.getAllByText("Central banks respond to a new global policy shock")).toHaveLength(
      1,
    );
  });

  it("renders NewsItem members without revisions or Story AI", async () => {
    renderNews(
      <NewsPage storyId="story-global-policy" token="test-token" />,
      "/news/stories/story-global-policy",
    );
    expect(await screen.findByText("聚类成员")).toBeInTheDocument();
    expect(screen.getByText("Story 聚合身份")).toBeInTheDocument();
    expect(screen.getByText("当前 96 小时 Story")).toBeInTheDocument();
    expect(screen.getAllByText("展示代表").length).toBeGreaterThan(0);
    expect(screen.getAllByText("评分依据").length).toBeGreaterThan(0);
    expect(screen.getByText("reuters")).toBeInTheDocument();
    const providerPanel = screen.getByRole("region", { name: "OpenNews 提供方元数据" });
    expect(within(providerPanel).getByText("提供方评分").parentElement).toHaveTextContent("88");
    expect(within(providerPanel).getByText("信号").parentElement).toHaveTextContent("long");
    expect(within(providerPanel).getByText("等级").parentElement).toHaveTextContent("A");
    expect(within(providerPanel).getByText("提供方来源").parentElement).toHaveTextContent("jin10");
    expect(within(providerPanel).getByText("关联代币").parentElement).toHaveTextContent(
      "BTC · spot · Bitcoin",
    );
    expect(screen.getByText(/不参与 Tracefold Story 重要度/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /阅读该 NewsItem 原文/ })).toHaveAttribute(
      "href",
      "https://www.reuters.com/world/story",
    );
    expect(screen.queryByText(/Revision/)).not.toBeInTheDocument();
  });

  it("renders explicit missing OpenNews metadata and member link states", async () => {
    const story = newsStoryDetailFixture();
    story.members[0] = {
      ...story.members[0],
      provider_metadata: {},
      url: null,
    };
    server.use(
      http.get(/.*\/api\/news\/stories\/story-global-policy$/, () =>
        HttpResponse.json({ ok: true, data: story }),
      ),
    );

    renderNews(
      <NewsPage storyId="story-global-policy" token="test-token" />,
      "/news/stories/story-global-policy",
    );

    const providerPanel = await screen.findByRole("region", {
      name: "OpenNews 提供方元数据",
    });
    expect(within(providerPanel).getByText("提供方评分").parentElement).toHaveTextContent("未提供");
    expect(within(providerPanel).getByText("关联代币").parentElement).toHaveTextContent("未提供");
    expect(within(providerPanel).getByText("该 NewsItem 未提供文章链接")).toBeInTheDocument();
  });

  it("separates reporting-origin counts from scoring points and boosts", async () => {
    const story = newsStoryDetailFixture({
      importance_score: 124,
      item_count: 2,
      source_count: 2,
    });
    story.importance_factors = {
      ...story.importance_factors,
      corroboration_points: 15,
      diplomacy_flashpoint_boost: 18,
      entity_corroboration_boost: 20,
      reporting_origin_count: 2,
      recency_points: 9.66,
      scoring_corroboration_count: 9,
      total: 124,
    };
    server.use(
      http.get(/.*\/api\/news\/stories\/story-global-policy$/, () =>
        HttpResponse.json({ ok: true, data: story }),
      ),
    );

    renderNews(
      <NewsPage storyId="story-global-policy" token="test-token" />,
      "/news/stories/story-global-policy",
    );

    expect(await screen.findByText("Tracefold Story 重要度")).toBeInTheDocument();
    expect(screen.getByText("Story 内报道来源").parentElement).toHaveTextContent("2");
    expect(screen.getByText("计分佐证来源").parentElement).toHaveTextContent("9");
    expect(screen.getByText("来源质量得分").parentElement).toHaveTextContent("20");
    expect(screen.getByText("来源质量得分").parentElement).toHaveTextContent(
      "Reuters World · Tier 1",
    );
    expect(screen.getByText("佐证得分").parentElement).toHaveTextContent("15");
    expect(screen.getByText("外交热点加分").parentElement).toHaveTextContent("+18");
    expect(screen.getByText("实体佐证加分").parentElement).toHaveTextContent("+20");
    expect(screen.getByText("总重要度").parentElement).toHaveTextContent("124");
  });

  it("renders one Chinese World Brief and its embedded history", async () => {
    renderNews(<NewsPage brief token="test-token" />, "/news/brief");
    expect(await screen.findByText("全球新闻简报")).toBeInTheDocument();
    expect(screen.getByText(/全球政策冲击正在改变央行预期/)).toBeInTheDocument();
    expect(screen.getByText("历史发布")).toBeInTheDocument();
    expect(screen.getByText(/引用序号锁定\s+通过/)).toBeInTheDocument();
  });

  it("renders the single OpenNews connection and recovery state", async () => {
    renderNews(<NewsPage sources token="test-token" />, "/news/sources");
    expect(await screen.findByText("新闻来源状态")).toBeInTheDocument();
    expect(await screen.findByText("OpenNews")).toBeInTheDocument();
    expect(screen.getByText("1 个来源")).toBeInTheDocument();
    expect(screen.getByText("WSS 已连接")).toBeInTheDocument();
    expect(screen.getByText("上次 REST 恢复")).toBeInTheDocument();
  });
});

function renderNews(node: ReactNode, path = "/news") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        {node}
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function LocationProbe() {
  const location = useLocation();
  return <span data-testid="location">{`${location.pathname}${location.search}`}</span>;
}
