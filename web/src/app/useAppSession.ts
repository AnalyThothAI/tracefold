import { getBootstrap, setAuthToken } from "@lib/api/client";
import { queryKeys } from "@shared/query/queryKeys";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";

export function useAppSession() {
  const [token, setToken] = useState("");
  const bootstrapQuery = useQuery({
    queryKey: queryKeys.bootstrap(),
    queryFn: getBootstrap,
    staleTime: Infinity,
  });

  useEffect(() => {
    const wsToken = bootstrapQuery.data?.data.ws_token;
    if (!wsToken) return;
    setAuthToken(wsToken);
    setToken(wsToken);
  }, [bootstrapQuery.data?.data.ws_token]);

  return useMemo(
    () => ({
      bootstrapError: bootstrapQuery.isError,
      bootstrapFailure: bootstrapQuery.error,
      bootstrapLoading: bootstrapQuery.isPending,
      retryBootstrap: bootstrapQuery.refetch,
      token,
    }),
    [
      bootstrapQuery.error,
      bootstrapQuery.isError,
      bootstrapQuery.isPending,
      bootstrapQuery.refetch,
      token,
    ],
  );
}

export type AppSession = ReturnType<typeof useAppSession>;
