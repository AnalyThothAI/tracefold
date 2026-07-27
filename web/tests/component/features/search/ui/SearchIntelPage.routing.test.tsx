import { SearchIntelPage } from "@features/search";
import { setAuthToken } from "@lib/api/client";
import type { SearchInspectData } from "@lib/types";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { tokenCaseFixture } from "@tests/fixtures/tokenCaseFixture";
import { createApiMock, ok, resetApiMock } from "@tests/msw/fixtures";
import { apiHandlers } from "@tests/msw/handlers";
import { server } from "@tests/msw/server";
import { axe } from "jest-axe";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

const apiMock = createApiMock();

beforeEach(() => {
  setAuthToken("test-token");
  resetApiMock(apiMock);
  apiMock.readApiImpl = async () => ok(searchInspectData());
  server.use(...apiHandlers(apiMock));
});

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
          <Route path="/search" element={<SearchIntelPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("SearchIntelPage", () => {
  it("loads search inspect data from the route and renders token evidence", async () => {
    const { container } = renderAt("/search?q=%24RKC&window=24h");

    expect(await screen.findByRole("heading", { name: "Search Intel" })).toBeInTheDocument();
    expect(await screen.findByRole("region", { name: /Token case/i })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: /\$RKC/i })).toBeInTheDocument();
    expect(screen.getByText("Mention Timeline")).toBeInTheDocument();
    expect(screen.getByText("Live Market")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
    expect(screen.getAllByText(/Runtime source fact/).length).toBeGreaterThan(0);
    expect(screen.queryByRole("navigation", { name: "Search sections" })).not.toBeInTheDocument();
    expect(screen.queryByText("candidates")).not.toBeInTheDocument();
    expect(screen.queryByText("94% confidence")).not.toBeInTheDocument();
    expect(screen.queryByText(["search", "content", "grid"].join("-"))).not.toBeInTheDocument();
    expect(container.querySelector(".search-sidebar-candidates")).not.toBeInTheDocument();
    const resolver = screen.getByLabelText("Search resolver");
    expect(within(resolver).getByText("token_result")).toBeInTheDocument();
    expect(within(resolver).getByText("one_resolved_target")).toBeInTheDocument();

    await waitFor(() => {
      expect(apiMock.readApi).toHaveBeenCalledWith(
        "/api/search/inspect",
        expect.objectContaining({
          params: expect.objectContaining({ q: "$RKC", window: "24h", limit: 200 }),
        }),
      );
    });
    expect(apiMock.getApi.mock.calls.filter(([path]) => path === "/api/token-case")).toHaveLength(
      0,
    );
    expect(await axe(container)).toHaveNoViolations();
  }, 10_000);

  it("keeps candidate compare for ambiguous search results", async () => {
    const base = searchInspectData();
    const data: SearchInspectData = {
      ...base,
      query: { ...base.query, result_kind: "ambiguous_result" },
      token_result: null,
      ambiguous_result: {
        candidates: [
          {
            target_type: "Asset",
            target_id: "asset:solana:rkc",
            symbol: "RKC",
            status: "resolved",
            source: "asset_identity_current",
            reason: "CANONICAL_SYMBOL_MATCH",
          },
          {
            target_type: "Asset",
            target_id: "asset:solana:rocky",
            symbol: "ROCKY",
            status: "candidate",
            source: "asset_identity_current",
            reason: "FUZZY_SYMBOL_MATCH",
          },
        ],
        summary: { posts: 9, authors: 4 },
        items: [],
      },
    };
    apiMock.readApiImpl = async () => ok(data);

    renderAt("/search?q=%24RKC&window=24h");

    expect(await screen.findByText("Ambiguous query")).toBeInTheDocument();
    const compare = screen.getByRole("heading", { name: "Candidate Compare" }).closest("section");
    expect(compare).toBeTruthy();
    expect(within(compare as HTMLElement).getByText("$RKC")).toBeInTheDocument();
    expect(within(compare as HTMLElement).getByText("$ROCKY")).toBeInTheDocument();
  });
});

function searchInspectData(): SearchInspectData {
  const tokenResult = tokenCaseFixture();
  return {
    query: {
      q: "$RKC",
      normalized_q: "rkc",
      window: "24h",
      result_kind: "token_result",
    },
    resolver: {
      target_candidates: [
        {
          target_type: "Asset",
          target_id: "asset:solana:rkc",
          symbol: "RKC",
          status: "resolved",
          source: "asset_identity_current",
          reason: "CANONICAL_SYMBOL_MATCH",
        },
      ],
      selected_target: {
        target_type: "Asset",
        target_id: "asset:solana:rkc",
        symbol: "RKC",
        status: "resolved",
        source: "asset_identity_current",
        reason: "CANONICAL_SYMBOL_MATCH",
      },
      reasons: ["one_resolved_target"],
    },
    token_result: {
      ...tokenResult,
      target: {
        target_type: "Asset",
        target_id: "asset:solana:rkc",
        symbol: "RKC",
        status: "resolved",
        source: "asset_identity_current",
        reason: "CANONICAL_SYMBOL_MATCH",
      },
      timeline: {
        ...tokenResult.timeline,
        query: {
          target_type: "Asset",
          target_id: "asset:solana:rkc",
          window: "24h",
          bucket: "1h",
        },
        summary: {
          posts: 73,
          authors: 18,
          effective_authors: 18,
          phase: "expansion",
          top_author_share: 0.22,
          duplicate_text_share: 0.12,
          peak_posts_per_bucket: 12,
          peak_new_authors_per_bucket: 5,
          reproduction_rate: null,
        },
        market_candles: {},
        stages: tokenResult.timeline.stages,
        buckets: [
          {
            start_ms: 1,
            end_ms: 2,
            posts: 8,
            authors: 4,
            new_authors: 4,
            duplicate_text_share: 0,
            price: null,
            price_change_from_start_pct: null,
          },
        ],
        authors: tokenResult.timeline.authors,
        posts: tokenResult.timeline.posts,
        cascade: tokenResult.timeline.cascade,
        returned_count: tokenResult.timeline.returned_count,
        has_more: tokenResult.timeline.has_more,
      },
      posts: {
        ...tokenResult.posts,
        query: {
          target_type: "Asset",
          target_id: "asset:solana:rkc",
          window: "24h",
          range: "current_window",
        },
        score_window: { window: "24h" },
        total_count: tokenResult.posts.total_count,
        returned_count: tokenResult.posts.returned_count,
        has_more: true,
        items: tokenResult.posts.items.map((item) => ({
          ...item,
          text:
            item.event_id === "event-hansa-3" ? "Runtime source fact validates $RKC" : item.text,
        })),
      },
      market_live: {
        status: "missing",
        target_type: "Asset",
        target_id: "asset:solana:rkc",
        price_usd: null,
        market_cap_usd: null,
        liquidity_usd: null,
        holders: null,
        observed_at_ms: null,
        provider: null,
      },
      profile: {
        status: "ready",
        provider: "gmgn",
        identity: {
          symbol: "RKC",
          name: "Runtime Coin",
          logo_url: null,
          description: "Runtime token profile",
        },
        links: {
          website_url: "https://rkc.example",
          twitter_url: "https://x.com/rkc",
          gmgn_url: "https://gmgn.ai/sol/token/rkc",
          geckoterminal_url: "https://www.geckoterminal.com/solana/pools/rkc",
        },
        source: {
          provider: "gmgn",
          raw_available: true,
          last_error: null,
        },
      },
    },
    topic_result: null,
    ambiguous_result: null,
  };
}
