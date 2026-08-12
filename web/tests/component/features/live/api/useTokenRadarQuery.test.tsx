import { useTokenRadarQuery } from "@features/live";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("useTokenRadarQuery", () => {
  it("reads the exact snapshot without query params and conditionally refreshes every 30 seconds", async () => {
    vi.useFakeTimers();
    const requests: Array<{ headers: Headers; url: URL }> = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = new URL(input instanceof Request ? input.url : String(input));
      const headers = new Headers(init?.headers);
      requests.push({ headers, url });
      if (requests.length === 1) {
        return jsonResponse(snapshot(), { ETag: '"radar-1"' });
      }
      if (requests.length === 2) return new Response(null, { status: 304 });
      return new Response(JSON.stringify({ ok: false, error: "refresh unavailable" }), {
        headers: { "content-type": "application/json" },
        status: 503,
      });
    });

    const { result } = renderHook(() => useTokenRadarQuery({ enabled: true, token: "secret" }), {
      wrapper: wrapper(),
    });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(result.current.data?.eligible_total).toBe(1);
    expect(requests[0]?.url.pathname).toBe("/api/token-radar");
    expect(requests[0]?.url.search).toBe("");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
      await vi.advanceTimersByTimeAsync(0);
    });

    expect(requests).toHaveLength(2);
    expect(requests[1]?.headers.get("if-none-match")).toBe('"radar-1"');
    expect(result.current.data).toEqual(snapshot().data);

    let refreshError: Error | null = null;
    await act(async () => {
      const refresh = await result.current.refetch();
      refreshError = refresh.error instanceof Error ? refresh.error : null;
    });

    expect(requests).toHaveLength(3);
    expect(refreshError).toMatchObject({ message: "refresh unavailable" });
    expect(result.current.data).toEqual(snapshot().data);
  });
});

function wrapper() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
}

function snapshot() {
  return {
    ok: true,
    data: {
      schema_version: "token_radar_snapshot_v4",
      state: "current",
      stale_reason: null,
      state_changed_at_ms: 1_778_426_420_000,
      social_evidence_as_of_ms: 1_778_426_440_000,
      eligible_total: 1,
      items: [
        {
          target: {
            target_type: "Asset",
            target_id: "asset:solana:token:abc",
            symbol: "UPEG",
            name: "Unpegged Token",
            logo_url: `/api/token-images/${"a".repeat(64)}`,
            chain: "solana",
            exchange: null,
            address: "abc",
          },
          trigger_event_id: "event-1",
          trigger_source_event_at_ms: 1_778_426_430_000,
          qualified_at_ms: 1_778_426_435_000,
          why_now: { current_mentions: 7, prior_mentions: 2, mention_delta: 5 },
          evidence: {
            independent_author_count: 4,
            independent_text_count: 5,
            time_to_nth_author_ms: 90_000,
            duplicate_share: 0.1,
          },
          market: {
            price_usd: 0.042,
            price_observed_at_ms: 1_778_426_439_000,
            price_change_since_signal: 0.12,
            market_cap_usd: 42_000_000,
            market_cap_observed_at_ms: 1_778_426_438_000,
          },
        },
      ],
    },
  } as const;
}

function jsonResponse(body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    headers: { "content-type": "application/json", ...headers },
    status: 200,
  });
}
