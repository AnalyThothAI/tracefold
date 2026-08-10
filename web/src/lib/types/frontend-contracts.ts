export type ApiResponse<T> = {
  ok: boolean;
  data: T;
  error?: string | null;
};

export type WindowKey = "5m" | "1h" | "4h" | "24h";
export type TimelineBucket = "30s" | "5m" | "15m" | "1h";
export type TokenPostRange = "current_window" | "since_ignition" | "all_history";

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

// Token Case exposes post-quality evidence independently of retired Radar
// admission/decision blocks.
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

export type EnrichmentJobsData = {
  items: Array<Record<string, unknown>>;
  counts: Record<string, number>;
};
