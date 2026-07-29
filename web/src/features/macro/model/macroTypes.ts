import type { components } from "@lib/types/openapi";

type MacroSchemas = components["schemas"];

export type JsonObject = Record<string, unknown>;

export type MacroReason = MacroSchemas["MacroReason"];
export type MacroCondition = MacroSchemas["MacroCompiledCondition"];
export type MacroModuleId = MacroSchemas["MacroDraftModuleAssessment"]["module_id"];

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

export type MacroTypedModuleReadData =
  | MacroRatesFedReadData
  | MacroEconomyInflationReadData
  | MacroLiquidityFundingReadData
  | MacroCreditReadData
  | MacroVolatilityReadData
  | MacroCrossAssetReadData;

export type MacroModuleUnavailableReadData = MacroSchemas["MacroModuleUnavailableData"];
export type MacroModuleRouteReadData = MacroTypedModuleReadData | MacroModuleUnavailableReadData;

export type MacroThesisV1 = MacroSchemas["MacroThesisV1"];
export type MacroThesisV2 = MacroSchemas["MacroThesisV2"];
export type MacroArchiveThesis = MacroThesisV1 | MacroThesisV2;
export type MacroLiveDeltaV2 = MacroSchemas["MacroLiveDeltaV2"];
export type MacroOutcomeReplayV2 = MacroSchemas["MacroOutcomeReplayV2"];
export type MacroRecoveryItem = MacroSchemas["MacroRecoveryItem"];

export type MacroModuleSummary = MacroSchemas["MacroModuleSummaryData"];
export type MacroThesisRunData = MacroSchemas["MacroThesisRunData"];
export type MacroThesisState = MacroSchemas["MacroOverviewReadData"]["thesis_state"];
export type MacroOverviewReadData = MacroSchemas["MacroOverviewReadData"];
export type MacroThesisDetailReadData = MacroSchemas["MacroThesisDetailReadData"];
export type MacroThesisArchiveDetailReadData = MacroSchemas["MacroThesisArchiveDetailReadData"];
export type MacroResearchReadData = MacroThesisDetailReadData | MacroThesisArchiveDetailReadData;
export type MacroPublicationHistoryItem = MacroSchemas["MacroPublicationHistoryItemData"];
