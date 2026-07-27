import { getApi } from "@lib/api/client";
import { keepPreviousData, useQuery } from "@tanstack/react-query";

import type {
  MacroModuleId,
  MacroOverviewReadData,
  MacroTypedModuleReadData,
} from "../model/macroTypes";

const MODULE_API_PATHS: Record<MacroModuleId, string> = {
  rates_fed: "/api/macro/rates-fed",
  economy_inflation: "/api/macro/economy-inflation",
  liquidity_funding: "/api/macro/liquidity-funding",
  credit: "/api/macro/credit",
  volatility: "/api/macro/volatility",
  cross_asset: "/api/macro/cross-asset",
};

export function useMacroOverviewQuery(token: string) {
  return useQuery({
    queryKey: ["macro", "overview"] as const,
    queryFn: async () => {
      const response = await getApi<MacroOverviewReadData>("/api/macro/overview", { token });
      return response.data;
    },
    enabled: Boolean(token),
    placeholderData: keepPreviousData,
    refetchInterval: 60_000,
    refetchOnWindowFocus: true,
  });
}

export function useMacroModuleQuery(token: string, moduleId: MacroModuleId) {
  return useQuery({
    queryKey: ["macro", "module", moduleId] as const,
    queryFn: async () => {
      const response = await getApi<MacroTypedModuleReadData>(MODULE_API_PATHS[moduleId], {
        token,
      });
      return response.data;
    },
    enabled: Boolean(token),
    placeholderData: keepPreviousData,
    refetchInterval: 60_000,
    refetchOnWindowFocus: true,
  });
}
