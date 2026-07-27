import { TokenCaseRoute } from "@features/token-case";
import { setAuthToken } from "@lib/api/client";
import type { TokenCaseDossier } from "@lib/types";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { tokenCaseFixture } from "@tests/fixtures/tokenCaseFixture";
import { createApiMock, ok, resetApiMock } from "@tests/msw/fixtures";
import { apiHandlers } from "@tests/msw/handlers";
import { server } from "@tests/msw/server";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

const apiMock = createApiMock();
const targetId = "asset:solana:token:FhoxjfsuStvRQKRXSuB9ZDB7WRGjqhUPxa3NztWspump";

beforeEach(() => {
  setAuthToken("test-token");
  resetApiMock(apiMock);
  apiMock.readApiImpl = async (path) => {
    if (path === "/api/token-case") return ok(routeTokenCaseFixture());
    throw new Error(`unexpected path ${path}`);
  };
  server.use(...apiHandlers(apiMock));
});

function routeTokenCaseFixture(): TokenCaseDossier {
  const dossier = tokenCaseFixture();
  return {
    ...dossier,
    timeline: {
      ...dossier.timeline,
      query: {
        ...dossier.timeline.query,
        window: "24h",
      },
    },
    posts: {
      ...dossier.posts,
      query: {
        ...dossier.posts.query,
        window: "24h",
      },
    },
  };
}

afterEach(() => {
  setAuthToken(null);
  cleanup();
});

function renderAt(url: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[url]}>
        <Routes>
          <Route path="/token/:targetType/:targetId" element={<TokenCaseRoute />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("TokenCaseRoute", () => {
  it("loads the token-case dossier for the route target without token-radar", async () => {
    renderAt(`/token/Asset/${encodeURIComponent(targetId)}?window=24h`);

    expect(await screen.findByRole("region", { name: /Token case/i })).toBeInTheDocument();
    expect(screen.getByText("#3")).toBeInTheDocument();
    expect(screen.getByText("resolved")).toBeInTheDocument();
    expect(screen.getByText("watch")).toBeInTheDocument();

    await waitFor(() => {
      expect(apiMock.getApi).toHaveBeenCalledWith(
        "/api/token-case",
        expect.objectContaining({
          params: expect.objectContaining({
            target_type: "Asset",
            target_id: targetId,
            window: "24h",
            posts_limit: "24",
          }),
        }),
      );
    });
    expect(apiMock.getApi.mock.calls.filter(([path]) => path === "/api/token-radar")).toHaveLength(
      0,
    );
  });
});
