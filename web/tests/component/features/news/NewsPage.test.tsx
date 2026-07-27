import { NewsPage } from "@features/news";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
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
    expect(screen.getByText(/严重度 41.3/)).toBeInTheDocument();
    expect(screen.getAllByText("4", { selector: ".news-story-counts b" })).toHaveLength(2);
    expect(screen.queryByText(/AI 分析/)).not.toBeInTheDocument();
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
    fireEvent.click(screen.getByRole("button", { name: "经济" }));
    expect(await screen.findByTestId("location")).toHaveTextContent("category=economic");
    expect(observedCategory).toBe("economic");
  });

  it("keeps latest sorting as URL state and sends it to the Feed endpoint", async () => {
    let observedSort: string | null = null;
    server.use(
      http.get(/.*\/api\/news\/feed$/, ({ request }) => {
        observedSort = new URL(request.url).searchParams.get("sort");
        return HttpResponse.json({ ok: true, data: newsFeedFixture() });
      }),
    );
    renderNews(<NewsPage token="test-token" />);
    await screen.findByText("Central banks respond to a new global policy shock");
    fireEvent.click(screen.getByRole("button", { name: "最新" }));
    expect(await screen.findByTestId("location")).toHaveTextContent("sort=latest");
    expect(observedSort).toBe("latest");
  });

  it("renders NewsItem members without revisions or Story AI", async () => {
    renderNews(
      <NewsPage storyId="story-global-policy" token="test-token" />,
      "/news/stories/story-global-policy",
    );
    expect(await screen.findByText("聚类成员")).toBeInTheDocument();
    expect(screen.getByText("reuters")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /阅读原文/ })).toHaveAttribute(
      "href",
      "https://www.reuters.com/world/story",
    );
    expect(screen.queryByText(/Revision/)).not.toBeInTheDocument();
  });

  it("renders one Chinese World Brief and its embedded history", async () => {
    renderNews(<NewsPage brief token="test-token" />, "/news/brief");
    expect(await screen.findByText("全球新闻简报")).toBeInTheDocument();
    expect(screen.getByText(/全球政策冲击正在改变央行预期/)).toBeInTheDocument();
    expect(screen.getByText("历史发布")).toBeInTheDocument();
    expect(screen.getByText(/引用序号锁定\s+通过/)).toBeInTheDocument();
  });

  it("renders source fetch health and gate counts", async () => {
    renderNews(<NewsPage sources token="test-token" />, "/news/sources");
    expect(await screen.findByText("新闻来源状态")).toBeInTheDocument();
    expect(await screen.findByText("Reuters World")).toBeInTheDocument();
    expect(screen.getByText(/duplicate 3/)).toBeInTheDocument();
    expect(screen.getByText("成功")).toBeInTheDocument();
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
