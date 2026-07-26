import { NewsPage } from "@features/news";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import {
  newsBriefPublicationFixture,
  newsGlobalBriefFixture,
  newsStoryDetailFixture,
  newsStoryFixture,
} from "@tests/fixtures/newsFixture";
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
      http.get(/.*\/api\/news\/brief$/, () =>
        HttpResponse.json({ ok: true, data: newsGlobalBriefFixture() }),
      ),
      http.get(/.*\/api\/news\/brief\/history$/, () =>
        HttpResponse.json({
          ok: true,
          data: { items: [newsBriefPublicationFixture()] },
        }),
      ),
    );
  });

  afterEach(cleanup);

  it("renders one Story row with evidence posture, impact, priority, and AI state", async () => {
    renderNews(<NewsPage token="test-token" />);

    expect(screen.getByRole("status", { name: "loading News stories" })).toBeInTheDocument();
    expect(
      await screen.findByText("Central banks respond to a new global policy shock"),
    ).toBeInTheDocument();
    expect(screen.getAllByText("独立多源佐证")).toHaveLength(2);
    expect(screen.getByText("82")).toBeInTheDocument();
    expect(screen.getByText("88")).toBeInTheDocument();
    expect(screen.getByText("AI 分析可用")).toBeInTheDocument();
    expect(screen.getByText("Reuters World")).toBeInTheDocument();
  });

  it("requests only search, source, and evidence-posture filters", async () => {
    const requests: Array<Record<string, string | null>> = [];
    server.use(
      http.get(/.*\/api\/news\/stories$/, ({ request }) => {
        const params = new URL(request.url).searchParams;
        requests.push({
          evidence_posture: params.get("evidence_posture"),
          q: params.get("q"),
          source: params.get("source"),
        });
        return HttpResponse.json({
          ok: true,
          data: { items: [newsStoryFixture()], next_cursor: null },
        });
      }),
    );
    renderNews(<NewsPage token="test-token" />);
    await screen.findByText("Central banks respond to a new global policy shock");

    fireEvent.click(screen.getByRole("button", { name: "独立多源佐证" }));
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
            request.evidence_posture === "independently_corroborated",
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

  it("shows validated Chinese analysis and Article Revision evidence", async () => {
    renderNews(
      <NewsPage storyId="story-global-policy" token="test-token" />,
      "/news/stories/story-global-policy",
    );

    expect(await screen.findByText("中文 Story Analysis")).toBeInTheDocument();
    expect(screen.getByText("发生了什么")).toBeInTheDocument();
    expect(screen.getByText("独立原始源")).toBeInTheDocument();
    expect(screen.getByText("Article 与 Revision 证据")).toBeInTheDocument();
    expect(screen.getByText("Revision 2 · content")).toBeInTheDocument();
    expect(screen.getByText("身份判定")).toBeInTheDocument();
    expect(screen.getByText("材料事件")).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "查看原文" })[0]).toHaveAttribute(
      "href",
      "https://www.reuters.com/world/story",
    );
  });

  it("keeps Story facts readable when no AI publication exists", async () => {
    server.use(
      http.get(/.*\/api\/news\/stories\/story-global-policy$/, () =>
        HttpResponse.json({
          ok: true,
          data: newsStoryDetailFixture({
            analysis: {
              current: null,
              history: [],
              request: null,
              status: "unavailable",
            },
          }),
        }),
      ),
    );
    renderNews(
      <NewsPage storyId="story-global-policy" token="test-token" />,
      "/news/stories/story-global-policy",
    );

    expect(await screen.findAllByText("尚无 AI 分析")).toHaveLength(2);
    expect(screen.getByText("Article 与 Revision 证据")).toBeInTheDocument();
    expect(screen.queryByText("发生了什么")).not.toBeInTheDocument();
  });

  it("renders the validated Global Brief and immutable publication history", async () => {
    renderNews(<NewsPage brief token="test-token" />, "/news/brief");

    expect(await screen.findByText("全球政治经济简报")).toBeInTheDocument();
    expect(screen.getByText(/全球政策冲击正在跨市场传导/)).toBeInTheDocument();
    expect(screen.getByText("主要央行对新的全球政策冲击作出回应。")).toBeInTheDocument();
    expect(screen.getByText(/deepseek-v4-flash/)).toBeInTheDocument();
  });

  it("falls back to frozen deterministic selection without synthesized prose", async () => {
    server.use(
      http.get(/.*\/api\/news\/brief$/, () =>
        HttpResponse.json({
          ok: true,
          data: {
            current: null,
            fallback: {
              cutoff_at_ms: 1_779_000_000_000,
              decisions: [],
              evidence_bundle: {
                stories: [{ story_id: "story-global-policy", title: "Frozen selection title" }],
              },
              selected_story_ids: ["story-global-policy"],
              selection_fingerprint: "selection-fingerprint",
              selection_snapshot_id: "selection-snapshot",
              status: "selected",
            },
            latest_failure: { error_code: "provider_unavailable" },
          },
        }),
      ),
      http.get(/.*\/api\/news\/brief\/history$/, () =>
        HttpResponse.json({ ok: true, data: { items: [] } }),
      ),
    );
    renderNews(<NewsPage brief token="test-token" />, "/news/brief");

    expect(await screen.findByText("确定性 Brief 选材")).toBeInTheDocument();
    expect(screen.getByText("Frozen selection title")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("最近一次生成失败");
    expect(screen.queryByText("全球政策冲击正在跨市场传导")).not.toBeInTheDocument();
  });

  it("renders explicit empty and request-error states", async () => {
    server.use(
      http.get(/.*\/api\/news\/stories$/, () =>
        HttpResponse.json({ ok: true, data: { items: [], next_cursor: null } }),
      ),
    );
    const empty = renderNews(<NewsPage token="test-token" />);
    expect(await screen.findByText("暂无 Story")).toBeInTheDocument();
    empty.unmount();

    server.use(
      http.get(/.*\/api\/news\/stories$/, () =>
        HttpResponse.json({ ok: false, error: "feed unavailable" }, { status: 503 }),
      ),
    );
    renderNews(<NewsPage token="test-token" />);
    expect(await screen.findByText("请求失败")).toBeInTheDocument();
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
