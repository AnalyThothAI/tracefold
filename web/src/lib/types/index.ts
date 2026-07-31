import type { components } from "./openapi";

export type { components, operations, paths } from "./openapi";

export type OpenApiBootstrapData = components["schemas"]["BootstrapData"];
export type OpenApiStatusData = components["schemas"]["StatusData"];
export type OpenApiRecentData = components["schemas"]["RecentData"];
export type OpenApiSearchData = components["schemas"]["SearchData"];
export type OpenApiSearchInspectData = components["schemas"]["SearchInspectData"];
export type OpenApiTokenRadarData = components["schemas"]["TokenRadarData"];
export type OpenApiStocksRadarData = components["schemas"]["StocksRadarData"];
export type OpenApiLiveMarketData = components["schemas"]["LiveMarketData"];
export type OpenApiTargetPostsData = components["schemas"]["TargetPostsData"];
export type OpenApiTargetSocialTimelineData = components["schemas"]["TargetSocialTimelineData"];

// frontend-contracts: these UI/domain shapes still encode frontend-specific view models
// that are richer than the current extensible OpenAPI response schemas.
export type {
  AlertRecord,
  ApiResponse,
  AssetFlowData,
  AssetFlowRow,
  BootstrapData,
  Decision,
  EntityRecord,
  EventRecord,
  FactorPoint,
  LiveMarketUpdatePayload,
  LivePayload,
  LiveMarketSnapshot,
  MarketCandle,
  MarketContext,
  MarketObservationSnapshot,
  RecentData,
  SearchAmbiguousResult,
  SearchData,
  SearchInspectData,
  SearchItem,
  SearchTargetCandidate,
  SearchTopicResult,
  SearchTokenResult,
  ScoreBlock,
  ScoreContribution,
  SourceEventDetail,
  SourceEventsByIdsData,
  StockRadarRow,
  StocksRadarData,
  TimelineBucket,
  TimingBlock,
  TokenDetailMode,
  TokenCaseDossier,
  TokenCasePostsData,
  TokenCasePostsQuery,
  TokenCaseSocialTimelineData,
  TokenCaseSocialTimelineQuery,
  TokenFactorFamily,
  TokenFactorFamilyKey,
  TokenFactorSnapshot,
  TokenFlowItem,
  TokenIntentRecord,
  TokenMarketBlock,
  TokenPostItem,
  TokenPostRange,
  TokenPostsData,
  TokenProfileBlock,
  TokenRadarFactRow,
  TokenRadarRowMeta,
  TokenReference,
  TokenResolutionRecord,
  TokenSocialTimelineData,
  TokenTimelineStage,
  TokenTimelinePost,
  TradeabilityBlock,
  WindowKey,
} from "./frontend-contracts";
