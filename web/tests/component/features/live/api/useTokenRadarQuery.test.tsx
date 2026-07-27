import { useTokenRadarQuery } from "@features/live";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

describe("useTokenRadarQuery", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("requests the server-ranked token radar venue", async () => {
    const requests: string[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = new URL(input instanceof Request ? input.url : String(input));
      requests.push(`${url.pathname}?${url.searchParams.toString()}`);
      return jsonResponse({
        ok: true,
        data: {
          window: "4h",
          targets: [],
          attention: [],
        },
      });
    });

    renderHook(
      () =>
        useTokenRadarQuery({
          enabled: true,
          limit: 48,
          token: "secret",
          venue: "bsc",
          window: "4h",
        }),
      { wrapper: wrapper() },
    );

    await waitFor(() =>
      expect(requests).toEqual(["/api/token-radar?window=4h&limit=48&venue=bsc"]),
    );
  });
});

function wrapper() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
}

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    headers: { "content-type": "application/json" },
    status: 200,
  });
}
