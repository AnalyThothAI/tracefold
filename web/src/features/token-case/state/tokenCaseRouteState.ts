import type { WindowKey } from "@lib/types";

const VALID_WINDOWS = new Set<WindowKey>(["5m", "1h", "4h", "24h"]);

export type TokenCaseRouteState = {
  window: WindowKey;
};

export const TOKEN_CASE_ROUTE_DEFAULTS: TokenCaseRouteState = {
  window: "24h",
};

export function parseTokenCaseRouteState(searchParams: URLSearchParams): TokenCaseRouteState {
  return {
    window: parseWindow(searchParams.get("window")),
  };
}

export function serializeTokenCaseRouteState(routeState: TokenCaseRouteState): URLSearchParams {
  const params = new URLSearchParams();
  const normalized: TokenCaseRouteState = {
    window: parseWindow(routeState.window),
  };
  if (normalized.window !== TOKEN_CASE_ROUTE_DEFAULTS.window) {
    params.set("window", normalized.window);
  }
  return params;
}

function parseWindow(value: string | null): WindowKey {
  return value && VALID_WINDOWS.has(value as WindowKey)
    ? (value as WindowKey)
    : TOKEN_CASE_ROUTE_DEFAULTS.window;
}
