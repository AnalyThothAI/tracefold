import type { components } from "./openapi";

export type { components, operations, paths } from "./openapi";

export type OpenApiBootstrapData = components["schemas"]["BootstrapData"];
export type OpenApiStatusData = components["schemas"]["StatusData"];
export type OpenApiRecentData = components["schemas"]["RecentData"];
export type OpenApiSearchData = components["schemas"]["SearchData"];
export type OpenApiSearchInspectData = components["schemas"]["SearchInspectData"];
export type OpenApiStocksRadarData = components["schemas"]["StocksRadarData"];
export type OpenApiLiveMarketData = components["schemas"]["LiveMarketData"];
export type OpenApiTargetPostsData = components["schemas"]["TargetPostsData"];
export type OpenApiTargetSocialTimelineData = components["schemas"]["TargetSocialTimelineData"];

// frontend-contracts: these UI/domain shapes still encode frontend-specific view models
// that are richer than the current extensible OpenAPI response schemas.
export type {
  AlertRecord,
  ApiResponse,
  BootstrapData,
  EntityRecord,
  EventRecord,
  LiveMarketUpdatePayload,
  LivePayload,
  LiveMarketSnapshot,
  MarketCandle,
  MarketObservationSnapshot,
  RecentData,
  ScoreBlock,
  ScoreContribution,
  SearchAmbiguousResult,
  SearchData,
  SearchInspectData,
  SearchItem,
  SearchTargetCandidate,
  SearchTopicResult,
  SearchTokenResult,
  StockRadarRow,
  StocksRadarData,
  TimelineBucket,
  TokenCaseDossier,
  TokenCasePostsData,
  TokenCasePostsQuery,
  TokenCaseSocialTimelineData,
  TokenCaseSocialTimelineQuery,
  TokenIntentRecord,
  TokenPostItem,
  TokenPostRange,
  TokenPostsData,
  TokenProfileBlock,
  TokenReference,
  TokenResolutionRecord,
  TokenSocialTimelineData,
  TokenTimelineStage,
  TokenTimelinePost,
  WindowKey,
} from "./frontend-contracts";
