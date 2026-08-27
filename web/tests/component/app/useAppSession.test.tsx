import { useAppSession } from "@app/useAppSession";
import { getApi, setAuthToken } from "@lib/api/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { server } from "@tests/msw/server";
import { HttpResponse, http } from "msw";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it } from "vitest";

afterEach(() => setAuthToken(null));

describe("application session", () => {
  it("installs the bootstrap token as the bearer for subsequent API reads", async () => {
    let authorization: string | null = null;
    server.use(
      http.get(/.*\/api\/bootstrap$/, () =>
        HttpResponse.json({ ok: true, data: { ws_token: "session-token" } }),
      ),
      http.get(/.*\/api\/status$/, ({ request }) => {
        authorization = request.headers.get("authorization");
        return HttpResponse.json({ ok: true, data: { runtime: { ok: true } } });
      }),
    );

    const { result } = renderHook(useAppSession, { wrapper: queryWrapper() });
    await waitFor(() => expect(result.current.token).toBe("session-token"));
    await getApi("/api/status");

    expect(authorization).toBe("Bearer session-token");
  });
});

function queryWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  });
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  };
}
