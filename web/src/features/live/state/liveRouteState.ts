import { OBSERVATION_WINDOWS } from "@lib/observationWindows";
import type { WindowKey } from "@lib/types";
import { useMemo } from "react";
import { useSearchParams } from "react-router-dom";

export type LiveRouteState = {
  window: WindowKey;
};

export const LIVE_ROUTE_DEFAULTS: LiveRouteState = {
  window: "1h",
};

export function parseLiveRouteState(searchParams: URLSearchParams): LiveRouteState {
  return {
    window: parseWindow(searchParams.get("window")),
  };
}

export function serializeLiveRouteState(state: LiveRouteState): URLSearchParams {
  const params = new URLSearchParams();
  const normalized = normalizeLiveRouteState(state);
  if (normalized.window !== LIVE_ROUTE_DEFAULTS.window) params.set("window", normalized.window);
  return params;
}

export function liveRouteStateWith(
  state: LiveRouteState,
  patch: Partial<LiveRouteState>,
): LiveRouteState {
  return normalizeLiveRouteState({ ...state, ...patch });
}

export function useLiveRouteState() {
  const [searchParams, replaceUrlSearch] = useSearchParams();
  const routeState = useMemo(() => parseLiveRouteState(searchParams), [searchParams]);
  const update = (patch: Partial<LiveRouteState>) => {
    replaceUrlSearch(serializeLiveRouteState(liveRouteStateWith(routeState, patch)));
  };
  return {
    ...routeState,
    updateWindow: (window: WindowKey) => update({ window }),
  };
}

function normalizeLiveRouteState(routeState: LiveRouteState): LiveRouteState {
  return {
    window: parseWindow(routeState.window),
  };
}

function parseWindow(value: string | null): WindowKey {
  return OBSERVATION_WINDOWS.includes(value as WindowKey)
    ? (value as WindowKey)
    : LIVE_ROUTE_DEFAULTS.window;
}
