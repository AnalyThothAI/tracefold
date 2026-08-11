import { useMacroModuleQuery } from "@features/macro";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { macroModuleFixture } from "@tests/fixtures/macroFixture";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("useMacroModuleQuery", () => {
  it("fails closed when the endpoint returns a different module contract", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ ok: true, data: macroModuleFixture("credit") }),
    );

    const { result } = renderHook(() => useMacroModuleQuery("secret", "rates_fed"), {
      wrapper: wrapper(),
    });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.data).toBeUndefined();
    expect(result.current.error).toMatchObject({
      message: "Macro endpoint contract mismatch: expected rates_fed/macro_rates_fed_v8.",
    });
  });

  it("revalidates with ETag and retains current facts when a refresh fails", async () => {
    const requests: Headers[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (_input, init) => {
      requests.push(new Headers(init?.headers));
      if (requests.length === 1) {
        return jsonResponse(
          { ok: true, data: macroModuleFixture("rates_fed") },
          { ETag: '"macro-rates-1"' },
        );
      }
      if (requests.length === 2) return new Response(null, { status: 304 });
      return new Response(JSON.stringify({ ok: false, error: "refresh unavailable" }), {
        headers: { "content-type": "application/json" },
        status: 503,
      });
    });

    const { result } = renderHook(() => useMacroModuleQuery("secret", "rates_fed"), {
      wrapper: wrapper(),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const current = result.current.data;

    await act(async () => {
      await result.current.refetch();
    });

    expect(requests[1]?.get("if-none-match")).toBe('"macro-rates-1"');
    expect(result.current.data).toBe(current);

    let refreshError: Error | null = null;
    await act(async () => {
      const refresh = await result.current.refetch();
      refreshError = refresh.error instanceof Error ? refresh.error : null;
    });

    expect(refreshError).toMatchObject({ message: "refresh unavailable" });
    expect(result.current.data).toBe(current);
  });
});

function wrapper() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
}

function jsonResponse(body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    headers: { "content-type": "application/json", ...headers },
    status: 200,
  });
}
