import type { SearchInspectData } from "@lib/types";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { tokenCaseFixture } from "@tests/fixtures/tokenCaseFixture";
import { ok } from "@tests/msw/fixtures";
import { mockAppRoutes } from "@tests/msw/scenarios";
import { renderAppRoute } from "@tests/render/renderRoute";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { apiMock, setupAppRouteTest } from "./routeTestSetup";

describe("search route", () => {
  afterEach(() => {
    cleanup();
  });

  beforeEach(() => {
    setupAppRouteTest((mock) => {
      mockAppRoutes(mock);
      const baseGetApi = mock.getApiImpl;
      mock.getApiImpl = async (path, options) => {
        if (path === "/api/search/inspect") {
          return ok(searchTokenInspectFixture(String(options?.params?.q ?? "$RKC")));
        }
        return baseGetApi(path, options);
      };
    });
  });

  it("routes topbar text search into Search Intel", async () => {
    renderAppRoute("/");

    fireEvent.change(await screen.findByLabelText("global search"), {
      target: { value: "$RKC" },
    });
    fireEvent.click(screen.getByRole("button", { name: "检索" }));

    expect(await screen.findByRole("heading", { name: "Search Intel" })).toBeInTheDocument();
    expect(await screen.findByRole("region", { name: /Token case/i })).toBeInTheDocument();
    await waitFor(() => {
      expect(apiMock.readApi).toHaveBeenCalledWith(
        "/api/search/inspect",
        expect.objectContaining({
          params: expect.objectContaining({ q: "$RKC", window: "24h" }),
        }),
      );
    });
    expect(apiMock.readApi.mock.calls.filter(([path]) => path === "/api/token-case")).toHaveLength(
      0,
    );
    expect(screen.queryByText(/Select Token/i)).not.toBeInTheDocument();
  }, 10_000);
});

function searchTokenInspectFixture(q: string): SearchInspectData {
  const tokenResult = tokenCaseFixture();
  return {
    query: {
      q,
      normalized_q: q.replace(/^\$/, "").toLowerCase(),
      window: "24h",
      result_kind: "token_result",
    },
    resolver: {
      target_candidates: [tokenResult.target],
      selected_target: tokenResult.target,
      reasons: ["one_resolved_target"],
    },
    token_result: {
      ...tokenResult,
      target: {
        ...tokenResult.target,
        target_id: "asset:solana:rkc",
        symbol: "RKC",
      },
      timeline: {
        ...tokenResult.timeline,
        query: {
          ...tokenResult.timeline.query,
          target_id: "asset:solana:rkc",
          window: "24h",
        },
      },
      posts: {
        ...tokenResult.posts,
        query: {
          ...tokenResult.posts.query,
          target_id: "asset:solana:rkc",
          window: "24h",
        },
      },
    },
    topic_result: null,
    ambiguous_result: null,
  };
}
