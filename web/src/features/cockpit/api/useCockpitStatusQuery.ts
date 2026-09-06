import { getApi } from "@lib/api/client";
import type { OpenApiStatusData } from "@lib/types";
import { queryKeys } from "@shared/query/queryKeys";
import { useQuery } from "@tanstack/react-query";

/**
 * The shell's own runtime row, on the console's 15 s rhythm — the same interval as the News status page
 * (`NEWS_STATUS_REFETCH_MS`) and the Trading desk (`TRADING_REFETCH_MS`), which is what a reader sees as
 * "the shell refreshed". `/api/status` folds the database probe with the Workers heartbeat, and the
 * Workers write that heartbeat on their own cadence, so a faster poll only re-renders the same row.
 */
export const COCKPIT_STATUS_REFETCH_MS = 15_000;

export function useCockpitStatusQuery({ token }: { token: string }) {
  return useQuery({
    queryKey: queryKeys.status(),
    queryFn: () => getApi<OpenApiStatusData>("/api/status", { token }),
    enabled: Boolean(token),
    refetchInterval: COCKPIT_STATUS_REFETCH_MS,
  });
}
