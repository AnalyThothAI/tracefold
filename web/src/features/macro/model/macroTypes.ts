export type MacroModuleId =
  | "rates_fed"
  | "economy_inflation"
  | "liquidity_funding"
  | "credit"
  | "volatility"
  | "cross_asset";

export type MacroReadiness = "ready" | "degraded" | "blocked";
export type JsonObject = Record<string, unknown>;

export type MacroChange = {
  dataset_id: string;
  label: string;
  as_of: string;
  value: number;
  unit: string;
  short_window: string;
  short_change: number | null;
  medium_window: string;
  medium_change: number | null;
  magnitude: number;
  source_url: string;
};

export type MacroChartPoint = {
  dataset_id: string;
  label?: string;
  x: string;
  y: number;
  unit: string;
};

export type MacroChart = {
  chart_id: string;
  title: string;
  series: string[];
  points: MacroChartPoint[];
};

export type MacroDatasetState = {
  dataset_id: string;
  label: string;
  state: string;
  reason: string;
  critical: boolean;
  trust_tier: "official" | "exchange" | "untrusted_proxy";
  source_url: string;
  latest_reference: string | null;
  latest_received_at_ms: number | null;
};

export type MacroModuleReadData = {
  schema_version: "macro_module_v1";
  module_id: MacroModuleId;
  label: string;
  readiness: MacroReadiness;
  judgment_cutoff_ms: number | null;
  latest_fact_at_ms: number;
  current_state: {
    headline: string;
    dominant_change: MacroChange | null;
    feature_count: number;
    interpretation: string;
  };
  top_changes: MacroChange[];
  features: JsonObject[];
  charts: MacroChart[];
  contradictions: string[];
  falsifiers: string[];
  next_checkpoints: JsonObject[];
  gaps: JsonObject[];
  dataset_states: MacroDatasetState[];
  raw_evidence: JsonObject[];
};

export type MacroModuleSummary = {
  module_id: MacroModuleId;
  label: string;
  readiness: MacroReadiness | "missing";
  latest_fact_at_ms: number;
  current_state: JsonObject | null;
  top_changes: MacroChange[];
  gap_count: number;
  href: string;
};

export type MacroDimension = {
  state: string;
  driver: string;
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
  schema_version: "macro_daily_judgment_v1";
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
  schema_version: "macro_overview_v1";
  read_at_ms: number;
  judgment_cutoff_ms: number | null;
  latest_fact_at_ms: number;
  overall_readiness: MacroReadiness;
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
