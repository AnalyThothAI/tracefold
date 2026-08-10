import { getApi } from "@lib/api/client";
import { queryKeys } from "@shared/query/queryKeys";
import { useQuery } from "@tanstack/react-query";

import { parseTokenRadarSnapshot, type TokenRadarSnapshot } from "../model/tokenRadarSnapshot";

export function useTokenRadarQuery({
  token,
  enabled = true,
}: {
  token: string;
  enabled?: boolean;
}) {
  return useQuery({
    queryKey: queryKeys.tokenRadar(),
    queryFn: async (): Promise<TokenRadarSnapshot> => {
      const response = await getApi<unknown>("/api/token-radar", {
        etagKey: "token-radar-snapshot",
        token,
      });
      return parseTokenRadarSnapshot(response.data);
    },
    enabled: Boolean(token) && enabled,
    refetchInterval: 30_000,
    staleTime: 15_000,
  });
}
