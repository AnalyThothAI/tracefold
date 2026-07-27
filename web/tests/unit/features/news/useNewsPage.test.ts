import { useNewsFeedWithToken } from "@features/news/useNewsPage";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { newsFeedFixture } from "@tests/fixtures/newsFixture";
import { server } from "@tests/msw/server";
import { HttpResponse, http } from "msw";
import { createElement, type ReactNode } from "react";
import { describe, expect, it } from "vitest";

describe("useNewsFeedWithToken", () => {
  it("reads only the WorldMonitor-compatible Feed endpoint", async () => {
    let category: string | null = null;
    let sort: string | null = null;
    server.use(
      http.get(/.*\/api\/news\/feed$/, ({ request }) => {
        category = new URL(request.url).searchParams.get("category");
        sort = new URL(request.url).searchParams.get("sort");
        return HttpResponse.json({ ok: true, data: newsFeedFixture() });
      }),
    );
    const { result } = renderHook(() => useNewsFeedWithToken("token", "economic", "latest"), {
      wrapper: wrapper(),
    });
    await waitFor(() => expect(result.current.data?.pages[0].stories).toHaveLength(1));
    expect(category).toBe("economic");
    expect(sort).toBe("latest");
    expect(result.current.data?.pages[0].stories[0].importance_score).toBe(83);
  });
});

function wrapper() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client: queryClient }, children);
}
