import { RadarPage, RadarQueue, useTokenRadarQuery } from "@features/live";
import { radarScrollTopFromState } from "@shared/routing/radarNavigationState";
import { useLocation } from "react-router-dom";

import { useShellRouteContext } from "./shellRouteContext";

export function Component() {
  const location = useLocation();
  const { bootstrapError, bootstrapLoading, token } = useShellRouteContext();
  const sessionAvailable = Boolean(token);
  const query = useTokenRadarQuery({
    enabled: sessionAvailable && !bootstrapLoading && !bootstrapError,
    token,
  });
  const snapshot = query.data ?? null;
  const error = query.error instanceof Error ? query.error : null;

  return (
    <RadarPage>
      <RadarQueue
        bootstrapError={bootstrapError}
        bootstrapLoading={bootstrapLoading}
        error={error}
        isLoading={sessionAvailable && query.isPending}
        isRefreshing={sessionAvailable && query.isFetching}
        initialScrollTop={radarScrollTopFromState(location.state)}
        snapshot={snapshot}
        onRetry={() => void query.refetch()}
        onSessionRetry={() => globalThis.location.reload()}
        sessionAvailable={sessionAvailable}
      />
    </RadarPage>
  );
}
