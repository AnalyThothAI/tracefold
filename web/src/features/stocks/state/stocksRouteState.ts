import { OBSERVATION_WINDOWS } from "@lib/observationWindows";
import type { WindowKey } from "@lib/types";

export type StocksRouteState = {
  window: WindowKey;
};

export const STOCKS_ROUTE_DEFAULTS: StocksRouteState = {
  window: "1h",
};

export function parseStocksRouteState(searchParams: URLSearchParams): StocksRouteState {
  return {
    window: parseWindow(searchParams.get("window")),
  };
}

export function serializeStocksRouteState(routeState: StocksRouteState): URLSearchParams {
  const params = new URLSearchParams();
  const normalized = {
    window: parseWindow(routeState.window),
  };
  if (normalized.window !== STOCKS_ROUTE_DEFAULTS.window) params.set("window", normalized.window);
  return params;
}

function parseWindow(value: string | null): WindowKey {
  return OBSERVATION_WINDOWS.includes(value as WindowKey)
    ? (value as WindowKey)
    : STOCKS_ROUTE_DEFAULTS.window;
}
