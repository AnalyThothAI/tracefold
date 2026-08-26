import {
  useNewsEventWithToken,
  useNewsFeedWithToken,
  useNewsStatusWithToken,
} from "@features/news/api/newsQueries";
import { queryKeys } from "@shared/query/queryKeys";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import {
  newsEventDetailFixture,
  newsFeedFixture,
  newsStatusFixture,
} from "@tests/fixtures/newsFixture";
import { server } from "@tests/msw/server";
import { HttpResponse, http } from "msw";
import { createElement, type ReactNode } from "react";
import { describe, expect, it } from "vitest";

const baseFilters = {
  admission: null,
  decision: null,
  directions: [],
  family: null,
  hours: null,
  outcome: null,
  channels: [],
  q: "",
  symbol: null,
} as const;

describe("useNewsFeedWithToken", () => {
  it("separates Feed cache identities by every server filter", () => {
    const latest = queryKeys.newsFeed(baseFilters);
    const pushed = queryKeys.newsFeed({ ...baseFilters, decision: "push" });

    expect(latest).not.toEqual(pushed);
    expect(latest[0]).toBe("news-feed");
    expect(pushed[4]).toBe("push");
    const held = queryKeys.newsFeed({ ...baseFilters, hours: 6, outcome: "held" });
    expect(held).not.toEqual(latest);
    expect(held[6]).toBe("held");
    expect(held[7]).toBe("6");
  });

  it("reads the Event Feed endpoint with exact server filter names", async () => {
    const observed: Record<string, string | null> = {};
    server.use(
      http.get(/.*\/api\/news\/feed$/, ({ request }) => {
        const params = new URL(request.url).searchParams;
        for (const name of [
          "admission",
          "decision",
          "family",
          "limit",
          "q",
          "symbol",
          "cursor",
          "outcome",
          "hours",
          "direction",
          "channel",
        ]) {
          observed[name] = params.get(name);
        }
        return HttpResponse.json({ ok: true, data: newsFeedFixture() });
      }),
    );
    const { result } = renderHook(
      () =>
        useNewsFeedWithToken("token", {
          admission: "candidate",
          decision: "push",
          directions: ["bullish", "neutral"],
          family: "general",
          hours: 24,
          outcome: "pushed",
          channels: ["news"],
          q: "bitcoin",
          symbol: "BTC",
        }),
      { wrapper: wrapper() },
    );
    await waitFor(() => expect(result.current.data?.events).toHaveLength(1));
    expect(observed).toEqual({
      admission: "candidate",
      cursor: null,
      decision: "push",
      direction: "bullish,neutral",
      family: "general",
      hours: "24",
      limit: "25",
      outcome: "pushed",
      channel: "news",
      q: "bitcoin",
      symbol: "BTC",
    });
    expect(result.current.data?.events[0].triage?.final_decision).toBe("push");
  });

  it("reads one Event detail by encoded id", async () => {
    let requestedPath: string | null = null;
    server.use(
      http.get(/.*\/api\/news\/events\/.+$/, ({ request }) => {
        requestedPath = new URL(request.url).pathname;
        return HttpResponse.json({ ok: true, data: newsEventDetailFixture() });
      }),
    );
    const { result } = renderHook(() => useNewsEventWithToken("token", "evt/with slash"), {
      wrapper: wrapper(),
    });

    await waitFor(() => expect(result.current.data?.event.event_id).toBe("evt-global-policy"));
    expect(requestedPath).toBe("/api/news/events/evt%2Fwith%20slash");
    expect(result.current.data?.verdicts).toHaveLength(1);
    expect(result.current.data?.deliveries[0].state).toBe("sent");
  });

  it("reads the single status document without a view parameter", async () => {
    let view: string | null = "unset";
    server.use(
      http.get(/.*\/api\/news\/status$/, ({ request }) => {
        view = new URL(request.url).searchParams.get("view");
        return HttpResponse.json({ ok: true, data: newsStatusFixture() });
      }),
    );
    const { result } = renderHook(() => useNewsStatusWithToken("token"), {
      wrapper: wrapper(),
    });

    await waitFor(() => expect(result.current.data?.state).toBe("ready"));
    expect(view).toBeNull();
    expect(result.current.data?.ingest.connected).toBe(true);
    expect(result.current.data?.pipeline.triage_p95_ms).toBe(1_900);
  });
});

function wrapper() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client: queryClient }, children);
}
