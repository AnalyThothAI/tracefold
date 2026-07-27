import { NewsPage } from "@features/news";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import {
  NEWS_NOW_MS,
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
          data: { items: [newsStoryFixture()], next_cursor: null, view: "latest" },
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

  it("keeps the latest or priority view in the URL and server query", async () => {
    const requests: Array<Record<string, string | null>> = [];
    server.use(
      http.get(/.*\/api\/news\/stories$/, ({ request }) => {
        const params = new URL(request.url).searchParams;
        requests.push({
          evidence_posture: params.get("evidence_posture"),
          q: params.get("q"),
          source: params.get("source"),
          view: params.get("view"),
        });
        return HttpResponse.json({
          ok: true,
          data: {
            items: [newsStoryFixture()],
            next_cursor: null,
            view: params.get("view") === "priority" ? "priority" : "latest",
          },
        });
      }),
    );
    renderNews(<NewsPage token="test-token" />);
    await screen.findByText("Central banks respond to a new global policy shock");

    fireEvent.click(screen.getByRole("button", { name: "独立多源佐证" }));
    fireEvent.click(screen.getByRole("button", { name: "当前优先级" }));
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
            request.evidence_posture === "independently_corroborated" &&
            request.view === "priority",
        ),
      ).toBe(true),
    );
    expect(screen.getByTestId("location")).toHaveTextContent("view=priority");
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
    const current = newsGlobalBriefFixture();
    const active = current.active_selection;
    if (!active) throw new Error("active selection fixture required");
    server.use(
      http.get(/.*\/api\/news\/brief$/, () =>
        HttpResponse.json({
          ok: true,
          data: newsGlobalBriefFixture({
            active_selection: {
              ...active,
              evidence_bundle: {
                ...active.evidence_bundle,
                stories: [
                  {
                    story_id: "story-global-policy",
                    title: "Frozen selection title",
                  },
                ],
              },
            },
            analysis: null,
            analysis_status: "failed",
            latest_failure: {
              activation_id: active.activation_id,
              attempt_count: 1,
              last_error: "provider_unavailable",
              requested_at_ms: 1_779_000_000_000,
              updated_at_ms: 1_779_000_001_000,
              validation_errors: [],
            },
          }),
        }),
      ),
      http.get(/.*\/api\/news\/brief\/history$/, () =>
        HttpResponse.json({ ok: true, data: { items: [] } }),
      ),
    );
    renderNews(<NewsPage brief token="test-token" />, "/news/brief");

    expect(await screen.findByText("Global Brief 当前确定性选材")).toBeInTheDocument();
    expect(screen.getByText("Frozen selection title")).toBeInTheDocument();
    expect(
      screen.getAllByRole("status").some((node) => node.textContent?.includes("最近一次生成失败")),
    ).toBe(true);
    expect(screen.queryByText("全球政策冲击正在跨市场传导")).not.toBeInTheDocument();
  });

  it("separates cached analysis provenance, pending selection, and prior history", async () => {
    const current = newsGlobalBriefFixture();
    const active = current.active_selection;
    const analysis = current.analysis;
    if (!active || !analysis) throw new Error("complete brief fixture required");
    server.use(
      http.get(/.*\/api\/news\/brief$/, () =>
        HttpResponse.json({
          ok: true,
          data: newsGlobalBriefFixture({
            analysis: {
              ...analysis,
              attachment_kind: "reused",
              published_at_ms: NEWS_NOW_MS - 300_000,
            },
            analysis_status: "reused",
            pending_proposal: {
              activation_due_at_ms: NEWS_NOW_MS + 120_000,
              first_proposed_at_ms: NEWS_NOW_MS,
              lane: "ordinary",
              last_observed_at_ms: NEWS_NOW_MS + 30_000,
              proposal_id: "proposal-next",
              selected_story_ids: ["story-next"],
              selection_fingerprint: "selection-next-fingerprint",
              selection_id: "selection-next",
            },
            previous_publication: {
              ...analysis,
              activation_id: "brief-activation-previous",
              activation_sequence: 0,
              payload: {
                ...analysis.payload,
                executive_summary: "这是明确标记为历史参考的上一份分析。",
              },
              publication_id: "brief-publication-previous",
              published_at_ms: NEWS_NOW_MS - 600_000,
            },
          }),
        }),
      ),
      http.get(/.*\/api\/news\/brief\/history$/, () =>
        HttpResponse.json({ ok: true, data: { items: [] } }),
      ),
    );
    renderNews(<NewsPage brief token="test-token" />, "/news/brief");

    expect(await screen.findByText(/复用完全相同输入的历史分析/)).toBeInTheDocument();
    expect(screen.getByText(/新选材正在稳定观察/)).toBeInTheDocument();
    expect(screen.getByText("上一份历史分析")).toBeInTheDocument();
    expect(screen.getByText("这是明确标记为历史参考的上一份分析。")).toBeInTheDocument();
  });

  it("renders explicit empty and request-error states", async () => {
    server.use(
      http.get(/.*\/api\/news\/stories$/, () =>
        HttpResponse.json({ ok: true, data: { items: [], next_cursor: null, view: "latest" } }),
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
