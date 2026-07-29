import type { components } from "@lib/types/openapi";

type MacroSchemas = components["schemas"];

/**
 * Local helper for extensible module payload fragments. All public HTTP shapes
 * below are aliases to the generated OpenAPI contract.
 */
export type JsonObject = Record<string, unknown>;

export type MacroReason = MacroSchemas["MacroReason"];
export type MacroCondition = MacroSchemas["MacroCondition"];
export type MacroModuleId = MacroCondition["module_id"];

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

/** Local union used by the shared module workbench. Its members are generated. */
export type MacroTypedModuleReadData =
  | MacroRatesFedReadData
  | MacroEconomyInflationReadData
  | MacroLiquidityFundingReadData
  | MacroCreditReadData
  | MacroVolatilityReadData
  | MacroCrossAssetReadData;

export type MacroModuleUnavailableReadData = MacroSchemas["MacroModuleUnavailableData"];

/** Local union matching the six module endpoints' generated response members. */
export type MacroModuleRouteReadData = MacroTypedModuleReadData | MacroModuleUnavailableReadData;

export type MacroThesisClaim = MacroSchemas["MacroThesisClaim"];
export type MacroModuleRole = MacroSchemas["MacroModuleRole"];
export type MacroMomentum = MacroSchemas["MacroMomentum"];
export type MacroHorizonOutlook = MacroSchemas["MacroHorizonOutlook"];
export type MacroAssetView = MacroSchemas["MacroAssetView"];
export type MacroThesisV1 = MacroSchemas["MacroThesisV1"];

export type MacroLiveDeltaItem = MacroSchemas["MacroLiveDeltaItemRead"];
export type MacroLiveDeltaReadData = MacroSchemas["MacroLiveDeltaRead"];
export type MacroAssetHorizonPresentation = MacroSchemas["MacroAssetHorizonPresentation"];
export type MacroAssetPresentation = MacroSchemas["MacroAssetPresentation"];
export type MacroClaimPresentation = MacroSchemas["MacroClaimPresentation"];
export type MacroOutcomeReplayReadData = MacroSchemas["MacroOutcomeReplayRead"];

export type MacroModuleSummary = MacroSchemas["MacroModuleSummaryData"];
export type MacroPublicationFallback = MacroSchemas["MacroPublicationFallbackContextData"];
export type MacroThesisRunData = MacroSchemas["MacroThesisRunData"];
export type MacroThesisState = MacroSchemas["MacroOverviewReadData"]["thesis_state"];
export type MacroOverviewReadData = MacroSchemas["MacroOverviewReadData"];
export type MacroThesisDetailReadData = MacroSchemas["MacroThesisDetailReadData"];
