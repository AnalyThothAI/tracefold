import { buildSearchCaseView } from "@features/search/model/searchCase";
import { buildTopicBuckets } from "@features/search/model/searchTopicTimeline";
import type { SearchInspectData } from "@lib/types";
import { tokenCaseFixture } from "@tests/fixtures/tokenCaseFixture";
import { describe, expect, it } from "vitest";

describe("buildSearchCaseView", () => {
  it("maps token search facts into the shared case grammar", () => {
    const view = buildSearchCaseView(searchInspectFixture());

    expect(view.resultKind).toBe("token_result");
    expect(view.title).toBe("$HANSA");
    expect(view.official.value).toBe("$HANSA");
    expect(view.official.source).toBe("official");
    expect(view.community.value).toBe("18 posts · 9 authors");
    expect(view.market.value).toBe("-");
    expect(view.resolver.value).toBe("token result");
    expect(view.resolver.detail).toBe("one resolved target");
    expect(view.evidence.value).toBe("3 events");
  });

  it("keeps topic search on resolver and source evidence facts", () => {
    const data = searchInspectFixture();
    data.query.result_kind = "topic_result";
    data.token_result = null;
    data.topic_result = {
      summary: { posts: 2, authors: 2 },
      items: [],
    };

    const view = buildSearchCaseView(data);

    expect(view.resultKind).toBe("topic_result");
    expect(view.community.value).toBe("2 posts · 2 authors");
    expect(view.official.value).toBe("No token identity");
    expect(view.evidence.value).toBe("0 events");
  });

  it("reads token market facts from market_live instead of market candle metadata", () => {
    const data = searchInspectFixture();
    data.token_result!.market_live = {
      status: "ready",
      target_type: "Asset",
      target_id: "asset:solana:hansa",
      price_usd: 0.0078,
      market_cap_usd: 51_000_000,
      observed_at_ms: 1_700_000_000_000,
      provider: "live-market",
    };
    data.token_result!.timeline.market_candles = {
      price_series_type: "anchor_line",
      provider: "candles-identity",
    };

    const view = buildSearchCaseView(data);

    expect(view.market.detail).toBe("live-market");
    expect(view.market.value).toBe("$51M");
  });

  it("builds topic buckets outside the route component", () => {
    const buckets = buildTopicBuckets([
      searchItem("e1", 1_700_000_000_000),
      searchItem("e2", 1_700_000_010_000),
      searchItem("e3", 1_700_004_000_000),
    ]);

    expect(buckets).toEqual([
      { posts: 2, startMs: 1_700_000_000_000 },
      { posts: 1, startMs: 1_700_003_600_000 },
    ]);
  });
});

function searchInspectFixture(): SearchInspectData {
  const dossier = tokenCaseFixture();
  return {
    query: {
      normalized_q: "hansa",
      q: "$HANSA",
      result_kind: "token_result",
      window: "24h",
    },
    resolver: {
      reasons: ["one_resolved_target"],
      selected_target: dossier.target,
      target_candidates: [dossier.target],
    },
    token_result: dossier,
    topic_result: null,
    ambiguous_result: null,
  };
}

function searchItem(eventId: string, receivedAtMs: number) {
  return {
    event: {
      action: "post",
      author_handle: "toly",
      canonical_url: null,
      content: { text: "topic mention" },
      event_id: eventId,
      received_at_ms: receivedAtMs,
      search_text: "topic mention",
      text_clean: "topic mention",
    },
    match_type: "topic",
    match_reasons: ["text_match"],
    route_scores: {},
    score: 1,
  };
}
