import type { TokenCaseDossier } from "@lib/types";
import { cleanup, screen, waitFor } from "@testing-library/react";
import { tokenCaseFixture } from "@tests/fixtures/tokenCaseFixture";
import { ok } from "@tests/msw/fixtures";
import { mockAppRoutes } from "@tests/msw/scenarios";
import { renderAppRoute } from "@tests/render/renderRoute";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { apiMock, setupAppRouteTest } from "./routeTestSetup";

describe("token target route", () => {
  afterEach(() => {
    cleanup();
  });

  beforeEach(() => {
    setupAppRouteTest((mock) => {
      mockAppRoutes(mock);
      const baseGetApi = mock.getApiImpl;
      mock.getApiImpl = async (path, options) => {
        if (path === "/api/token-case") return ok(routeTokenCaseFixture());
        return baseGetApi(path, options);
      };
    });
  });

  it("renders the token case route as a standalone case file", async () => {
    const targetId = "asset:solana:token:FhoxjfsuStvRQKRXSuB9ZDB7WRGjqhUPxa3NztWspump";

    renderAppRoute(`/token/Asset/${encodeURIComponent(targetId)}?window=24h`);

    expect(await screen.findByRole("region", { name: /Token case/i })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: /\$HANSA/i })).toBeInTheDocument();
    expect(screen.getByText("Mention Timeline")).toBeInTheDocument();
    expect(screen.queryByText("score audit")).not.toBeInTheDocument();

    await waitFor(() => {
      expect(apiMock.getApi).toHaveBeenCalledWith(
        "/api/token-case",
        expect.objectContaining({
          params: expect.objectContaining({
            target_type: "Asset",
            target_id: targetId,
            window: "24h",
          }),
        }),
      );
    });
  });
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
