import { NewsPage } from "@features/news";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { newsStoryDetailFixture, newsStoryFixture } from "@tests/fixtures/newsFixture";
import { server } from "@tests/msw/server";
import { HttpResponse, http } from "msw";
import type { ReactNode } from "react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

describe("NewsPage", () => {
  beforeEach(() => {
    server.use(
      http.get(/.*\/api\/news\/stories$/, () =>
        HttpResponse.json({
          ok: true,
          data: { items: [newsStoryFixture()], next_cursor: null },
        }),
      ),
      http.get(/.*\/api\/news\/stories\/story-global-policy$/, () =>
        HttpResponse.json({ ok: true, data: newsStoryDetailFixture() }),
      ),
    );
  });

  afterEach(cleanup);

  it("renders one Story row with verification, importance, sources, and AI state", async () => {
    renderNews(<NewsPage token="test-token" />);

    expect(screen.getByRole("status", { name: "loading News stories" })).toBeInTheDocument();
    expect(
      await screen.findByText("Central banks respond to a new global policy shock"),
    ).toBeInTheDocument();
    expect(screen.getAllByText("多源核验")).toHaveLength(2);
    expect(screen.getByText("77")).toBeInTheDocument();
    expect(screen.getByText("AI 已分析")).toBeInTheDocument();
    expect(screen.getByText("Reuters World")).toBeInTheDocument();
  });

  it("requests search, source, and verification filters", async () => {
    const requests: Array<Record<string, string | null>> = [];
    server.use(
      http.get(/.*\/api\/news\/stories$/, ({ request }) => {
        const params = new URL(request.url).searchParams;
        requests.push({
          q: params.get("q"),
          source: params.get("source"),
          verification: params.get("verification_status"),
        });
        return HttpResponse.json({
          ok: true,
          data: { items: [newsStoryFixture()], next_cursor: null },
        });
      }),
    );
    renderNews(<NewsPage token="test-token" />);
    await screen.findByText("Central banks respond to a new global policy shock");

    fireEvent.click(screen.getByRole("button", { name: "多源核验" }));
    fireEvent.change(screen.getByLabelText("Search stories"), { target: { value: "rates" } });
    fireEvent.change(screen.getByLabelText("Filter by source"), {
      target: { value: "Reuters" },
    });

    await waitFor(() =>
      expect(
        requests.some(
          (request) =>
            request.q === "rates" &&
            request.source === "Reuters" &&
            request.verification === "corroborated",
        ),
      ).toBe(true),
    );
  });

  it("opens the Story detail route", async () => {
    renderNews(<NewsPage token="test-token" />);
    const story = await screen.findByRole("button", {
      name: /Central banks respond to a new global policy shock/,
    });
    fireEvent.click(story);
    expect(screen.getByTestId("location")).toHaveTextContent("/news/stories/story-global-policy");
  });

  it("shows Chinese analysis and article-level provenance on the Story detail", async () => {
    renderNews(
      <NewsPage storyId="story-global-policy" token="test-token" />,
      "/news/stories/story-global-policy",
    );

    expect(await screen.findByText("DeepSeek 中文分析")).toBeInTheDocument();
    expect(screen.getByText("发生了什么")).toBeInTheDocument();
    expect(screen.getByText("独立原始源")).toBeInTheDocument();
    expect(screen.getAllByText("AI 引用")).toHaveLength(2);
    expect(screen.getByText("来源权威度")).toBeInTheDocument();
    expect(screen.getByText("采集链：reuters")).toBeInTheDocument();
    expect(screen.getByText(/原始出处：Reuters · reuters.com/)).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "查看原文" })[0]).toHaveAttribute(
      "href",
      "https://www.reuters.com/world/story",
    );
  });

  it("renders an explicit empty state", async () => {
    server.use(
      http.get(/.*\/api\/news\/stories$/, () =>
        HttpResponse.json({ ok: true, data: { items: [], next_cursor: null } }),
      ),
    );
    renderNews(<NewsPage token="test-token" />);
    expect(await screen.findByText("暂无 Story")).toBeInTheDocument();
  });

  it("renders an explicit request-error state", async () => {
    server.use(
      http.get(/.*\/api\/news\/stories$/, () =>
        HttpResponse.json({ ok: false, error: "feed unavailable" }, { status: 503 }),
      ),
    );
    renderNews(<NewsPage token="test-token" />);
    expect(await screen.findByText("请求失败")).toBeInTheDocument();
  });

  it("renders a Story fully when AI is unavailable", async () => {
    server.use(
      http.get(/.*\/api\/news\/stories\/story-global-policy$/, () =>
        HttpResponse.json({
          ok: true,
          data: newsStoryDetailFixture({
            analysis: null,
            analysis_error: null,
            analysis_status: "unavailable",
          }),
        }),
      ),
    );
    renderNews(
      <NewsPage storyId="story-global-policy" token="test-token" />,
      "/news/stories/story-global-policy",
    );
    expect(await screen.findAllByText("AI 未配置")).toHaveLength(2);
    expect(screen.queryByText("发生了什么")).not.toBeInTheDocument();
  });

  it("keeps the last successful Story visible when a refresh fails", async () => {
    const rendered = renderNews(<NewsPage token="test-token" />);
    expect(
      await screen.findByText("Central banks respond to a new global policy shock"),
    ).toBeInTheDocument();
    server.use(
      http.get(/.*\/api\/news\/stories$/, () =>
        HttpResponse.json({ ok: false, error: "refresh failed" }, { status: 503 }),
      ),
    );

    await rendered.queryClient.invalidateQueries({ queryKey: ["news-stories"] });

    expect(
      await screen.findByText("最新更新失败，正在显示上一次成功读取的 Story。"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Central banks respond to a new global policy shock"),
    ).toBeInTheDocument();
  });
});

function renderNews(children: ReactNode, route = "/news") {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const rendered = render(
    <MemoryRouter initialEntries={[route]}>
      <QueryClientProvider client={queryClient}>
        {children}
        <LocationProbe />
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return { ...rendered, queryClient };
}

function LocationProbe() {
  const location = useLocation();
  return <span data-testid="location">{`${location.pathname}${location.search}`}</span>;
}
