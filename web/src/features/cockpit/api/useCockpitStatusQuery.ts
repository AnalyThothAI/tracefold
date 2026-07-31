import { getApi } from "@lib/api/client";
import type { OpenApiStatusData } from "@lib/types";
import { queryKeys } from "@shared/query/queryKeys";
import { useQuery } from "@tanstack/react-query";

export function useCockpitStatusQuery({ token }: { token: string }) {
  return useQuery({
    queryKey: queryKeys.status(),
    queryFn: () => getApi<OpenApiStatusData>("/api/status", { token }),
    enabled: Boolean(token),
    refetchInterval: 12_000,
  });
}
