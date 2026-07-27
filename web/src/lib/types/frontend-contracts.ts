export type ApiResponse<T> = {
  ok: boolean;
  data: T;
  error?: string | null;
};

export type WindowKey = "5m" | "1h" | "4h" | "24h";
export type Decision = "driver" | "watch" | "investigate" | "discard";
export type TimelineBucket = "30s" | "5m" | "15m" | "1h";
export type TokenPostRange = "current_window" | "since_ignition" | "all_history";
export type TokenDetailMode = "compact" | "replay";

export type BootstrapData = {
  ws_token: string;
  replay_limit: number;
};

export type EventRecord = {
  event_id: string;
  tweet_id?: string | null;
  action?: string | null;
  canonical_url?: string | null;
  received_at_ms?: number | null;
  author_handle?: string | null;
  text_clean?: string | null;
  search_text?: string | null;
  cashtags?: string[];
  hashtags?: string[];
  mentions?: string[];
  urls?: string[];
  source?: {
    provider?: string | null;
    transport?: string | null;
    coverage?: string | null;
    channel?: string | null;
  } | null;
  author?: {
    handle?: string | null;
    name?: string | null;
    avatar?: string | null;
    followers?: number | null;
    tags?: string[];
  } | null;
  content?: {
    text?: string | null;
  } | null;
};

export type EntityRecord = {
  entity_type: string;
  normalized_value: string;
  chain?: string | null;
  author_handle?: string | null;
  received_at_ms?: number | null;
};

export type AlertRecord = {
  alert_type: string;
  event_id: string;
  author_handle?: string | null;
  entity_key?: string | null;
  entity_type?: string | null;
  normalized_value?: string | null;
  chain?: string | null;
  token_resolution_status?: string | null;
  received_at_ms?: number | null;
  is_first_seen_global?: number | boolean | null;
  is_first_seen_by_author?: number | boolean | null;
  confidence?: number | null;
  summary?: string | null;
  evidence?: string | null;
};

export type TokenIntentRecord = {
  intent_id?: string | null;
  event_id?: string | null;
  display_symbol?: string | null;
  display_name?: string | null;
  chain_hint?: string | null;
  address_hint?: string | null;
  intent_status?: string | null;
  intent_confidence?: number | null;
};

export type TokenResolutionRecord = {
  resolution_id?: string | null;
  intent_id?: string | null;
  event_id?: string | null;
  target_type?: string | null;
  target_id?: string | null;
  symbol?: string | null;
  pricefeed_id?: string | null;
  price?: TokenMessagePrice | null;
  resolution_status?: string | null;
  reason_codes_json?: string[];
  candidate_ids_json?: string[];
  lookup_keys_json?: string[];
};

export type LivePayload = {
  type: "event";
  event: EventRecord;
  entities: EntityRecord[];
  token_intents?: TokenIntentRecord[];
  token_resolutions?: TokenResolutionRecord[];
  harness?: unknown | null;
};

export type LiveMarketUpdatePayload = {
  type: "live_market_update";
  target_type: string;
  target_id: string;
  provider?: string | null;
  observed_at_ms?: number | null;
  market: {
    decision_latest: MarketObservationSnapshot;
  };
};

export type RecentData = {
  events: EventRecord[];
  items: LivePayload[];
};

export type SearchItem = {
  event: EventRecord;
  match_type: string;
  score: number;
  match_reasons: string[];
  target?: SearchTargetCandidate | null;
  route_scores: Record<string, number>;
};

export type SearchTargetCandidate = {
  target_type: "Asset" | "CexToken" | string;
  target_id: string;
  symbol?: string | null;
  chain_id?: string | null;
  address?: string | null;
  status: string;
  source: string;
  reason: string;
};

export type SearchData = {
  query: Record<string, unknown>;
  page: {
    returned_count: number;
    has_more: boolean;
    next_cursor?: string | null;
  };
  target_candidates: SearchTargetCandidate[];
  items: SearchItem[];
};

export type SearchInspectResultKind =
  | "token_result"
  | "topic_result"
  | "ambiguous_result"
  | "empty_result";

export type TokenProfileBlock = {
  status: "ready" | "pending" | "missing" | "unsupported" | "error" | string;
  provider?: string | null;
  observed_at_ms?: number | null;
  identity?: {
    symbol?: string | null;
    name?: string | null;
    logo_url?: string | null;
    banner_url?: string | null;
    description?: string | null;
  } | null;
  links?: {
    website_url?: string | null;
    twitter_url?: string | null;
    twitter_username?: string | null;
    telegram_url?: string | null;
    gmgn_url?: string | null;
    geckoterminal_url?: string | null;
  } | null;
  source?: {
    provider?: string | null;
    source_kind?: string | null;
    source_ref?: string | null;
    quality_flags?: string[] | null;
    raw_available?: boolean | null;
    last_error?: string | null;
  } | null;
};

export type LiveMarketSnapshot = MarketObservationSnapshot & {
  status?: "ready" | "missing" | "unsupported" | "error" | "stale" | string | null;
  error?: string | null;
  message?: string | null;
  stale?: boolean | null;
  readiness?: Partial<MarketReadiness> | null;
  [key: string]: unknown;
};

export type TokenCasePostsQuery = TokenPostsQuery;

export type TokenCasePostsData = Omit<TokenPostsData, "query"> & {
  query: TokenCasePostsQuery;
};

export type TokenCaseSocialTimelineQuery = TokenSocialTimelineQuery;

export type TokenCaseSocialTimelineData = Omit<TokenSocialTimelineData, "query"> & {
  query: TokenCaseSocialTimelineQuery;
};

export type TokenCaseDossier = {
  target: SearchTargetCandidate;
  profile: TokenProfileBlock | null;
  timeline: TokenCaseSocialTimelineData;
  posts: TokenCasePostsData;
  market_live: LiveMarketSnapshot;
  current_radar: TokenRadarFactRow | null;
};

export type SearchTokenResult = TokenCaseDossier;

export type SearchTopicResult = {
  summary: {
    posts: number;
    authors: number;
  };
  items: SearchItem[];
};

export type SearchAmbiguousResult = {
  candidates: SearchTargetCandidate[];
  summary: {
    posts: number;
    authors: number;
  };
  items: SearchItem[];
};

export type SearchInspectData = {
  query: {
    q: string;
    normalized_q: string;
    window: WindowKey;
    result_kind: SearchInspectResultKind;
  };
  resolver: {
    target_candidates: SearchTargetCandidate[];
    selected_target: SearchTargetCandidate | null;
    reasons: string[];
  };
  token_result: SearchTokenResult | null;
  topic_result: SearchTopicResult | null;
  ambiguous_result: SearchAmbiguousResult | null;
};

export type TokenRadarIntentBlock = {
  intent_id: string;
  event_id: string;
  display_symbol?: string | null;
  display_name?: string | null;
  evidence: unknown[];
};

export type MarketObservationSnapshot = {
  target_type?: string | null;
  target_id?: string | null;
  source?: "event_anchor" | "decision_latest" | string | null;
  provider?: string | null;
  pricefeed_id?: string | null;
  price_usd?: number | null;
  price_quote?: number | null;
  quote_symbol?: string | null;
  price_basis?: string | null;
  market_cap_usd?: number | null;
  liquidity_usd?: number | null;
  holders?: number | null;
  volume_24h_usd?: number | null;
  open_interest_usd?: number | null;
  observed_at_ms?: number | null;
  received_at_ms?: number | null;
};

export type MarketReadiness = {
  anchor_status: "ready" | "missing" | "stale" | string;
  latest_status: "live" | "ready" | "stale" | "missing" | string;
  dex_floor_status: "ready" | "missing_fields" | "not_applicable" | string;
  missing_fields: string[];
  stale_fields: string[];
};

export type MarketContext = {
  event_anchor: MarketObservationSnapshot | null;
  decision_latest: MarketObservationSnapshot | null;
  readiness: MarketReadiness;
  capture_method?: string | null;
  capture_reason?: string | null;
  tick_lag_ms?: number | null;
};

export type TokenRadarRowMeta = {
  lane?: "resolved" | "attention" | string | null;
  rank?: number | null;
  listed_at_ms?: number | null;
  computed_at_ms?: number | null;
  source_max_received_at_ms?: number | null;
};

export type TokenRadarFactRow = {
  intent: TokenRadarIntentBlock;
  radar: TokenRadarRowMeta;
  resolution: {
    status: "EXACT" | "UNIQUE_BY_CONTEXT" | "NIL" | "AMBIGUOUS" | string;
    target_type: string | null;
    target_id: string | null;
    pricefeed_id: string | null;
    reason_codes: string[];
    candidate_ids: string[];
    lookup_keys: string[];
    discovery: TokenDiscoveryAudit[];
  };
  factor_snapshot: TokenFactorSnapshot;
  quality: {
    status: string;
    degraded_reasons: string[];
  };
};

export type AssetFlowRow = TokenRadarFactRow & {
  profile?: TokenProfileBlock | null;
};

export type TokenDiscoveryAudit = {
  lookup_key?: string | null;
  lookup_type?: string | null;
  status?: "running" | "found" | "not_found" | "error" | string | null;
  candidate_count?: number | null;
  last_lookup_at_ms?: number | null;
  next_refresh_at_ms?: number | null;
  last_error?: string | null;
  error_count?: number | null;
};

export type AssetFlowData = {
  window: WindowKey;
  venue: string;
  targets: AssetFlowRow[];
  attention: AssetFlowRow[];
  projection: {
    status: "fresh" | "stale" | "pending" | string;
    version: string;
    source: string;
    venue: string;
    reason: string | null;
    latest_attempt_status: string;
    row_count: number;
    source_rows: number;
    source_max_received_at_ms: number;
    source_frontier_ms: number | null;
    computed_at_ms: number | null;
    error: string | null;
    anchor_coverage: {
      status: string;
      ready: number;
      missing: number;
      total: number;
    };
    quality_status: "ready" | "degraded" | "insufficient" | "failed" | string;
    degraded_reasons: string[];
    unresolved: {
      identity_missing_count: number;
      nil_count: number;
      ambiguous_count: number;
      sample_symbols: string[];
    };
  };
};

export type StockQuoteSnapshot = {
  status: "ready" | "unavailable" | string;
  price?: number | null;
  reference_close_price?: number | null;
  change_pct?: number | null;
  asof?: string | null;
  provider?: string | null;
  provider_symbol?: string | null;
  latency_class?: string | null;
  freshness_class?: string | null;
  error?: string | null;
};

export type StockRadarRow = {
  target: {
    target_type: "MarketInstrument" | string;
    target_id: string;
    symbol: string;
    market?: "us_equity" | string | null;
    exchange?: string | null;
    instrument_type?: string | null;
    name?: string | null;
  };
  attention: {
    mentions: number;
    unique_authors: number;
    latest_seen_ms?: number | null;
  };
  latest_event: {
    event_id?: string | null;
    author_handle?: string | null;
    text?: string | null;
    received_at_ms?: number | null;
  };
  quote: StockQuoteSnapshot;
  source_event_ids: string[];
  row_health: string[];
};

export type StocksRadarData = {
  window: WindowKey;
  query: {
    window: WindowKey;
    limit: number;
    window_start_ms: number;
    window_end_ms: number;
  };
  rows: StockRadarRow[];
  health: {
    returned_count: number;
    quote_ready_count: number;
    quote_unavailable_count: number;
  };
};

export type ScoreContribution = {
  feature: string;
  value: number;
  reason: string;
};

export type RiskCap = {
  risk: string;
  cap: number;
};

export type ScoreBlock = {
  score: number;
  score_version: string;
  reasons: string[];
  risks: string[];
  contributions: ScoreContribution[];
  risk_caps: RiskCap[];
  data_health?: Record<string, unknown>;
};

export type TokenIdentityBlock = {
  identity_key: string;
  identity_status: "resolved_ca" | string;
  target_type?: string | null;
  target_id?: string | null;
  asset_id?: string | null;
  asset_type?: string | null;
  venue_type?: string | null;
  exchange?: string | null;
  inst_id?: string | null;
  inst_type?: string | null;
  chain?: string | null;
  address?: string | null;
  symbol?: string | null;
  resolution_reasons?: string[];
  lookup_keys?: string[];
  candidate_count?: number;
  discovery_status?: string | null;
};

export type TokenMarketBlock = {
  event_anchor: MarketObservationSnapshot | null;
  decision_latest: MarketObservationSnapshot | null;
  readiness: MarketReadiness;
  market_status: "fresh" | "partial" | "stale" | "missing" | string;
  price?: number | null;
  price_status?: string | null;
  market_cap?: number | null;
  market_cap_status?: string | null;
  liquidity?: number | null;
  liquidity_status?: string | null;
  pool_status?: "ready" | "missing" | string;
  holder_count?: number | null;
  holder_count_status?: string | null;
  volume_24h?: number | null;
  volume_24h_status?: string | null;
  provider?: string | null;
  snapshot_age_ms?: number | null;
  snapshot_received_at_ms?: number | null;
  social_signal_start_ms?: number | null;
  reference_ms?: number | null;
  price_at_social_start?: number | null;
  price_at_reference?: number | null;
  price_change_since_social_pct?: number | null;
  price_before_social_start?: number | null;
  price_change_before_social_pct?: number | null;
  price_at_first_snapshot?: number | null;
  first_snapshot_observed_at_ms?: number | null;
  price_change_since_first_snapshot_pct?: number | null;
  market_observation_status?:
    | "ready"
    | "pending"
    | "running"
    | "provider_not_configured"
    | "provider_not_found"
    | "provider_error"
    | "rate_limited"
    | "dead"
    | string;
  price_change_status:
    | "ready"
    | "pending_observation"
    | "insufficient_history"
    | "missing_market"
    | "provider_not_configured"
    | "provider_not_found"
    | "provider_error"
    | "rate_limited"
    | "dead"
    | string;
};

export type TokenFlowBlock = {
  window: WindowKey;
  window_start_ms?: number | null;
  window_end_ms?: number | null;
  mentions: number;
  direct_mentions?: number;
  symbol_mentions?: number;
  weighted_mentions?: number;
  avg_attribution_confidence?: number;
  previous_mentions: number;
  mention_delta: number;
  mention_delta_pct?: number | null;
  z_score?: number | null;
  new_burst_score?: number | null;
  stream_dominance: number;
  baseline_status: "ready" | "insufficient_history" | string;
  baseline_sample_count: number;
};

export type SocialHeatBlock = ScoreBlock & {
  window: WindowKey;
  mentions: number;
  mentions_5m: number;
  mentions_1h: number;
  mentions_4h: number;
  mentions_24h: number;
  weighted_mentions: number;
  previous_mentions: number;
  mention_delta: number;
  mention_delta_pct?: number | null;
  z_score?: number | null;
  new_burst_score?: number | null;
  stream_share: number;
  status: "cold" | "rising" | "burst" | "new_burst" | "insufficient_history" | string;
};

export type DiscussionQualityBlock = ScoreBlock & {
  evidence_specificity: number;
  avg_post_quality: number;
  avg_attribution_confidence: number;
  duplicate_text_share: number;
  informative_post_count: number;
};

export type PropagationBlock = ScoreBlock & {
  independent_authors: number;
  effective_authors: number;
  new_authors: number;
  top_author_share: number;
  duplicate_text_share: number;
  author_entropy: number;
  reproduction_rate?: number | null;
  phase: "seed" | "ignition" | "expansion" | "concentration" | "fade" | string;
  top_authors: Array<{
    handle?: string | null;
    count?: number;
    posts?: number;
    followers?: number | null;
    role?: string | null;
  }>;
};

export type TradeabilityBlock = ScoreBlock & {
  identity_tradeable: boolean;
  market_fresh: boolean;
  market_cap_present: boolean;
  liquidity_present: boolean;
  pool_present: boolean;
  hard_risks?: string[];
};

export type TimingBlock = {
  score: number;
  score_version: string;
  status: "neutral" | "market_pending" | "market_unavailable" | "chase_risk";
  social_signal_start_ms?: number | null;
  price_change_since_social_pct?: number | null;
  price_change_before_social_pct?: number | null;
  market_observation_status?: string | null;
  chase_risk: boolean;
  reasons: string[];
  risks: string[];
  contributions?: ScoreContribution[];
  risk_caps?: RiskCap[];
};

export type OpportunityBlock = ScoreBlock & {
  decision: Decision;
  decision_priority?: number;
  hard_risks?: string[];
  components: {
    heat: number;
    propagation: number;
    timing: number;
  };
};

export type TokenPostsQuery = {
  target_type?: string | null;
  target_id?: string | null;
  window: WindowKey;
  range: TokenPostRange;
};

export type TokenSocialTimelineParams = {
  target_type?: string | null;
  target_id?: string | null;
  window: WindowKey;
};

export type TokenSocialTimelineQuery = TokenSocialTimelineParams & {
  bucket: TimelineBucket;
};

export type TokenFlowItem = {
  identity: TokenIdentityBlock;
  market: TokenMarketBlock;
  flow: TokenFlowBlock;
  social_heat: SocialHeatBlock;
  discussion_quality: DiscussionQualityBlock;
  propagation: PropagationBlock;
  tradeability: TradeabilityBlock;
  timing: TimingBlock;
  opportunity: OpportunityBlock;
  profile?: TokenProfileBlock | null;
  factor_data_health?: TokenFactorSnapshot["data_health"];
  factor_gates?: TokenFactorSnapshot["gates"];
  factor_normalization?: TokenFactorSnapshot["normalization"];
  radar?: TokenRadarRowMeta;
  evidence_total_count: number;
  posts_query: TokenPostsQuery;
  timeline_query: TokenSocialTimelineParams;
};

export type TokenPostItem = {
  event_id: string;
  tweet_id?: string | null;
  author_handle?: string | null;
  text?: string | null;
  url?: string | null;
  received_at_ms?: number | null;
  mention_source?: string | null;
  target_type?: string | null;
  target_id?: string | null;
  symbol?: string | null;
  attribution_status?: string | null;
  attribution_confidence?: number | null;
  attribution_weight?: number | null;
  event_type?: string | null;
  reference?: TokenReference | null;
  price?: TokenMessagePrice | null;
  stage_id?: string | null;
  stage_phase?: string | null;
  author_role?: string | null;
  is_stage_representative?: boolean | number | null;
  price_delta_from_previous_post_pct?: number | null;
  post_quality: ScoreBlock;
};

export type TokenMessagePrice = {
  status: "ready" | "stale" | "pending_observation" | string;
  provider?: string | null;
  pricefeed_id?: string | null;
  price_usd?: number | null;
  price_quote?: number | null;
  quote_symbol?: string | null;
  observed_at_ms?: number | null;
  observation_lag_ms?: number | null;
  observation_id?: string | null;
  observation_kind?: string | null;
};

export type TokenPostsData = {
  query: TokenPostsQuery;
  score_window: { window: WindowKey };
  total_count: number;
  returned_count: number;
  has_more: boolean;
  next_cursor?: string | null;
  items: TokenPostItem[];
};

export type TokenFlowData = {
  window: WindowKey;
  items: TokenFlowItem[];
};

export type TokenTimelineBucket = {
  start_ms: number;
  end_ms: number;
  posts: number;
  authors?: number;
  new_authors: number;
  duplicate_text_share: number;
  price?: TokenMessagePrice | null;
  price_change_from_start_pct?: number | null;
};

export type TokenTimelineAuthor = {
  handle: string;
  first_seen_ms?: number | null;
  latest_seen_ms?: number | null;
  posts: number;
  followers?: number | null;
  role?: "seed" | "early_amplifier" | "amplifier" | "repeater" | string | null;
  quality_score?: number | null;
};

export type TokenTimelinePost = {
  event_id: string;
  tweet_id?: string | null;
  author_handle?: string | null;
  received_at_ms?: number | null;
  bucket_start_ms?: number | null;
  text?: string | null;
  url?: string | null;
  target_type?: string | null;
  target_id?: string | null;
  attribution_status?: string | null;
  event_type?: string | null;
  reference?: TokenReference | null;
  price?: TokenMessagePrice | null;
  attribution_confidence?: number | null;
  attribution_weight?: number | null;
  mention_source?: string | null;
  stage_id?: string | null;
  stage_phase?: string | null;
  author_role?: string | null;
  is_stage_representative?: boolean | number | null;
  price_delta_from_previous_post_pct?: number | null;
  post_quality: ScoreBlock;
};

export type TokenReference = {
  tweet_id?: string | null;
  author_handle?: string | null;
  type?: string | null;
};

export type TokenTimelineCascadeEdge = {
  event_id?: string | null;
  parent_event_id?: string | null;
  parent_tweet_id?: string | null;
  edge_type?: string | null;
  parent_author_handle?: string | null;
  resolved: boolean;
};

export type TokenTimelineCascade = {
  edges: TokenTimelineCascadeEdge[];
  unresolved_parents: TokenTimelineCascadeEdge[];
};

export type TokenTimelineMarketCandles = {
  target_type?: string | null;
  target_id?: string | null;
  chain_id?: string | null;
  address?: string | null;
  symbol?: string | null;
  pricefeed_id?: string | null;
  provider?: string | null;
  native_market_id?: string | null;
  quote_symbol?: string | null;
  feed_type?: string | null;
  price_series_type?: "anchor_line" | "ohlc" | string | null;
  candle_status?:
    | "ready"
    | "empty"
    | "unsupported"
    | "missing_target"
    | "missing_identity"
    | "missing_market_id"
    | "error"
    | string
    | null;
  candle_source?: string | null;
  candle_bar?: string | null;
  candle_error?: string | null;
  candles?: MarketCandle[];
  [key: string]: unknown;
};

export type MarketCandle = {
  time_ms: number;
  open?: number | null;
  high?: number | null;
  low?: number | null;
  close?: number | null;
  volume?: number | null;
  volume_quote?: number | null;
  volume_usd?: number | null;
  confirmed?: boolean | null;
};

export type TokenTimelineStage = {
  stage_id: string;
  phase: string;
  start_ms: number;
  end_ms: number;
  duration_ms: number;
  trigger_reason: string;
  confidence: number;
  people: {
    posts: number;
    authors: number;
    new_authors: number;
    top_author_share: number;
  };
  representative_event_ids: string[];
  price: {
    status: string;
    start_price?: number | null;
    end_price?: number | null;
    delta_pct?: number | null;
    observation_ids: string[];
    max_observation_lag_ms?: number | null;
  };
  risks: string[];
};

export type TokenSocialTimelineData = {
  query: TokenSocialTimelineQuery;
  summary: {
    posts: number;
    authors: number;
    effective_authors: number;
    first_seen_ms?: number | null;
    latest_seen_ms?: number | null;
    phase: string;
    top_author_share: number;
    duplicate_text_share: number;
    peak_posts_per_bucket: number;
    peak_new_authors_per_bucket: number;
    reproduction_rate: number | null;
  };
  market_candles?: TokenTimelineMarketCandles | null;
  stages: TokenTimelineStage[];
  buckets: TokenTimelineBucket[];
  authors: TokenTimelineAuthor[];
  posts: TokenTimelinePost[];
  cascade: TokenTimelineCascade;
  returned_count: number;
  has_more: boolean;
  next_cursor?: string | null;
};

export type FactorPoint = {
  family: string;
  key: string;
  raw_value?: unknown;
  score?: number | null;
  confidence?: number | null;
  data_health?: string | null;
  freshness_ms?: number | null;
  source_refs?: string[];
  risk_flags?: string[];
};

export type TokenFactorFamilyKey = "social_heat" | "social_propagation" | "timing_risk";

export type TokenFactorFamily = {
  raw_score: number;
  score: number;
  weight: number;
  facts: Record<string, unknown>;
  factors: Record<string, FactorPoint>;
  data_health: string;
};

export type TokenFactorSnapshot = {
  schema_version: "token_factor_snapshot_v5_provider_neutral";
  subject: {
    target_type: string | null;
    target_id: string | null;
    symbol: string | null;
    target_market_type: string | null;
    chain: string | null;
    address: string | null;
    pricefeed_id: string | null;
  };
  market: MarketContext;
  gates: {
    eligible_for_high_alert: boolean;
    max_decision: "discard" | "watch" | "high_alert";
    blocked_reasons: string[];
    risk_reasons: string[];
  };
  data_health: {
    identity: string;
    market: string;
    social: string;
    alpha: string;
    [key: string]: unknown;
  };
  families: Record<TokenFactorFamilyKey, TokenFactorFamily>;
  normalization: {
    status: string;
    cohort_status: string;
    cohort: Record<string, unknown>;
    factor_ranks: Record<TokenFactorFamilyKey, number | null>;
    alpha_rank: number | null;
  };
  composite: {
    raw_alpha_score: number;
    rank_score: number;
    recommended_decision: "discard" | "watch" | "high_alert";
    family_scores: Record<TokenFactorFamilyKey, number>;
  };
  provenance: {
    source_event_ids: string[];
    computed_at_ms: number;
  };
};

export type SourceEventDetail = {
  event_id: string;
  timestamp_ms: number;
  source_provider: string;
  channel: string;
  action: "tweet" | "quote" | "repost" | "reply" | string;
  author_handle: string | null;
  author_name: string | null;
  author_followers: number | null;
  text_clean: string | null;
  canonical_url: string | null;
};

export type SourceEventsByIdsData = {
  events: SourceEventDetail[];
  not_found: string[];
};

export type EnrichmentJobsData = {
  items: Array<Record<string, unknown>>;
  counts: Record<string, number>;
};

export type WorkerStatusData = {
  enabled: boolean;
  running: boolean;
  effective_status: string;
  unavailable_reason: string | null;
  last_started_at_ms: number | null;
  last_finished_at_ms: number | null;
  last_result: Record<string, unknown> | null;
  last_error: string | null;
  iteration_duration_p99_ms: number | null;
};

export type StatusData = {
  ok: boolean;
  reasons: string[];
  store: string;
  snapshot_gate: Record<string, unknown>;
  db: Record<string, unknown>;
  provider_states: Record<string, unknown>;
  workers: Record<string, WorkerStatusData> & {
    collector: WorkerStatusData;
  };
};
