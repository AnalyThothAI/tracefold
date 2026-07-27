import type { WindowKey } from "@lib/types";

const VALID_WINDOWS = new Set<WindowKey>(["5m", "1h", "4h", "24h"]);

export type SearchRouteState = {
  q: string;
  window: WindowKey;
};

export function parseSearchRouteState(params: URLSearchParams): SearchRouteState {
  const windowParam = params.get("window") as WindowKey | null;
  return {
    q: params.get("q")?.trim() ?? "",
    window: windowParam && VALID_WINDOWS.has(windowParam) ? windowParam : "24h",
  };
}

export function serializeSearchRouteState(routeState: SearchRouteState): URLSearchParams {
  const next = new URLSearchParams();
  if (routeState.q.trim()) {
    next.set("q", routeState.q.trim());
  }
  next.set("window", routeState.window);
  return next;
}
