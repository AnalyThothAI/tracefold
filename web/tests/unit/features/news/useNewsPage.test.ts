import { useNewsStoriesWithToken } from "@features/news/useNewsPage";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { newsStoryFixture } from "@tests/fixtures/newsFixture";
import { server } from "@tests/msw/server";
import { HttpResponse, http } from "msw";
import { createElement, type ReactNode } from "react";
import { describe, expect, it } from "vitest";

describe("useNewsStoriesWithToken", () => {
  it("loads the Story-first contract", async () => {
    server.use(
      http.get(/.*\/api\/news\/stories$/, () =>
        HttpResponse.json({
          ok: true,
          data: { items: [newsStoryFixture()], next_cursor: null },
        }),
      ),
    );

    const { result } = renderHook(() => useNewsStoriesWithToken("token"), {
      wrapper: wrapper(),
    });

    await waitFor(() => expect(result.current.data?.items[0].story_id).toBe("story-global-policy"));
    expect(result.current.data?.items[0]).toMatchObject({
      analysis_status: "available",
      independent_origin_count: 2,
      verification_status: "corroborated",
    });
  });

  it("requests only the public Story filters", async () => {
    let observedKeys: string[] = [];
    const observedParams: Record<string, string | null> = {};
    server.use(
      http.get(/.*\/api\/news\/stories$/, ({ request }) => {
        const searchParams = new URL(request.url).searchParams;
        observedKeys = [...searchParams.keys()].sort();
        for (const key of ["limit", "q", "source", "verification_status"]) {
          observedParams[key] = searchParams.get(key);
        }
        return HttpResponse.json({ ok: true, data: { items: [], next_cursor: null } });
      }),
    );

    renderHook(
      () =>
        useNewsStoriesWithToken("token", {
          q: "rates",
          source: "Reuters",
          verificationStatus: "corroborated",
        }),
      { wrapper: wrapper() },
    );

    await waitFor(() => expect(observedParams.q).toBe("rates"));
    expect(observedParams).toEqual({
      limit: "50",
      q: "rates",
      source: "Reuters",
      verification_status: "corroborated",
    });
    expect(observedKeys).toEqual(["limit", "q", "source", "verification_status"]);
  });
});

function wrapper() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client: queryClient }, children);
}
