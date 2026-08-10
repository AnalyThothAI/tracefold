import type { ApiResponse, LiveMarketUpdatePayload, TokenCaseDossier } from "@lib/types";
import type { QueryClient } from "@tanstack/react-query";

import { queryKeys } from "./queryKeys";

export function patchTokenCaseLiveMarketUpdate(
  queryClient: QueryClient,
  update: LiveMarketUpdatePayload,
) {
  queryClient.setQueriesData<ApiResponse<TokenCaseDossier>>(
    { queryKey: queryKeys.tokenCaseRoot() },
    (response) => {
      if (!response?.data || !tokenCaseMatchesMarketUpdate(response.data, update)) {
        return response;
      }
      return {
        ...response,
        data: {
          ...response.data,
          market_live: {
            status: "live",
            target_type: update.target_type,
            target_id: update.target_id,
            ...update.market.decision_latest,
          },
        },
      };
    },
  );
}

function tokenCaseMatchesMarketUpdate(
  dossier: TokenCaseDossier,
  update: LiveMarketUpdatePayload,
): boolean {
  return (
    dossier.target.target_type === update.target_type &&
    dossier.target.target_id === update.target_id
  );
}
