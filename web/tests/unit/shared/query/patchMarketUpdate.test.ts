import type { ApiResponse, LiveMarketUpdatePayload, TokenCaseDossier } from "@lib/types";
import { patchTokenCaseLiveMarketUpdate } from "@shared/query/patchMarketUpdate";
import { queryKeys } from "@shared/query/queryKeys";
import { QueryClient } from "@tanstack/react-query";
import { tokenCaseFixture } from "@tests/fixtures/tokenCaseFixture";
import { describe, expect, it } from "vitest";

describe("live market update patch", () => {
  it("patches only the matching Token Case dossier", () => {
    const queryClient = new QueryClient();
    const matching = apiResponse(tokenCaseFixture());
    const unrelated = apiResponse({
      ...tokenCaseFixture(),
      target: { ...tokenCaseFixture().target, target_id: "asset:solana:token:other" },
    });
    const matchingKey = queryKeys.tokenCase(`Asset:${matching.data.target.target_id}`, "1h", 24);
    const unrelatedKey = queryKeys.tokenCase("Asset:asset:solana:token:other", "1h", 24);
    queryClient.setQueryData(matchingKey, matching);
    queryClient.setQueryData(unrelatedKey, unrelated);

    patchTokenCaseLiveMarketUpdate(
      queryClient,
      liveMarketUpdate("Asset", matching.data.target.target_id, 123),
    );

    expect(
      queryClient.getQueryData<ApiResponse<TokenCaseDossier>>(matchingKey)?.data.market_live
        .price_usd,
    ).toBe(123);
    expect(
      queryClient.getQueryData<ApiResponse<TokenCaseDossier>>(matchingKey)?.data.market_live.status,
    ).toBe("live");
    expect(queryClient.getQueryData(unrelatedKey)).toBe(unrelated);
  });
});

function apiResponse<T>(data: T): ApiResponse<T> {
  return { ok: true, data };
}

function liveMarketUpdate(
  targetType: string,
  targetId: string,
  price: number,
): LiveMarketUpdatePayload {
  return {
    type: "live_market_update",
    target_type: targetType,
    target_id: targetId,
    market: {
      decision_latest: {
        target_type: targetType,
        target_id: targetId,
        source: "decision_latest",
        price_usd: price,
        price_basis: "usd",
        observed_at_ms: 2,
        received_at_ms: 2,
        provider: "test",
      },
    },
  };
}
