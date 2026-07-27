import { getApi, getAuthToken } from "@lib/api/client";
import type { SearchInspectData, WindowKey } from "@lib/types";
import { queryKeys } from "@shared/query/queryKeys";
import { useQuery } from "@tanstack/react-query";

type SearchInspectArgs = {
  q: string;
  window: WindowKey;
  token?: string | null;
};

export function useSearchInspectQuery({ q, window, token }: SearchInspectArgs) {
  const requestToken = token ?? getAuthToken();

  return useQuery({
    queryKey: queryKeys.searchInspect(requestToken ?? "", q, window),
    queryFn: () =>
      getApi<SearchInspectData>("/api/search/inspect", {
        token: requestToken ?? undefined,
        params: { q, window, limit: 200 },
      }),
    enabled: Boolean(requestToken && q.trim()),
  });
}
