import { getApi } from "@lib/api/client";
import { useQuery } from "@tanstack/react-query";

import { MACRO_MODULES } from "../model/macroModules";
import type {
  MacroModuleId,
  MacroModuleRouteReadDataFor,
  MacroOverviewReadData,
} from "../model/macroTypes";

export function useMacroOverviewQuery(token: string) {
  return useQuery({
    queryKey: ["macro", "overview"] as const,
    queryFn: async () => {
      const response = await getApi<MacroOverviewReadData>("/api/macro/overview", {
        etagKey: "macro:overview",
        token,
      });
      return response.data;
    },
    enabled: Boolean(token),
    refetchInterval: 60_000,
    refetchOnWindowFocus: true,
    staleTime: 30_000,
  });
}

export function useMacroModuleQuery<ModuleId extends MacroModuleId>(
  token: string,
  moduleId: ModuleId,
) {
  const definition = MACRO_MODULES[moduleId];
  return useQuery({
    queryKey: ["macro", "module", moduleId] as const,
    queryFn: async () => {
      const response = await getApi<MacroModuleRouteReadDataFor<ModuleId>>(definition.apiPath, {
        etagKey: `macro:${moduleId}`,
        token,
      });
      return assertModuleContract(response.data, moduleId);
    },
    enabled: Boolean(token),
    refetchInterval: 60_000,
    refetchOnWindowFocus: true,
    staleTime: 30_000,
  });
}

function assertModuleContract<ModuleId extends MacroModuleId>(
  data: MacroModuleRouteReadDataFor<ModuleId>,
  moduleId: ModuleId,
): MacroModuleRouteReadDataFor<ModuleId> {
  const expected = MACRO_MODULES[moduleId];
  if (
    data.module_id !== moduleId ||
    (data.availability === "available" && data.schema_version !== expected.schemaVersion) ||
    (data.availability === "unavailable" && data.schema_version !== "macro_module_unavailable_v1")
  ) {
    throw new Error(
      `Macro endpoint contract mismatch: expected ${moduleId}/${expected.schemaVersion}.`,
    );
  }
  return data;
}
