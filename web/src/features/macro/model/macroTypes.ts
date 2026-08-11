import type { components } from "@lib/types/openapi";

type MacroSchemas = components["schemas"];

export type MacroReason = MacroSchemas["MacroReason"];
export type MacroModuleId = MacroSchemas["MacroModuleUnavailableData"]["module_id"];

export type MacroCoverageCapability = MacroSchemas["MacroCoverageCapabilityData"];
export type MacroCoverage = MacroSchemas["MacroCoverageData"];
export type MacroCoverageState = MacroCoverage["state"];

export type MacroCurrentHealthGroup = MacroSchemas["MacroCurrentHealthGroupData"];
export type MacroCurrentHealth = MacroSchemas["MacroCurrentHealthData"];
export type MacroCurrentHealthState = MacroCurrentHealth["state"];

export type MacroHistoryDepth = MacroSchemas["MacroHistoryDepthData"];
export type MacroHistoryDepthState = MacroHistoryDepth["state"];
export type MacroBackfillExecution = MacroSchemas["MacroBackfillExecutionData"];

export type MacroChange = MacroSchemas["MacroChangeData"];
export type MacroDatasetState = MacroSchemas["MacroDatasetStateData"];
export type MacroEvidenceFact = MacroSchemas["MacroEvidenceFactData"];

export type MacroRatesFedReadData = MacroSchemas["MacroRatesFedReadData"];
export type MacroEconomyInflationReadData = MacroSchemas["MacroEconomyInflationReadData"];
export type MacroLiquidityFundingReadData = MacroSchemas["MacroLiquidityFundingReadData"];
export type MacroCreditReadData = MacroSchemas["MacroCreditReadData"];
export type MacroVolatilityReadData = MacroSchemas["MacroVolatilityReadData"];
export type MacroCrossAssetReadData = MacroSchemas["MacroCrossAssetReadData"];
export type MacroCreditCycleDimension = MacroSchemas["MacroCreditCycleDimensionData"];
export type MacroFedTimelineEvent = MacroSchemas["MacroFedTimelineEventData"];
export type MacroSettlement = MacroSchemas["MacroSettlementData"];

export type MacroTypedModuleReadData =
  | MacroRatesFedReadData
  | MacroEconomyInflationReadData
  | MacroLiquidityFundingReadData
  | MacroCreditReadData
  | MacroVolatilityReadData
  | MacroCrossAssetReadData;

export type MacroModuleUnavailableReadData = MacroSchemas["MacroModuleUnavailableData"];
export type MacroModuleRouteReadData = MacroTypedModuleReadData | MacroModuleUnavailableReadData;

export type MacroAvailableModuleById = {
  [ModuleId in MacroModuleId]: Extract<MacroTypedModuleReadData, { module_id: ModuleId }>;
};

export type MacroModuleRouteReadDataFor<ModuleId extends MacroModuleId> =
  | MacroAvailableModuleById[ModuleId]
  | (Omit<MacroModuleUnavailableReadData, "module_id"> & { module_id: ModuleId });

export type MacroModuleSummary = MacroSchemas["MacroModuleSummaryData"];
export type MacroOverviewReadData = MacroSchemas["MacroOverviewReadData"];
