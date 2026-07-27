export type MacroModuleId =
  | "rates_fed"
  | "economy_inflation"
  | "liquidity_funding"
  | "credit"
  | "volatility"
  | "cross_asset";

export type JsonObject = Record<string, unknown>;
export type MacroCoverageState = "complete" | "partial" | "licensed_unavailable";
export type MacroDataHealthState =
  | "current"
  | "delayed"
  | "stale"
  | "invalid"
  | "backfilling"
  | "unavailable";
export type MacroJudgmentState = "current" | "missing" | "blocked";

export type MacroCoverageCapability = {
  capability_id: string;
  label: string;
  requirement: "required" | "supporting" | "licensed_unavailable";
  state: "available" | "missing" | "licensed_unavailable";
  dataset_ids: string[];
  reason: string | null;
};

export type MacroModuleStatus = {
  coverage: {
    state: MacroCoverageState;
    expected_capabilities: number;
    available_capabilities: number;
    capabilities: MacroCoverageCapability[];
  };
  data_health: {
    state: MacroDataHealthState;
    current_datasets: number;
    tracked_datasets: number;
    as_of_ms: number;
  };
  judgment: {
    state: MacroJudgmentState;
    cutoff_ms: number | null;
  };
};

export type MacroChange = {
  dataset_id: string;
  label: string;
  as_of: string | null;
  value: number;
  unit: string;
  change_1w: number | null;
  change_1m: number | null;
  magnitude: number;
  source_url: string;
};

export type MacroDatasetState = {
  dataset_id: string;
  label: string;
  state: MacroDataHealthState;
  reason: string;
  critical: boolean;
  trust_tier: "official" | "exchange" | "untrusted_proxy";
  source_url: string;
  latest_reference: string | null;
  latest_received_at_ms: number | null;
};

export type MacroEvidenceFact = {
  dataset_id: string;
  series_id?: string | null;
  contract_code?: string | null;
  fact_ref: string | null;
  reference: string | null;
  value: number | string | null;
  unit: string;
  published_at_ms: number | null;
  received_at_ms: number;
  source_url: string;
};

export type MacroIndicator = {
  dataset_id: string;
  label: string;
  latest_value: number;
  unit: string;
  as_of: string;
  change_1w: number | null;
  change_1m: number | null;
  sample_count: number;
  history_start: string;
  history_end: string;
  source_url: string;
  percentile?: number | null;
  history: Array<{ date: string; value: number }>;
};

export type MacroAssetRow = {
  dataset_id: string;
  symbol: string;
  label: string;
  instrument_type: string;
  asset_class: string;
  latest_value: number;
  unit: string;
  as_of: string | null;
  change_1d_pct: number | null;
  change_1w_pct: number | null;
  change_1m_pct: number | null;
  trust_tier: "official" | "exchange" | "untrusted_proxy";
  source_url: string;
};

export type MacroModuleBase = {
  module_id: MacroModuleId;
  label: string;
  status: MacroModuleStatus;
  latest_fact_at_ms: number;
  summary: {
    headline: string;
    interpretation: string;
    top_changes: MacroChange[];
  };
  contradictions: string[];
  falsifiers: string[];
  next_checkpoints: JsonObject[];
  evidence: {
    dataset_states: MacroDatasetState[];
    latest_facts: MacroEvidenceFact[];
  };
};

export type MacroRatesFedReadData = MacroModuleBase & {
  schema_version: "macro_rates_fed_v2";
  module_id: "rates_fed";
  curve: {
    nominal_snapshots: MacroCurveSnapshot[];
    real_snapshots: MacroCurveSnapshot[];
    breakeven_snapshots: Array<{
      window: string;
      as_of: string;
      points: Array<{ tenor: string; years: number; breakeven_pct: number }>;
    }>;
    spreads: Record<string, Array<{ date: string; value_bp: number }>>;
    classification: {
      state: string;
      label: string;
      formula_version: string;
      inputs: Record<string, string | number | null>;
    };
  };
  policy_pricing: {
    rates: MacroIndicator[];
    cme_policy_probabilities: { state: "licensed_unavailable"; reason: string };
  };
  fed: {
    institutional_stance: {
      state: string;
      direction: string;
      change_from_prior: string;
      reason: string;
    };
    officials_distribution: {
      state: string;
      window_days: number;
      as_of: string | null;
      hawkish: number;
      neutral: number;
      dovish: number;
      mixed: number;
      not_policy_signal: number;
      uncertain: number;
      analyzed_events: number;
    };
    timeline: MacroFedTimelineEvent[];
    roster: { state: string; reason: string; officials: JsonObject[] };
  };
  positioning: JsonObject[];
};

export type MacroCurveSnapshot = {
  window: "current" | "1w" | "1m" | "3m";
  as_of: string;
  points: Array<{ tenor: string; years: number; yield_pct: number }>;
};

export type MacroFedTimelineEvent = {
  document_id: string;
  document_type: string;
  title: string;
  effective_date: string;
  published_at_ms: number;
  source_url: string;
  speaker_name: string | null;
  official_id: string | null;
  role_title: string | null;
  fomc_voter: boolean | null;
  analysis: {
    state: string;
    policy_relevance: string;
    stance: string;
    confidence: number | null;
    change_from_prior: string | null;
    evidence: JsonObject[];
    analysis_id: string | null;
    model_name: string | null;
    prompt_version: string | null;
    reviewer_disposition: string | null;
  };
};

export type MacroEconomyInflationReadData = MacroModuleBase & {
  schema_version: "macro_economy_inflation_v2";
  module_id: "economy_inflation";
  inflation: { indicators: MacroIndicator[]; official_releases: JsonObject[] };
  labor: { indicators: MacroIndicator[]; official_releases: JsonObject[] };
  growth: { indicators: MacroIndicator[] };
};

export type MacroLiquidityFundingReadData = MacroModuleBase & {
  schema_version: "macro_liquidity_funding_v2";
  module_id: "liquidity_funding";
  balance_sheet: { indicators: MacroIndicator[] };
  funding: { indicators: MacroIndicator[] };
};

export type MacroCreditReadData = MacroModuleBase & {
  schema_version: "macro_credit_v2";
  module_id: "credit";
  cycle_dimensions: Array<{
    dimension_id:
      | "spread_level_velocity"
      | "funding_cost"
      | "credit_supply"
      | "credit_quality"
      | "market_liquidity";
    label: string;
    state: string;
    driver: string;
    evidence_dataset_ids: string[];
    conflicts: string[];
  }>;
  spread_ladder: {
    rows: MacroIndicator[];
    tail_gap: number | null;
    tail_gap_unit: "basis_points";
  };
  funding_costs: {
    corporate_yields: MacroIndicator[];
    reference_rates: MacroIndicator[];
    comparisons: Array<{
      label: string;
      corporate_dataset_id: string;
      reference_dataset_id: string;
      as_of: string;
      value_bp: number;
      formula_version: string;
      input_fact_ids: string[];
    }>;
  };
  bank_lending: { indicators: MacroIndicator[] };
  loan_quality: { indicators: MacroIndicator[] };
  confirmations: {
    etfs: MacroAssetRow[];
    positions: JsonObject[];
    trace_nav: { state: "licensed_unavailable"; reason: string };
  };
};

export type MacroVolatilityReadData = MacroModuleBase & {
  schema_version: "macro_volatility_v2";
  module_id: "volatility";
  term_structure: {
    spot_and_three_month: MacroIndicator[];
    spread_history: Array<{ date: string; value: number }>;
  };
  cross_asset_implied: { indicators: MacroIndicator[] };
};

export type MacroCrossAssetReadData = MacroModuleBase & {
  schema_version: "macro_cross_asset_v2";
  module_id: "cross_asset";
  assets: {
    benchmarks: JsonObject[];
    proxies: MacroAssetRow[];
    normalized: Array<{ symbol: string; date: string; normalized_value: number }>;
  };
  correlations: Array<{
    left: string;
    right: string;
    correlation: number | null;
    sample_count: number;
    window: string;
  }>;
  futures: { vix_settlements: JsonObject[]; positions: JsonObject[] };
};

export type MacroTypedModuleReadData =
  | MacroRatesFedReadData
  | MacroEconomyInflationReadData
  | MacroLiquidityFundingReadData
  | MacroCreditReadData
  | MacroVolatilityReadData
  | MacroCrossAssetReadData;

export type MacroModuleSummary = {
  module_id: MacroModuleId;
  label: string;
  coverage_state: MacroCoverageState | "missing";
  data_health_state: MacroDataHealthState | "missing";
  judgment_state: MacroJudgmentState;
  latest_fact_at_ms: number;
  summary: MacroModuleBase["summary"] | null;
  top_changes: MacroChange[];
  coverage_gap_count: number;
  health_gap_count: number;
  href: string;
};

export type MacroDimension = {
  state: string;
  driver: string;
  subdimensions?: JsonObject[];
  conflicts?: string[];
};

export type MacroAssetDirection = {
  "1w": "up" | "down" | "range" | "no_call";
  "1m": "up" | "down" | "range" | "no_call";
  drivers: string[];
  conflicts: string[];
  invalidation: string;
  confidence: "low" | "medium" | "high";
  dataset_id: string;
};

export type MacroDailyJudgment = {
  schema_version: "macro_daily_judgment_v2";
  session_date: string;
  judgment_cutoff_ms: number;
  latest_fact_at_ms: number;
  overall_state: string;
  dominant_pressures: JsonObject[];
  top_3_changes: JsonObject[];
  dimensions: Record<string, MacroDimension>;
  module_judgments: JsonObject[];
  asset_directions: Record<string, MacroAssetDirection>;
  contradictions: JsonObject[];
  falsifiers: JsonObject[];
  next_checkpoints: JsonObject[];
  gaps: JsonObject[];
  citations: JsonObject[];
};

export type MacroOverviewReadData = {
  schema_version: "macro_overview_v2";
  read_at_ms: number;
  judgment_cutoff_ms: number | null;
  latest_fact_at_ms: number;
  coverage_state: MacroCoverageState;
  data_health_state: MacroDataHealthState;
  judgment_state: MacroJudgmentState;
  daily_judgment: MacroDailyJudgment | null;
  modules: MacroModuleSummary[];
  changes_since_judgment: JsonObject[];
  research: {
    state: "current" | "generating" | "failed" | "missing";
    session_date: string | null;
    evidence_pack_id: string | null;
    market_cutoff_ms: number | null;
    title: string | null;
    executive_summary: string | null;
    reviewer_disposition: "pass" | "revise" | "block" | null;
    href: "/macro/research";
  };
};

export type MacroResearchSectionData = {
  section_id: string;
  title: string;
  body_markdown: string;
  citation_ids: string[];
};

export type MacroResearchEvidenceGapData = {
  gap_id: string;
  summary: string;
  details: string | null;
  citation_ids: string[];
};

export type MacroResearchCitationData = {
  citation_id: string;
  source_type: string;
  source_ref: string;
  source_label: string;
  observed_at: string | null;
  published_at_ms: number | null;
  available_at_ms: number | null;
  source_url: string | null;
  lineage: Record<string, unknown>;
};

export type MacroResearchPublicationData = {
  schema_version: string;
  session_date: string;
  market_cutoff_ms: number;
  evidence_pack_id: string;
  title: string;
  executive_summary: string;
  sections: MacroResearchSectionData[];
  evidence_gaps: MacroResearchEvidenceGapData[];
  citations: MacroResearchCitationData[];
  reviewer_disposition: "pass" | "revise" | "block";
  reviewer_notes: string[];
  audit: Record<string, unknown>;
  published_at_ms: number | null;
};

export type MacroResearchRunData = {
  session_date: string;
  evidence_pack_id: string;
  status: string;
  attempt_count: number;
  max_attempts: number;
  last_error: string | null;
  updated_at_ms: number;
};

export type MacroResearchReadData = {
  state: "current" | "historical" | "generating" | "failed" | "missing";
  requested_session_date: string;
  current_session_date: string;
  publication: MacroResearchPublicationData | null;
  run: MacroResearchRunData | null;
};
