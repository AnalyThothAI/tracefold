import type { WindowKey } from "@lib/types";

const VALID_WINDOWS = new Set<WindowKey>(["5m", "1h", "4h", "24h"]);

export type TokenCaseRouteState = {
  window: WindowKey;
  focus: "trigger" | null;
  triggerEventId: string | null;
};

export const TOKEN_CASE_ROUTE_DEFAULTS: TokenCaseRouteState = {
  window: "24h",
  focus: null,
  triggerEventId: null,
};

export function parseTokenCaseRouteState(searchParams: URLSearchParams): TokenCaseRouteState {
  const triggerEventId = cleanText(searchParams.get("trigger_event_id"));
  const hasTriggerFocus = searchParams.get("focus") === "trigger" && Boolean(triggerEventId);
  return {
    window: parseWindow(searchParams.get("window")),
    focus: hasTriggerFocus ? "trigger" : null,
    triggerEventId: hasTriggerFocus ? triggerEventId : null,
  };
}

export function serializeTokenCaseRouteState(routeState: TokenCaseRouteState): URLSearchParams {
  const params = new URLSearchParams();
  const normalized: TokenCaseRouteState = {
    window: parseWindow(routeState.window),
    focus: routeState.focus === "trigger" && routeState.triggerEventId ? "trigger" : null,
    triggerEventId: routeState.focus === "trigger" ? cleanText(routeState.triggerEventId) : null,
  };
  if (normalized.window !== TOKEN_CASE_ROUTE_DEFAULTS.window) {
    params.set("window", normalized.window);
  }
  if (normalized.focus && normalized.triggerEventId) {
    params.set("focus", normalized.focus);
    params.set("trigger_event_id", normalized.triggerEventId);
  }
  return params;
}

function parseWindow(value: string | null): WindowKey {
  return value && VALID_WINDOWS.has(value as WindowKey)
    ? (value as WindowKey)
    : TOKEN_CASE_ROUTE_DEFAULTS.window;
}

function cleanText(value: string | null): string | null {
  const normalized = value?.trim();
  return normalized ? normalized : null;
}
