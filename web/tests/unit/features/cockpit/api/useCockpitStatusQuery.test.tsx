import { useCockpitStatusQuery } from "@features/cockpit/api/useCockpitStatusQuery";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { appStatusFixture } from "@tests/fixtures/appRouteFixtures";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

describe("useCockpitStatusQuery", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("surfaces a malformed fixed status payload as a query error", async () => {
    const payload = { ...appStatusFixture() } as Record<string, unknown>;
    delete payload.provider_states;
    vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse({ ok: true, data: payload }));

    const { result } = renderHook(() => useCockpitStatusQuery({ token: "secret" }), {
      wrapper: wrapper(),
    });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error).toHaveProperty(
      "message",
      "status_current_contract:status.provider_states",
    );
  });
});

function wrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  });
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
