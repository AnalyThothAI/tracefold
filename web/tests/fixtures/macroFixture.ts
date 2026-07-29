import type {
  MacroLiveDeltaV2,
  MacroModuleId,
  MacroOutcomeReplayV2,
  MacroOverviewReadData,
  MacroThesisDetailReadData,
  MacroThesisV2,
  MacroTypedModuleReadData,
} from "@features/macro";
import type { components } from "@lib/types/openapi";

type Schemas = components["schemas"];

const SESSION_DATE = "2026-07-28";
const CUTOFF_MS = Date.parse("2026-07-28T12:50:00Z");
const PUBLISHED_AT_MS = CUTOFF_MS + 90_000;
const READ_AT_MS = PUBLISHED_AT_MS + 60_000;
const HASH_A = `sha256:${"a".repeat(64)}`;
const HASH_B = `sha256:${"b".repeat(64)}`;
const HASH_C = `sha256:${"c".repeat(64)}`;
const ASSETS = [
  "SPY",
  "QQQ",
  "IWM",
  "TLT",
  "IEF",
  "LQD",
  "HYG",
  "UUP",
  "GLD",
  "USO",
  "BTC",
  "VIX",
] as const;
const MODULES: readonly MacroModuleId[] = [
  "rates_fed",
  "economy_inflation",
  "liquidity_funding",
  "credit",
  "volatility",
  "cross_asset",
];

export function macroThesisFixture(overrides: Partial<MacroThesisV2> = {}): MacroThesisV2 {
  const thesis: MacroThesisV2 = {
    schema_version: "macro_thesis_v2",
    publication_id: "macro-publication-2026-07-28",
    session_date: SESSION_DATE,
    cutoff_ms: CUTOFF_MS,
    evidence_pack_id: "macro-pack-2026-07-28",
    evidence_pack_hash: HASH_A,
    research_input_id: "macro-input-2026-07-28",
    research_input_hash: HASH_B,
    draft_hash: HASH_C,
    prior_publication_id: null,
    mainline: {
      stance: "call",
      title: "真实利率回落正在缓和风险资产的贴现压力",
      thesis: "曲线与风险资产共同显示贴现压力缓和，但信用尾部仍需要确认。",
      stage: "developing",
      horizon: "1w_to_1m",
      confidence: "medium",
      causal_edges: [
        {
          edge_id: "edge-rates-risk",
          source: "10Y 实际利率",
          mechanism: "贴现率下行",
          target: "久期与风险资产估值",
          evidence_refs: ["rates:real10y"],
          conflicting_evidence_refs: [],
        },
      ],
      supporting_evidence_refs: ["rates:real10y"],
      conflicting_evidence_refs: ["credit:tail-gap"],
      no_call_reason: null,
    },
    alternative: null,
    tensions: [
      {
        tension_id: "tension-credit",
        statement: "信用尾部尚未确认宽松传导",
        side_a: {
          statement: "实际利率回落",
          evidence_refs: ["rates:real10y"],
        },
        side_b: {
          statement: "CCC-BB 利差仍高",
          evidence_refs: ["credit:tail-gap"],
        },
        leading_side: "side_a",
        unresolved_reason: "需要观察下一批信用数据。",
      },
    ],
    material_changes: [
      {
        change_id: "change-real10y",
        status: "strengthened",
        statement: "10Y 实际利率一周变化转负。",
        evidence_refs: ["rates:real10y"],
      },
    ],
    module_assessments: [
      {
        module_id: "rates_fed",
        role: "driver",
        analysis: "实际利率是本次主线的主要驱动。",
        evidence_refs: ["rates:real10y"],
      },
      {
        module_id: "credit",
        role: "contradicting",
        analysis: "信用尾部仍构成反证。",
        evidence_refs: ["credit:tail-gap"],
      },
    ],
    assets: ASSETS.map((symbol, index) => ({
      symbol,
      display_order: index,
      momentum_1w: symbol === "SPY" ? "up" : "insufficient",
      momentum_1m: symbol === "SPY" ? "up" : "insufficient",
      return_1w_pct: symbol === "SPY" ? 1.2 : null,
      return_1m_pct: symbol === "SPY" ? 2.8 : null,
      source_dataset_id: symbol === "SPY" ? "nasdaq.spy.daily" : null,
      as_of: symbol === "SPY" ? SESSION_DATE : null,
    })),
    asset_outlooks: [
      {
        outlook_id: "outlook-spy-1w",
        symbol: "SPY",
        horizon: "1w",
        outlook_context: "mainline",
        direction: "bullish",
        causal_transmission: "实际利率回落缓和权益贴现压力。",
        supporting_evidence_refs: ["rates:real10y"],
        conflicting_evidence_refs: ["credit:tail-gap"],
        confidence: "medium",
      },
    ],
    citations: [
      {
        evidence_ref: "rates:real10y",
        module_id: "rates_fed",
        dataset_id: "fred.dfii10",
        source_id: "FRED",
        source_role: "canonical",
        label: "10Y 实际利率",
        value: 1.82,
        unit: "percent",
        as_of: SESSION_DATE,
        authoritative_at_ms: CUTOFF_MS - 60_000,
        source_url: "https://fred.stlouisfed.org/series/DFII10",
      },
      {
        evidence_ref: "credit:tail-gap",
        module_id: "credit",
        dataset_id: "fred.ccc_minus_bb",
        source_id: "FRED",
        source_role: "canonical",
        label: "CCC-BB 利差",
        value: 6.1,
        unit: "percent",
        as_of: SESSION_DATE,
        authoritative_at_ms: CUTOFF_MS - 60_000,
        source_url: "https://fred.stlouisfed.org/",
      },
    ],
    conditions: [
      {
        condition_id: "rates.real10y.tail:fred.dfii10:leq20",
        candidate_type: "metric_condition",
        candidate_id: "rates.real10y.tail:fred.dfii10:leq20",
        kind: "confirmation",
        scope_kind: "mainline",
        scope_id: "mainline",
        symbol: null,
        horizon: null,
        rationale: "实际利率保持在五年分位数下尾，确认贴现压力缓和。",
        evidence_refs: ["rates:real10y"],
        module_id: "rates_fed",
        dataset_id: "fred.dfii10",
        metric: "percentile_5y",
        unit: "percentile",
        operator: "lte",
        threshold: 20,
        frozen_value: 18,
        as_of: SESSION_DATE,
        event_id: null,
        scheduled_at_ms: null,
      },
    ],
    gaps: [],
    catalysts: [],
    provenance: {
      research_input_id: "macro-input-2026-07-28",
      research_input_hash: HASH_B,
      draft_hash: HASH_C,
      candidate_hash: HASH_A,
      attempt_id: "attempt-1",
      provider_response_id: "response-1",
      provider_name: "openai-compatible",
      research_model: "test-model",
      profile_version: "macro_thesis_thin_v1",
      prompt_version: "macro_thesis_sop_v2",
      workflow_version: "macro_thesis_workflow_v2",
    },
    published_at_ms: PUBLISHED_AT_MS,
  };
  return { ...thesis, ...overrides };
}

export function macroOverviewFixture(
  overrides: Partial<MacroOverviewReadData> = {},
): MacroOverviewReadData {
  const thesis = macroThesisFixture();
  const overview: MacroOverviewReadData = {
    schema_version: "macro_overview_v8",
    read_at_ms: READ_AT_MS,
    transport: {
      state: "current",
      last_successful_read_at_ms: READ_AT_MS,
      reason: null,
    },
    session_date: SESSION_DATE,
    cutoff_ms: CUTOFF_MS,
    latest_fact_at_ms: CUTOFF_MS - 30_000,
    thesis_state: "published",
    thesis_reason: null,
    thesis,
    run: runFixture("published"),
    live_delta: liveDeltaFixture(thesis.publication_id),
    outcome_replay: outcomeReplayFixture(thesis.publication_id),
    recovery: recoveryFixture(),
    modules: MODULES.map(moduleSummaryFixture),
    data_quality: {
      coverage_state: "complete",
      current_health_state: "current",
      history_depth_state: "complete",
      coverage_gap_count: 0,
      current_health_gap_count: 0,
      history_gap_count: 0,
    },
  };
  return { ...overview, ...overrides };
}

export function macroResearchFixture(
  state: MacroThesisDetailReadData["state"] = "published",
): MacroThesisDetailReadData {
  const thesis = state === "published" ? macroThesisFixture() : null;
  return {
    schema_version: "macro_thesis_detail_v4",
    state,
    session_date: SESSION_DATE,
    cutoff_ms: CUTOFF_MS,
    reason:
      state === "published"
        ? null
        : reasonFixture("macro_thesis_current_not_published", "当前 session 尚未发布。"),
    thesis,
    live_delta: thesis ? liveDeltaFixture(thesis.publication_id) : null,
    outcome_replay: thesis ? outcomeReplayFixture(thesis.publication_id) : null,
    recovery: thesis ? recoveryFixture() : [],
    run: runFixture(state),
    history: [
      {
        schema_version: "macro_publication_history_item_v2",
        publication_schema_version: "macro_thesis_v2",
        publication_id: "macro-publication-2026-07-28",
        session_date: SESSION_DATE,
        cutoff_ms: CUTOFF_MS,
        published_at_ms: PUBLISHED_AT_MS,
        title: "真实利率回落正在缓和风险资产的贴现压力",
        stance: "call",
        confidence: "medium",
        horizon: "1w_to_1m",
      },
    ],
  };
}

export function macroModuleFixture(moduleId: MacroModuleId): MacroTypedModuleReadData {
  const common = moduleBase(moduleId);
  if (moduleId === "rates_fed") {
    return {
      ...common,
      schema_version: "macro_rates_fed_v5",
      module_id: "rates_fed",
      curve: {
        nominal_snapshots: [
          {
            window: "current",
            as_of: SESSION_DATE,
            points: [
              { tenor: "2Y", years: 2, yield_pct: 3.9 },
              { tenor: "10Y", years: 10, yield_pct: 4.15 },
            ],
          },
        ],
        real_snapshots: [],
        breakeven_snapshots: [],
        spreads: {
          "2s10s": [{ date: SESSION_DATE, value_bp: 25 }],
          "3m10s": [],
          "5s30s": [],
        },
        classification: {
          state: "bull_steepening",
          label: "牛陡",
          formula_version: "level_slope_curvature_classification_v2",
          inputs: {
            current_as_of: SESSION_DATE,
            prior_as_of: "2026-07-21",
            "2y_change_bp": -8,
            "10y_change_bp": -3,
            level_change_bp: -5.5,
            slope_change_bp: 5,
            curvature_change_bp: 0,
            current_2s10s_bp: 25,
          },
        },
      },
      policy_pricing: { rates: [indicatorFixture("fred.dff", "有效联邦基金利率", 4.33)] },
      fed: {
        institutional_stance: {
          state: "no_call",
          direction: "no_call",
          change_from_prior: "no_call",
          reason: "没有足够的当期官方材料。",
          analysis_id: null,
        },
        officials_distribution: {
          state: "no_call",
          window_days: 45,
          as_of: null,
          stance_event_counts: stanceCounts(),
          stance_unique_official_counts: stanceCounts(),
          not_policy_signal_event_count: 0,
          uncertain_event_count: 0,
          analyzed_event_count: 0,
          unique_official_count: 0,
        },
        timeline: [],
        roster: {
          state: "unavailable",
          reason: "effective_dated_roster_not_ingested",
          officials: [],
        },
      },
      positioning: [],
    };
  }
  if (moduleId === "economy_inflation") {
    return {
      ...common,
      schema_version: "macro_economy_inflation_v5",
      module_id: "economy_inflation",
      inflation: {
        indicators: [indicatorFixture("fred.cpi", "CPI 同比", 2.7)],
        official_releases: [],
      },
      labor: { indicators: [], official_releases: [] },
      growth: { indicators: [], official_releases: [] },
    };
  }
  if (moduleId === "liquidity_funding") {
    return {
      ...common,
      schema_version: "macro_liquidity_funding_v5",
      module_id: "liquidity_funding",
      balance_sheet: { indicators: [] },
      funding: {
        indicators: [indicatorFixture("fred.sofr", "SOFR", 4.34)],
        sofr_minus_iorb_bp_history: [{ date: SESSION_DATE, value: 1 }],
      },
    };
  }
  if (moduleId === "credit") {
    return {
      ...common,
      schema_version: "macro_credit_v7",
      module_id: "credit",
      cycle_dimensions: [],
      spread_ladder: {
        rows: [indicatorFixture("fred.hy", "HY OAS", 3.1)],
        tail_gap: 6.1,
        tail_gap_unit: "basis_points",
      },
      funding_costs: { corporate_yields: [], reference_rates: [], comparisons: [] },
      bank_lending: { indicators: [] },
      loan_quality: { indicators: [] },
      confirmations: { return_matrix: [], source_identity: [], positions: [] },
    };
  }
  if (moduleId === "volatility") {
    return {
      ...common,
      schema_version: "macro_volatility_v7",
      module_id: "volatility",
      term_structure: {
        spot_and_three_month: [indicatorFixture("fred.vixcls", "VIX", 15.2)],
        spread_history: [],
        official_vx_curve: [
          {
            trade_date: SESSION_DATE,
            contract_code: "VXQ26",
            contract_expiration_date: "2026-08-19",
            settlement_price: 16.2,
            open_interest: 120_000,
            volume: 45_000,
            published_at_ms: CUTOFF_MS - 120_000,
            received_at_ms: CUTOFF_MS - 60_000,
            source_url: "https://www.cboe.com/us/futures/market_statistics/historical_data/",
          },
        ],
      },
      cross_asset_implied: { indicators: [], normalized_groups: [] },
    };
  }
  return {
    ...common,
    schema_version: "macro_cross_asset_v7",
    module_id: "cross_asset",
    assets: { return_matrix: [], normalized_groups: [], source_identity: [] },
    correlations: [],
    futures: { return_matrix: [], positions: [] },
  };
}

function moduleBase(moduleId: MacroModuleId) {
  return {
    label: moduleLabel(moduleId),
    availability: "available" as const,
    reason: null,
    status: statusFixture(),
    latest_fact_at_ms: CUTOFF_MS - 30_000,
    summary: {
      headline: moduleId === "rates_fed" ? "实际利率回落。" : null,
      interpretation: moduleId === "rates_fed" ? "贴现压力边际缓和。" : null,
      top_changes: [],
    },
    evidence: {
      dataset_states: [],
      latest_facts: [],
      asset_changes: [],
      reconciliation_receipts: [],
    },
    contradictions: [],
    falsifiers: [],
    next_checkpoints: [],
    thesis_context: thesisContextFixture(moduleId),
  };
}

function thesisContextFixture(moduleId: MacroModuleId): Schemas["MacroModuleThesisContextData"] {
  const assessment =
    moduleId === "rates_fed"
      ? {
          module_id: "rates_fed" as const,
          role: "driver" as const,
          analysis: "实际利率是本次主线的主要驱动。",
          evidence_refs: ["rates:real10y"],
        }
      : null;
  return {
    state: "published",
    session_date: SESSION_DATE,
    cutoff_ms: CUTOFF_MS,
    role: assessment?.role ?? "not_material",
    assessment,
    conditions: moduleId === "rates_fed" ? macroThesisFixture().conditions : [],
    recovery: [],
    reason: null,
  };
}

function moduleSummaryFixture(moduleId: MacroModuleId): Schemas["MacroModuleSummaryData"] {
  const module = macroModuleFixture(moduleId);
  return {
    module_id: moduleId,
    label: module.label,
    availability: "available",
    reason: null,
    role: module.thesis_context.role,
    coverage_state: module.status.coverage.state,
    current_health_state: module.status.current_health.state,
    history_depth_state: module.status.history_depth.state,
    backfill_execution: module.status.backfill_execution,
    latest_fact_at_ms: module.latest_fact_at_ms,
    summary: module.summary,
    coverage_gap_count: 0,
    current_health_gap_count: 0,
    history_gap_count: 0,
    href: `/macro/${modulePath(moduleId)}`,
    thesis_context: module.thesis_context,
  };
}

function statusFixture(): Schemas["MacroModuleStatusData"] {
  return {
    coverage: {
      state: "complete",
      expected_capabilities: 1,
      available_capabilities: 1,
      capabilities: [],
    },
    current_health: {
      state: "current",
      current_datasets: 1,
      tracked_datasets: 1,
      as_of_ms: CUTOFF_MS,
      groups: [],
    },
    history_depth: {
      state: "complete",
      complete_datasets: 1,
      tracked_datasets: 1,
    },
    backfill_execution: {
      state: "not_required",
      worker_enabled: true,
      total_targets: 0,
      complete_targets: 0,
      pending_targets: 0,
      failed_targets: 0,
      next_check_at_ms: null,
      reason: null,
    },
  };
}

function runFixture(status: string): Schemas["MacroThesisRunData"] {
  return {
    session_date: SESSION_DATE,
    status,
    evidence_pack_id: "macro-pack-2026-07-28",
    research_input_id: "macro-input-2026-07-28",
    attempt_count: 1,
    max_attempts: 2,
    error_code: null,
    gate_category: null,
    candidate_hash: HASH_A,
    reason: null,
    updated_at_ms: PUBLISHED_AT_MS,
  };
}

function liveDeltaFixture(publicationId: string): MacroLiveDeltaV2 {
  return {
    schema_version: "macro_live_delta_v2",
    live_delta_id: "live-delta-1",
    publication_id: publicationId,
    evaluated_at_ms: READ_AT_MS,
    module_fact_cutoff_ms: READ_AT_MS - 30_000,
    mainline_validity: "confirming",
    items: [
      {
        item_type: "metric_condition",
        condition_id: "rates.real10y.tail:fred.dfii10:leq20",
        candidate_id: "rates.real10y.tail:fred.dfii10:leq20",
        scope_kind: "mainline",
        scope_id: "mainline",
        kind: "confirmation",
        state: "confirming",
        dataset_id: "fred.dfii10",
        metric: "percentile_5y",
        observed_value: 18,
        observed_at_ms: READ_AT_MS - 30_000,
        operator: "lte",
        threshold: 20,
        reason_code: "condition_true",
      },
    ],
    reason_codes: ["condition_true"],
    input_hash: HASH_B,
  };
}

function outcomeReplayFixture(publicationId: string): MacroOutcomeReplayV2 {
  return {
    schema_version: "macro_outcome_replay_v2",
    replay_id: "outcome-replay-1",
    publication_id: publicationId,
    evaluated_at_ms: READ_AT_MS,
    horizons: [
      {
        horizon: "1w",
        expires_at_ms: PUBLISHED_AT_MS + 7 * 86_400_000,
        status: "pending",
        reason_code: "horizon_not_due",
        asset_results: [
          {
            symbol: "SPY",
            horizon: "1w",
            expires_at_ms: PUBLISHED_AT_MS + 7 * 86_400_000,
            status: "pending",
            published_direction: "bullish",
            realized_return_pct: null,
            direction_correct: null,
            reason_code: "horizon_not_due",
          },
        ],
      },
    ],
    input_hash: HASH_C,
  };
}

function recoveryFixture(): Schemas["MacroRecoveryItem"][] {
  return ASSETS.map((symbol) => ({
    scope_kind: "asset",
    scope_id: symbol,
    state: "unchanged",
    publication: {
      dataset_id: symbol === "SPY" ? "nasdaq.spy.daily" : null,
      source_id: symbol === "SPY" ? "NASDAQ" : null,
      value: symbol === "SPY" ? 2.8 : null,
      unit: "percent",
      as_of: symbol === "SPY" ? SESSION_DATE : null,
    },
    current: {
      dataset_id: symbol === "SPY" ? "nasdaq.spy.daily" : null,
      source_id: symbol === "SPY" ? "NASDAQ" : null,
      value: symbol === "SPY" ? 2.8 : null,
      unit: "percent",
      as_of: symbol === "SPY" ? SESSION_DATE : null,
    },
    reason: "asset_fact_unchanged",
  }));
}

function indicatorFixture(
  datasetId: string,
  label: string,
  value: number,
): Schemas["MacroIndicatorData"] {
  return {
    dataset_id: datasetId,
    label,
    latest_value: value,
    unit: "percent",
    as_of: SESSION_DATE,
    change_1w: -0.1,
    change_1m: -0.2,
    sample_count: 2,
    percentile: null,
    history_start: "2026-07-21",
    history_end: SESSION_DATE,
    source_url: "https://fred.stlouisfed.org/",
    history: [
      { date: "2026-07-21", value: value + 0.1 },
      { date: SESSION_DATE, value },
    ],
  };
}

function reasonFixture(code: string, message: string): Schemas["MacroReason"] {
  return {
    code,
    message,
    impact: "limited",
    affected_dataset_ids: [],
    affected_claim_ids: [],
    retryable: false,
    recovery: "none",
    next_action: null,
    next_check_at_ms: null,
  };
}

function stanceCounts(): Schemas["MacroFedStanceCountsData"] {
  return { hawkish: 0, neutral: 0, dovish: 0, mixed: 0 };
}

function moduleLabel(moduleId: MacroModuleId): string {
  return {
    rates_fed: "利率与美联储",
    economy_inflation: "经济与通胀",
    liquidity_funding: "流动性与融资",
    credit: "信用市场",
    volatility: "波动率",
    cross_asset: "大类资产与期货",
  }[moduleId];
}

function modulePath(moduleId: MacroModuleId): string {
  return {
    rates_fed: "rates-fed",
    economy_inflation: "economy-inflation",
    liquidity_funding: "liquidity-funding",
    credit: "credit",
    volatility: "volatility",
    cross_asset: "cross-asset",
  }[moduleId];
}
