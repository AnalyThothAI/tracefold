import { LivePage, RadarQueue, useTokenRadarQuery } from "@features/live";

import { useShellRouteContext } from "./shellRouteContext";

export function Component() {
  const { bootstrapError, bootstrapLoading, token } = useShellRouteContext();
  const sessionAvailable = Boolean(token);
  const query = useTokenRadarQuery({
    enabled: sessionAvailable && !bootstrapLoading && !bootstrapError,
    token,
  });
  const snapshot = query.data ?? null;
  const error = query.error instanceof Error ? query.error : null;

  return (
    <LivePage>
      <RadarQueue
        bootstrapError={bootstrapError}
        bootstrapLoading={bootstrapLoading}
        error={error}
        isLoading={sessionAvailable && query.isPending}
        isRefreshing={sessionAvailable && query.isFetching}
        snapshot={snapshot}
        onRetry={() => void query.refetch()}
        onSessionRetry={() => globalThis.location.reload()}
        sessionAvailable={sessionAvailable}
      />
    </LivePage>
  );
}
