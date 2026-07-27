import { countDecisions, tokenRadarItems } from "@lib/tokenRadar";
import type { AssetFlowData, TokenFlowItem, WindowKey } from "@lib/types";
import type { TokenRadarVenueFilter } from "@lib/venue";
import { useEffect, useMemo, useRef, useState } from "react";

import { targetRefFromTokenItem } from "../../../domain/tokenTarget";
import { radarResponseMatchesIdentity, type RadarStatusInput } from "../model/radarContentStatus";

import { useTokenRadarQuery } from "./useTokenRadarQuery";

type RadarFrame = {
  rawTokenItems: TokenFlowItem[];
  responseIdentityMatches: boolean;
  sourceMaxReceivedAtMs: number | null;
  tokenItems: TokenFlowItem[];
};

export function useLiveRadarRouteData({
  enabled = true,
  token,
  window,
}: {
  enabled?: boolean;
  token: string;
  window: WindowKey;
}) {
  const [venueFilter, setVenueFilter] = useState<TokenRadarVenueFilter>("all");
  const assetFlowQuery = useTokenRadarQuery({
    token,
    window,
    venue: venueFilter,
    limit: 48,
    enabled,
  });
  const lastReadyFrames = useRef(new Map<string, RadarFrame>());
  const cacheKey = `${window}:${venueFilter}`;
  const responseData = assetFlowQuery.data?.data;
  const responseIdentityMatches = radarResponseMatchesIdentity(responseData, {
    venue: venueFilter,
    window,
  });
  const projectionStatus = responseIdentityMatches
    ? (responseData?.projection?.status ?? null)
    : null;
  const projectionPending = projectionStatus === "pending";
  const parsed = useMemo(() => parseTokenRadarItems(responseData, window), [responseData, window]);
  const currentFrame = useMemo(
    () => ({
      rawTokenItems: parsed.items,
      responseIdentityMatches,
      sourceMaxReceivedAtMs: responseIdentityMatches
        ? positiveTimestamp(responseData?.projection?.source_max_received_at_ms)
        : null,
      tokenItems: parsed.items,
    }),
    [parsed.items, responseData?.projection?.source_max_received_at_ms, responseIdentityMatches],
  );
  const cachedFrame = lastReadyFrames.current.get(cacheKey);
  const queryError = assetFlowQuery.error instanceof Error ? assetFlowQuery.error : null;
  const hasRefreshError = Boolean(queryError || parsed.error);
  const projectionDegraded =
    projectionStatus === "stale" || projectionStatus === "pending" || projectionStatus === "failed";
  const shouldUseCachedFrame =
    Boolean(cachedFrame) &&
    (!responseIdentityMatches ||
      hasRefreshError ||
      projectionDegraded ||
      (assetFlowQuery.isFetching &&
        currentFrame.tokenItems.length === 0 &&
        !assetFlowQuery.isPending));
  const displayedFrame = shouldUseCachedFrame && cachedFrame ? cachedFrame : currentFrame;
  const rawTokenItems = displayedFrame.rawTokenItems;
  const tokenItems = displayedFrame.tokenItems;
  const hasUsableRows =
    displayedFrame.responseIdentityMatches && displayedFrame.tokenItems.length > 0;
  const marketTargets = useMemo(
    () =>
      rawTokenItems.flatMap((item) => {
        const target = targetRefFromTokenItem(item);
        return target ? [target] : [];
      }),
    [rawTokenItems],
  );
  const decisionCounts = useMemo(() => countDecisions(tokenItems), [tokenItems]);
  const projectionError =
    responseIdentityMatches &&
    !hasUsableRows &&
    (projectionStatus === "failed" || projectionStatus === "stale")
      ? new Error(
          responseData?.projection?.error ??
            responseData?.projection?.reason ??
            "Token Radar projection is unavailable.",
        )
      : null;
  const assetFlowError = hasUsableRows ? null : (queryError ?? parsed.error ?? projectionError);
  const isAssetFlowLoading =
    !assetFlowError &&
    tokenItems.length === 0 &&
    (assetFlowQuery.isPending ||
      assetFlowQuery.fetchStatus === "fetching" ||
      projectionPending ||
      !responseIdentityMatches);
  const isAssetFlowRefreshing =
    !assetFlowError &&
    hasUsableRows &&
    (assetFlowQuery.isFetching || projectionPending || projectionStatus === "stale");
  const radarStatus: RadarStatusInput = {
    hasRefreshError,
    hasUsableRows,
    lastSuccessfulHttpAtMs: assetFlowQuery.lastSuccessfulHttpAtMs,
    projectionStatus,
    responseIdentityMatches: displayedFrame.responseIdentityMatches,
    sourceMaxReceivedAtMs: displayedFrame.sourceMaxReceivedAtMs,
  };

  useEffect(() => {
    if (
      parsed.error ||
      !responseIdentityMatches ||
      projectionStatus !== "fresh" ||
      currentFrame.tokenItems.length === 0
    ) {
      return;
    }
    lastReadyFrames.current.set(cacheKey, currentFrame);
  }, [cacheKey, currentFrame, parsed.error, projectionStatus, responseIdentityMatches]);

  return {
    assetFlowError,
    decisionCounts,
    isAssetFlowLoading,
    isAssetFlowRefreshing,
    marketTargets,
    projectionStatus,
    radarStatus,
    setVenueFilter,
    tokenItems,
    venueFilter,
  };
}

function positiveTimestamp(value: number | null | undefined): number | null {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : null;
}

function parseTokenRadarItems(
  data: AssetFlowData | null | undefined,
  window: WindowKey,
): { items: TokenFlowItem[]; error: Error | null } {
  try {
    return { items: tokenRadarItems(data, window), error: null };
  } catch (error) {
    return {
      items: [],
      error:
        error instanceof Error ? error : new Error("Token Radar response could not be parsed."),
    };
  }
}
