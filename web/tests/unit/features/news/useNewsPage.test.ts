import {
  useNewsEventWithToken,
  useNewsFeedWindowWithToken,
  useNewsStatusWithToken,
  uniqueFeedEvents,
  type NewsFeedFilters,
} from "@features/news/api/newsQueries";
import { queryKeys } from "@shared/query/queryKeys";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import {
  newsEventDetailFixture,
  newsFeedFixture,
  newsFeedEventFixture,
  newsStatusFixture,
} from "@tests/fixtures/newsFixture";
import { server } from "@tests/msw/server";
import { HttpResponse, http } from "msw";
import { createElement, type ReactNode } from "react";
import { describe, expect, it } from "vitest";

const baseFilters: NewsFeedFilters = {
  admission: null,
  eventKinds: [],
  hours: null,
  outcome: null,
  q: "",
  sourceAuthorities: [],
  subjectCodes: [],
  symbol: null,
};

describe("useNewsFeedWindowWithToken", () => {
  it("keeps at most three cursor pages and prefers the newest copy of an overlapping Event", async () => {
    const requested: string[] = [];
    server.use(
      http.get(/.*\/api\/news\/feed$/, ({ request }) => {
        const cursor = new URL(request.url).searchParams.get("cursor") ?? "first";
        requested.push(cursor);
        const page = requested.length;
        return HttpResponse.json({
          ok: true,
          data: newsFeedFixture({
            events: [newsFeedEventFixture({ event_id: `event-${page}` })],
            next_cursor: page < 4 ? `cursor-${page}` : null,
            counts: page === 1 ? newsFeedFixture().counts : null,
          }),
        });
      }),
    );
    const { result } = renderHook(() => useNewsFeedWindowWithToken("token", baseFilters), {
      wrapper: wrapper(),
    });
    await waitFor(() => expect(result.current.query.data?.pages).toHaveLength(1));
    for (let page = 2; page <= 4; page += 1) {
      await act(async () => {
        await result.current.query.fetchNextPage();
      });
      await waitFor(() =>
        expect(result.current.query.data?.pages.at(-1)?.events[0].event_id).toBe(`event-${page}`),
      );
    }
    expect(requested).toEqual(["first", "cursor-1", "cursor-2", "cursor-3"]);
    expect(result.current.query.data?.pages).toHaveLength(3);
    expect(result.current.olderWindow).toBe(true);
    const fresh = newsFeedEventFixture({ event_id: "same", leader_title: "new" });
    const stale = newsFeedEventFixture({ event_id: "same", leader_title: "old" });
    expect(
      uniqueFeedEvents([
        newsFeedFixture({ events: [fresh] }),
        newsFeedFixture({ events: [stale] }),
      ]),
    ).toEqual([fresh]);
  });

  it("separates Feed cache identities by every server filter", () => {
    const latest = queryKeys.newsFeedWindow(baseFilters, 0);
    const pushed = queryKeys.newsFeedWindow({ ...baseFilters, outcome: "pushed" }, 0);

    expect(latest).not.toEqual(pushed);
    expect(latest[0]).toBe("news-feed-window");
    expect(pushed).toContain("pushed");
    const held = queryKeys.newsFeedWindow({ ...baseFilters, hours: 6, outcome: "held" }, 0);
    expect(held).not.toEqual(latest);
    expect(held).toContain("held");
    expect(held).toContain("6");
    for (const filtered of [
      { ...baseFilters, sourceAuthorities: ["issuer_first_party"] as const },
      { ...baseFilters, subjectCodes: ["medtop:04000000"] as const },
      { ...baseFilters, eventKinds: ["news"] as const },
    ]) {
      expect(queryKeys.newsFeedWindow(filtered, 0)).not.toEqual(latest);
    }
  });

  it("reads the Event Feed endpoint with exact server filter names", async () => {
    const observed: Record<string, string | null> = {};
    server.use(
      http.get(/.*\/api\/news\/feed$/, ({ request }) => {
        const params = new URL(request.url).searchParams;
        for (const name of [
          "admission",
          // #706: the three retired taxonomy axes, which the browser must never send again.
          "assertion_status",
          "change_state",
          "event_family",
          "event_kind",
          "final_decision",
          "limit",
          "q",
          "symbol",
          "cursor",
          "outcome",
          "hours",
          "direction",
          "source_authority",
          "subject_code",
        ]) {
          observed[name] = params.get(name);
        }
        return HttpResponse.json({ ok: true, data: newsFeedFixture() });
      }),
    );
    const { result } = renderHook(
      () =>
        useNewsFeedWindowWithToken("token", {
          admission: "candidate",
          eventKinds: ["news"],
          hours: 24,
          outcome: "pushed",
          q: "bitcoin",
          sourceAuthorities: ["issuer_first_party"],
          subjectCodes: ["medtop:04000000"],
          symbol: "BTC",
        }),
      { wrapper: wrapper() },
    );
    await waitFor(() => expect(result.current.query.data?.pages[0].events).toHaveLength(1));
    expect(observed).toEqual({
      admission: "candidate",
      assertion_status: null,
      change_state: null,
      cursor: null,
      direction: null,
      event_family: null,
      event_kind: "news",
      final_decision: null,
      hours: "24",
      limit: "25",
      outcome: "pushed",
      q: "bitcoin",
      source_authority: "issuer_first_party",
      subject_code: "medtop:04000000",
      symbol: "BTC",
    });
    expect(result.current.query.data?.pages[0].events[0].update?.headline_source).toBe("sent_card");
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
    expect(result.current.data?.event_update?.claims.length).toBeGreaterThan(0);
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
    expect(result.current.data?.pipeline.semantic_adopted_24h).toBe(150);
  });
});

function wrapper() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client: queryClient }, children);
}
