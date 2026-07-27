import type { AssetFlowData, WindowKey } from "@lib/types";
import type { TokenRadarVenueFilter } from "@lib/venue";

export const RADAR_HTTP_UNAVAILABLE_AFTER_MS = 30_000;

export type RadarQueryIdentity = {
  venue: TokenRadarVenueFilter;
  window: WindowKey;
};

export type RadarStatusInput = {
  hasRefreshError: boolean;
  hasUsableRows: boolean;
  lastSuccessfulHttpAtMs: number | null;
  projectionStatus: string | null;
  responseIdentityMatches: boolean;
  sourceMaxReceivedAtMs: number | null;
};

export type RadarHealth = "loading" | "healthy" | "delayed" | "unavailable";

export type RadarStatusView = {
  ageSeconds: number | null;
  health: RadarHealth;
};

export function radarIdentityKey(identity: RadarQueryIdentity): string {
  return `${identity.window}:${identity.venue}`;
}

export function radarResponseMatchesIdentity(
  data: AssetFlowData | null | undefined,
  identity: RadarQueryIdentity,
): boolean {
  return Boolean(
    data &&
    data.window === identity.window &&
    data.venue === identity.venue &&
    data.projection?.venue === identity.venue,
  );
}

export function deriveRadarStatus(input: RadarStatusInput, nowMs: number): RadarStatusView {
  const ageSeconds = contentAgeSeconds(input.sourceMaxReceivedAtMs, nowMs);
  if (!input.responseIdentityMatches) {
    return {
      ageSeconds: null,
      health: input.hasRefreshError && !input.hasUsableRows ? "unavailable" : "loading",
    };
  }

  if (
    input.lastSuccessfulHttpAtMs !== null &&
    Math.max(0, nowMs - input.lastSuccessfulHttpAtMs) > RADAR_HTTP_UNAVAILABLE_AFTER_MS
  ) {
    return { ageSeconds, health: "unavailable" };
  }

  if (input.projectionStatus === "failed" && !input.hasUsableRows) {
    return { ageSeconds, health: "unavailable" };
  }

  if (input.hasRefreshError) {
    return {
      ageSeconds,
      health: input.hasUsableRows ? "delayed" : "unavailable",
    };
  }

  if (input.projectionStatus === "fresh" && input.lastSuccessfulHttpAtMs !== null) {
    return { ageSeconds, health: "healthy" };
  }

  if (
    input.hasUsableRows &&
    (input.projectionStatus === "stale" ||
      input.projectionStatus === "pending" ||
      input.projectionStatus === "failed")
  ) {
    return { ageSeconds, health: "delayed" };
  }

  if (input.projectionStatus === "stale") {
    return { ageSeconds, health: "unavailable" };
  }

  return { ageSeconds, health: "loading" };
}

export function formatRadarContentAge(ageSeconds: number): string {
  if (ageSeconds < 60) {
    return `${ageSeconds}s`;
  }
  if (ageSeconds < 3_600) {
    const minutes = Math.floor(ageSeconds / 60);
    return `${minutes}m ${padTwo(ageSeconds % 60)}s`;
  }
  if (ageSeconds < 86_400) {
    const hours = Math.floor(ageSeconds / 3_600);
    const minutes = Math.floor((ageSeconds % 3_600) / 60);
    const seconds = ageSeconds % 60;
    return `${hours}h ${padTwo(minutes)}m ${padTwo(seconds)}s`;
  }
  const days = Math.floor(ageSeconds / 86_400);
  const hours = Math.floor((ageSeconds % 86_400) / 3_600);
  const minutes = Math.floor((ageSeconds % 3_600) / 60);
  const seconds = ageSeconds % 60;
  return `${days}d ${padTwo(hours)}:${padTwo(minutes)}:${padTwo(seconds)}`;
}

function contentAgeSeconds(sourceMaxReceivedAtMs: number | null, nowMs: number): number | null {
  if (!sourceMaxReceivedAtMs || sourceMaxReceivedAtMs <= 0) {
    return null;
  }
  return Math.max(0, Math.floor((nowMs - sourceMaxReceivedAtMs) / 1_000));
}

function padTwo(value: number): string {
  return String(value).padStart(2, "0");
}
