import { getApi } from "@lib/api/client";
import { useQuery } from "@tanstack/react-query";

import type {
  MacroModuleId,
  MacroModuleRouteReadData,
  MacroOverviewReadData,
} from "../model/macroTypes";

const MODULE_API_PATHS: Record<MacroModuleId, string> = {
  rates_fed: "/api/macro/rates-fed",
  economy_inflation: "/api/macro/economy-inflation",
  liquidity_funding: "/api/macro/liquidity-funding",
  credit: "/api/macro/credit",
  volatility: "/api/macro/volatility",
  cross_asset: "/api/macro/cross-asset",
};

export function macroSessionBucket(now = new Date()): string {
  const parts = new Intl.DateTimeFormat("en-CA", {
    day: "2-digit",
    hour: "2-digit",
    hour12: false,
    minute: "2-digit",
    month: "2-digit",
    timeZone: "America/New_York",
    year: "numeric",
  }).formatToParts(now);
  const value = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  const date = `${value.year}-${value.month}-${value.day}`;
  const afterCutoff = Number(value.hour) * 60 + Number(value.minute) >= 8 * 60 + 50;
  return `${date}:${afterCutoff ? "post-cutoff" : "pre-cutoff"}`;
}

export function useMacroOverviewQuery(token: string) {
  const bucket = macroSessionBucket();
  return useQuery({
    queryKey: ["macro", "overview", bucket] as const,
    queryFn: async () => {
      const response = await getApi<MacroOverviewReadData>("/api/macro/overview", { token });
      return response.data;
    },
    enabled: Boolean(token),
    refetchInterval: 60_000,
    refetchOnWindowFocus: true,
  });
}

export function useMacroModuleQuery(token: string, moduleId: MacroModuleId) {
  const bucket = macroSessionBucket();
  return useQuery({
    queryKey: ["macro", "module", moduleId, bucket] as const,
    queryFn: async () => {
      const response = await getApi<MacroModuleRouteReadData>(MODULE_API_PATHS[moduleId], {
        token,
      });
      return response.data;
    },
    enabled: Boolean(token),
    refetchInterval: 60_000,
    refetchOnWindowFocus: true,
  });
}
