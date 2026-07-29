export type JsonObject = Record<string, unknown>;

export type MacroModuleId =
  | "rates_fed"
  | "economy_inflation"
  | "liquidity_funding"
  | "credit"
  | "volatility"
  | "cross_asset";

export type MacroCoverageState = "complete" | "partial";
export type MacroCurrentHealthState = "current" | "degraded" | "unavailable";
export type MacroHistoryDepthState = "complete" | "partial" | "insufficient" | "not_required";
export type MacroThesisState =
  | "published"
  | "pending"
  | "running"
  | "retryable"
  | "failed"
  | "config_error"
  | "not_published"
  | "missing";

export type MacroCoverageCapability = {
  capability_id: string;
  label: string;
  requirement: "required" | "supporting";
  state: "available" | "missing";
  dataset_ids: string[];
  reason: string | null;
};

export type MacroCoverage = {
  state: MacroCoverageState;
  expected_capabilities: number;
  available_capabilities: number;
  capabilities: MacroCoverageCapability[];
};

export type MacroCurrentHealthGroup = {
  group_id: string;
  label: string;
  current_health: MacroCurrentHealthState | "mixed";
  market_state: "open" | "closed" | "maintenance" | "unknown" | "not_applicable" | "mixed";
  source_state: "healthy" | "degraded" | "failed" | "not_applicable" | "mixed";
  current_datasets: number;
  tracked_datasets: number;
};

export type MacroCurrentHealth = {
  state: MacroCurrentHealthState;
  current_datasets: number;
  tracked_datasets: number;
  as_of_ms: number;
  groups: MacroCurrentHealthGroup[];
};

export type MacroHistoryDepth = {
  state: MacroHistoryDepthState;
  complete_datasets: number;
  tracked_datasets: number;
};

export type MacroChange = {
  dataset_id: string;
  concept_id: string;
  source_role: string;
  label: string;
  as_of: string | null;
  value: number;
  unit: string;
  cadence: string;
  metrics: Record<string, number | null>;
  metric_unit: string;
  primary_change: number | null;
  importance_rank: number;
  importance_factors: {
    standardized_magnitude: number;
    surprise_magnitude: number;
    revision_magnitude: number;
    decision_relevance: number;
    trust_tier: "official" | "exchange" | "untrusted_proxy";
    fact_clock_ms: number;
  };
  importance_explanation: string;
  source_url: string;
};

export type MacroDatasetState = {
  dataset_id: string;
  concept_id: string;
  source_role: string;
  label: string;
  current_health: MacroCurrentHealthState;
  history_depth: MacroHistoryDepthState;
  market_state: "open" | "closed" | "maintenance" | "unknown" | "not_applicable";
  source_state: "healthy" | "degraded" | "failed" | "not_applicable";
  current_reason: string;
  history_reason: string;
  critical: boolean;
  trust_tier: "official" | "exchange" | "untrusted_proxy";
  source_url: string;
  latest_reference: string | null;
  latest_received_at_ms: number | null;
  last_market_at_ms: number | null;
  next_open_ms: number | null;
  health_group: string;
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

type MacroModuleBase = {
  module_id: MacroModuleId;
  label: string;
  status: {
    coverage: MacroCoverage;
    current_health: MacroCurrentHealth;
    history_depth: MacroHistoryDepth;
  };
  latest_fact_at_ms: number;
  summary: {
    headline: string;
    interpretation: string;
    top_changes: MacroChange[];
  };
  contradictions: string[];
  falsifiers: string[];
  next_checkpoints: Array<{
    dataset_id: string;
    label: string;
    current_health: string;
    history_depth: string;
    next_check: string;
  }>;
  evidence: {
    dataset_states: MacroDatasetState[];
    latest_facts: MacroEvidenceFact[];
    reconciliation_receipts: JsonObject[];
  };
};

export type MacroRatesFedReadData = MacroModuleBase & {
  schema_version: "macro_rates_fed_v4";
  module_id: "rates_fed";
  curve: JsonObject;
  policy_pricing: JsonObject;
  fed: JsonObject;
  positioning: JsonObject[];
};

export type MacroEconomyInflationReadData = MacroModuleBase & {
  schema_version: "macro_economy_inflation_v4";
  module_id: "economy_inflation";
  inflation: JsonObject;
  labor: JsonObject;
  growth: JsonObject;
};

export type MacroLiquidityFundingReadData = MacroModuleBase & {
  schema_version: "macro_liquidity_funding_v4";
  module_id: "liquidity_funding";
  balance_sheet: JsonObject;
  funding: JsonObject;
};

export type MacroCreditReadData = MacroModuleBase & {
  schema_version: "macro_credit_v5";
  module_id: "credit";
  cycle_dimensions: JsonObject[];
  spread_ladder: JsonObject;
  funding_costs: JsonObject;
  bank_lending: JsonObject;
  loan_quality: JsonObject;
  confirmations: JsonObject;
};

export type MacroVolatilityReadData = MacroModuleBase & {
  schema_version: "macro_volatility_v4";
  module_id: "volatility";
  term_structure: JsonObject;
  cross_asset_implied: JsonObject;
};

export type MacroCrossAssetReadData = MacroModuleBase & {
  schema_version: "macro_cross_asset_v5";
  module_id: "cross_asset";
  assets: JsonObject;
  correlations: JsonObject[];
  futures: JsonObject;
};

export type MacroTypedModuleReadData =
  | MacroRatesFedReadData
  | MacroEconomyInflationReadData
  | MacroLiquidityFundingReadData
  | MacroCreditReadData
  | MacroVolatilityReadData
  | MacroCrossAssetReadData;

export type MacroCondition = {
  condition_id: string;
  module_id: MacroModuleId;
  dataset_id: string;
  metric_name: string;
  operator: "gt" | "gte" | "lt" | "lte" | "abs_gte";
  threshold: number;
  effect: "confirming" | "weakening" | "invalidation_triggered";
  rationale: string;
};

export type MacroThesisClaim = {
  claim_id: string;
  statement: string;
  causal_edges: Array<{
    source: string;
    mechanism: string;
    target: string;
    evidence_refs: string[];
    conflicting_evidence_refs: string[];
  }>;
  supporting_evidence_refs: string[];
  conflicting_evidence_refs: string[];
  conditions: MacroCondition[];
};

export type MacroModuleRole = {
  module_id: MacroModuleId;
  role: "driver" | "confirming" | "contradicting" | "uncertain";
  analysis: string;
  claim_ids: string[];
  supporting_evidence_refs: string[];
  conflicting_evidence_refs: string[];
};

export type MacroMomentum = {
  symbol: string;
  momentum_1w: "up" | "down" | "flat" | "insufficient";
  momentum_1m: "up" | "down" | "flat" | "insufficient";
  return_1w_pct: number | null;
  return_1m_pct: number | null;
  source_dataset_id: string | null;
  as_of: string | null;
};

export type MacroHorizonOutlook = {
  horizon: "1w" | "1m";
  direction: "bullish" | "bearish" | "neutral" | "no_call";
  causal_channel: string;
  supporting_evidence_refs: string[];
  conflicting_evidence_refs: string[];
  confirmation_triggers: MacroCondition[];
  falsifiers: MacroCondition[];
  checkpoints: MacroCondition[];
  confidence: "low" | "medium" | "high";
};

export type MacroAssetView = {
  symbol: string;
  momentum: MacroMomentum;
  outlook_1w: MacroHorizonOutlook;
  outlook_1m: MacroHorizonOutlook;
};

export type MacroThesisV1 = {
  schema_version: "macro_thesis_v1";
  publication_id: string;
  session_date: string;
  cutoff_ms: number;
  evidence_pack_id: string;
  evidence_pack_hash: string;
  prior_publication_id: string | null;
  mainline: {
    stance: "call" | "no_call";
    title: string;
    thesis: string;
    stage: "emerging" | "developing" | "mature" | "reversing" | "uncertain";
    confidence: "low" | "medium" | "high";
    horizon: "1w" | "1m" | "1w_to_1m";
    claims: MacroThesisClaim[];
    supporting_evidence_refs: string[];
    conflicting_evidence_refs: string[];
    falsifiers: MacroCondition[];
    checkpoints: MacroCondition[];
  };
  alternative_explanation: {
    title: string;
    thesis: string;
    causal_edges: MacroThesisClaim["causal_edges"];
    supporting_evidence_refs: string[];
    conflicting_evidence_refs: string[];
    trigger_conditions: MacroCondition[];
  } | null;
  core_tensions: Array<{
    tension_id: string;
    statement: string;
    side_a: { label: string; statement: string; evidence_refs: string[] };
    side_b: { label: string; statement: string; evidence_refs: string[] };
    leading_side: "side_a" | "side_b" | "balanced" | "uncertain";
    lagging_signal: string;
    unresolved_reason: string;
    resolution_triggers: MacroCondition[];
  }>;
  changes_from_prior: Array<{
    change_id: string;
    status: "new" | "strengthened" | "weakened" | "reversed" | "unchanged";
    statement: string;
    evidence_refs: string[];
  }>;
  module_assessments: MacroModuleRole[];
  assets: MacroAssetView[];
  gaps: Array<{
    gap_id: string;
    module_id: MacroModuleId;
    dataset_id: string;
    axis: "coverage" | "current_health" | "history_depth";
    state: string;
    reason: string;
    affected_claim_ids: string[];
  }>;
  citations: Array<{
    evidence_ref: string;
    module_id: MacroModuleId;
    dataset_id: string | null;
    source_role: string | null;
    label: string;
    reference: string | null;
    published_at_ms: number | null;
    received_at_ms: number | null;
    source_url: string | null;
  }>;
  narrative_sections: Array<{
    section_id: string;
    title: string;
    markdown: string;
    evidence_refs: string[];
  }>;
  review: {
    draft_hash: string;
    disposition: "pass";
    findings: string[];
    required_changes: string[];
    invocation_id: string;
    model_name: string;
    prompt_version: string;
  };
  provenance: {
    draft_hash: string;
    research_invocation_id: string;
    research_model: string;
    reviewer_model: string;
    research_prompt_version: string;
    reviewer_prompt_version: string;
    workflow_version: "macro_thesis_workflow_v1";
  };
  published_at_ms: number;
};

export type MacroLiveDeltaV1 = {
  schema_version: "macro_live_delta_v1";
  live_delta_id: string;
  publication_id: string;
  evaluated_at_ms: number;
  module_fact_cutoff_ms: number;
  status: "confirming" | "weakening" | "invalidation_triggered" | "unrelated" | "insufficient";
  matched_claim_ids: string[];
  matched_falsifier_ids: string[];
  matched_checkpoint_ids: string[];
  items: Array<{
    binding_type: "claim" | "falsifier" | "checkpoint";
    binding_id: string;
    condition_id: string;
    status: "confirming" | "weakening" | "invalidation_triggered" | "unrelated" | "insufficient";
    dataset_id: string;
    metric_name: string;
    observed_value: number | null;
    operator: MacroCondition["operator"];
    threshold: number;
    reason_code: string;
  }>;
  reason_codes: string[];
  input_hash: string;
};

export type MacroOutcomeReplayV1 = {
  schema_version: "macro_outcome_replay_v1";
  replay_id: string;
  publication_id: string;
  evaluated_at_ms: number;
  horizons: Array<{
    horizon: "1d" | "1w" | "1m";
    expires_at_ms: number;
    status: "pending" | "evaluated" | "insufficient";
    benchmark_symbol: string;
    realized_return_pct: number | null;
    direction_correct: boolean | null;
    reason_code: string;
    asset_results: Array<{
      symbol: string;
      horizon: "1w" | "1m";
      expires_at_ms: number;
      status: "pending" | "evaluated" | "insufficient";
      published_direction: "bullish" | "bearish" | "neutral" | "no_call";
      realized_return_pct: number | null;
      direction_correct: boolean | null;
      reason_code: string;
    }>;
  }>;
  input_hash: string;
};

export type MacroModuleSummary = {
  module_id: MacroModuleId;
  label: string;
  role: MacroModuleRole["role"] | null;
  coverage_state: MacroCoverageState;
  current_health_state: MacroCurrentHealthState;
  history_depth_state: MacroHistoryDepthState;
  latest_fact_at_ms: number;
  summary: MacroModuleBase["summary"];
  coverage_gap_count: number;
  current_health_gap_count: number;
  history_gap_count: number;
  href: string;
};

export type MacroOverviewReadData = {
  schema_version: "macro_overview_v5";
  read_at_ms: number;
  transport: {
    state: "current" | "stale";
    last_successful_read_at_ms: number;
    reason: string | null;
  };
  session_date: string;
  cutoff_ms: number;
  latest_fact_at_ms: number;
  thesis_state: MacroThesisState;
  thesis: MacroThesisV1 | null;
  live_delta: MacroLiveDeltaV1 | null;
  outcome_replay: MacroOutcomeReplayV1 | null;
  modules: MacroModuleSummary[];
  data_quality: {
    coverage_state: MacroCoverageState;
    current_health_state: MacroCurrentHealthState;
    history_depth_state: MacroHistoryDepthState;
    coverage_gap_count: number;
    current_health_gap_count: number;
    history_gap_count: number;
  };
};

export type MacroThesisRunData = {
  session_date: string;
  status: string;
  evidence_pack_id: string;
  attempt_count: number;
  max_attempts: number;
  error_code: string | null;
  error_message: string | null;
  updated_at_ms: number;
};

export type MacroThesisDetailReadData = {
  state: "current" | "historical" | "generating" | "not_published" | "failed" | "missing";
  requested_session_date: string;
  current_session_date: string;
  thesis: MacroThesisV1 | null;
  live_delta: MacroLiveDeltaV1 | null;
  outcome_replay: MacroOutcomeReplayV1 | null;
  run: MacroThesisRunData | null;
  history: Array<{
    publication_id: string;
    session_date: string;
    cutoff_ms: number;
    published_at_ms: number;
    title: string;
    stance: "call" | "no_call";
    confidence: "low" | "medium" | "high";
    horizon: "1w" | "1m" | "1w_to_1m";
  }>;
};
