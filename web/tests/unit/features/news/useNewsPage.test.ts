import { useNewsFeedWithToken, useNewsSourcesWithToken } from "@features/news/useNewsPage";
import { queryKeys } from "@shared/query/queryKeys";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import {
  newsFeedFixture,
  newsSourceFixture,
  newsSourcesFixture,
} from "@tests/fixtures/newsFixture";
import { server } from "@tests/msw/server";
import { HttpResponse, http } from "msw";
import { createElement, type ReactNode } from "react";
import { describe, expect, it } from "vitest";

describe("useNewsFeedWithToken", () => {
  it("separates focus and complete Feed cache identities", () => {
    const focus = queryKeys.newsFeed("", null, null, null, 70, "latest");
    const complete = queryKeys.newsFeed("", null, null, null, null, "latest");

    expect(focus).not.toEqual(complete);
    expect(focus[5]).toBe(70);
    expect(complete[5]).toBeNull();
  });

  it("reads only the WorldMonitor-compatible Feed endpoint", async () => {
    let category: string | null = null;
    let level: string | null = null;
    let limit: string | null = null;
    let q: string | null = null;
    let reportingOrigin: string | null = null;
    let providerScoreGt: string | null = null;
    let sort: string | null = null;
    server.use(
      http.get(/.*\/api\/news\/feed$/, ({ request }) => {
        const params = new URL(request.url).searchParams;
        category = params.get("category");
        level = params.get("level");
        limit = params.get("limit");
        q = params.get("q");
        reportingOrigin = params.get("reporting_origin");
        providerScoreGt = params.get("provider_score_gt");
        sort = params.get("sort");
        return HttpResponse.json({ ok: true, data: newsFeedFixture() });
      }),
    );
    const { result } = renderHook(
      () =>
        useNewsFeedWithToken("token", {
          category: "economic",
          level: "high",
          providerScoreGt: 70,
          q: "bitcoin",
          reportingOrigin: "reuters",
          sort: "latest",
        }),
      { wrapper: wrapper() },
    );
    await waitFor(() => expect(result.current.data?.pages[0].stories).toHaveLength(1));
    expect(category).toBe("economic");
    expect(level).toBe("high");
    expect(limit).toBe("25");
    expect(q).toBe("bitcoin");
    expect(reportingOrigin).toBe("reuters");
    expect(providerScoreGt).toBe("70");
    expect(sort).toBe("latest");
    expect(result.current.data?.pages[0].stories[0].importance_score).toBe(83);
  });

  it("appends public Sources pages in exact server order", async () => {
    server.use(
      http.get(/.*\/api\/news\/sources$/, ({ request }) => {
        const cursor = new URL(request.url).searchParams.get("cursor");
        return HttpResponse.json({
          ok: true,
          data: cursor
            ? newsSourcesFixture({
                items: [newsSourceFixture({ name: "Third", source_id: "source-third" })],
                page: { has_more: false, next_cursor: null, returned_count: 1 },
              })
            : newsSourcesFixture({
                items: [
                  newsSourceFixture({ name: "First", source_id: "source-first" }),
                  newsSourceFixture({ name: "Second", source_id: "source-second" }),
                ],
                page: { has_more: true, next_cursor: "sources-2", returned_count: 2 },
              }),
        });
      }),
    );
    const { result } = renderHook(() => useNewsSourcesWithToken("token"), {
      wrapper: wrapper(),
    });

    await waitFor(() => expect(result.current.data?.pages).toHaveLength(1));
    await result.current.fetchNextPage();
    await waitFor(() => expect(result.current.data?.pages).toHaveLength(2));

    expect(
      result.current.data?.pages.flatMap((page) => page.items.map((item) => item.name)),
    ).toEqual(["First", "Second", "Third"]);
  });
});

function wrapper() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client: queryClient }, children);
}
