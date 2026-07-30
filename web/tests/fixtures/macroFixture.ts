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
        candidate_id: "fred.dfii10:2026-07-28",
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
        kind: "falsifier",
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
      prompt_version: "macro_thesis_sop_v3",
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
  const result = { ...overview, ...overrides };
  if (result.thesis) return result;
  return {
    ...result,
    modules: result.modules.map((module) => ({
      ...module,
      role: "unassessed",
      thesis_context: {
        ...module.thesis_context,
        state: result.thesis_state,
        role: "unassessed",
        assessment: null,
        conditions: [],
        recovery: [],
        reason: result.thesis_reason,
      },
    })),
  };
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
        : reasonFixture(
            `macro_thesis_${state}`,
            state === "running"
              ? "Thin Agent 正在执行本次唯一模型调用。"
              : "今日研究未发布；当前读取不会回退到历史主线。",
          ),
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
  if (moduleId === "rates_fed") {
    return {
      ...moduleCore(moduleId),
      schema_version: "macro_rates_fed_v6",
      module_id: "rates_fed",
      decision: {
        state: "available",
        reference_date: "2026-07-29",
        headline: "最近完整交易日：2Y 下行4bp，10Y 上行6bp，30Y 上行11bp（2026-07-29）",
        session_completeness: {
          state: "complete",
          reference_date: "2026-07-29",
          required_tenors: ["2Y", "10Y", "30Y"],
          latest_observations: (["2Y", "10Y", "30Y"] as const).map((tenor) => ({
            tenor,
            reference_date: "2026-07-29",
            fact_id: ratesFactId("treasury.daily_nominal_curve", tenor, "2026-07-29"),
          })),
          reason: null,
        },
        tenor_matrix: [
          ratesTenorFixture("2Y", 4.22, -4),
          ratesTenorFixture("10Y", 4.67, 6),
          ratesTenorFixture("30Y", 5.2, 11),
        ],
        spread_summary: [
          {
            spread_id: "2s10s",
            label: "2Y–10Y",
            state: "available",
            current_date: "2026-07-29",
            prior_date: "2026-07-28",
            value_bp: 45,
            change_1d_bp: 10,
            input_fact_ids: [],
          },
          {
            spread_id: "10s30s",
            label: "10Y–30Y",
            state: "available",
            current_date: "2026-07-29",
            prior_date: "2026-07-28",
            value_bp: 53,
            change_1d_bp: 5,
            input_fact_ids: [],
          },
        ],
        decompositions: [
          {
            tenor: "10Y",
            state: "available",
            current_date: "2026-07-29",
            prior_date: "2026-07-28",
            nominal_change_bp: 6,
            real_change_bp: 0,
            breakeven_change_bp: 6,
            assessment_state: "inflation_compensation_dominant",
            assessment: "10Y 名义收益率上行6bp主要由通胀补偿变化推动。",
            input_fact_ids: [],
            gap: null,
          },
          {
            tenor: "30Y",
            state: "available",
            current_date: "2026-07-29",
            prior_date: "2026-07-28",
            nominal_change_bp: 11,
            real_change_bp: 6,
            breakeven_change_bp: 5,
            assessment_state: "mixed_real_and_inflation_compensation",
            assessment: "30Y 名义收益率变化同时包含实际收益率与通胀补偿变化。",
            input_fact_ids: [],
            gap: null,
          },
        ],
        classifications: [
          {
            window: "1d",
            state: "twist_steepening",
            label: "扭转式陡峭化",
            formula_version: "level_slope_curvature_classification_v3",
            inputs: {
              current_as_of: "2026-07-29",
              prior_as_of: "2026-07-28",
              "2y_change_bp": -4,
              "10y_change_bp": 6,
              "30y_change_bp": 11,
              level_change_bp: 4.3333,
              slope_change_bp: 10,
              curvature_change_bp: 5,
              current_2s10s_bp: 45,
              current_10s30s_bp: 53,
            },
          },
          ...(["1w", "mtd", "3m"] as const).map((window) => ({
            window,
            state: "insufficient_history" as const,
            label: "历史不足",
            formula_version: "level_slope_curvature_classification_v3" as const,
            inputs: {
              current_as_of: "2026-07-29",
              prior_as_of: null,
              "2y_change_bp": null,
              "10y_change_bp": null,
              "30y_change_bp": null,
              level_change_bp: null,
              slope_change_bp: null,
              curvature_change_bp: null,
              current_2s10s_bp: null,
              current_10s30s_bp: null,
            },
          })),
        ],
        explanation: {
          facts: [
            {
              statement: "最近完整交易日：2Y 下行4bp，10Y 上行6bp，30Y 上行11bp（2026-07-29）",
              input_fact_ids: [],
            },
          ],
          bounded_assessments: [
            {
              assessment_id: "10y_one_day_decomposition",
              statement: "10Y 名义收益率上行6bp主要由通胀补偿变化推动。",
              input_fact_ids: [],
              uncertainty: "仅描述名义、实际与 Breakeven 的同日机械分解。",
            },
          ],
          hypotheses: [],
        },
        source_policy: {
          decision_primary_dataset_ids: [
            "treasury.daily_nominal_curve",
            "treasury.daily_real_curve",
          ],
          history_only_dataset_ids: [
            "fred.dgs2",
            "fred.dgs10",
            "fred.dgs30",
            "fred.dfii10",
            "fred.t10yie",
          ],
          selection_policy: "treasury_completed_session_primary_fred_history_only",
        },
      },
      curve: {
        nominal_snapshots: [
          {
            window: "current",
            as_of: "2026-07-29",
            points: [
              ratesCurvePoint("2Y", 2, 4.22, "2026-07-29"),
              ratesCurvePoint("10Y", 10, 4.67, "2026-07-29"),
              ratesCurvePoint("30Y", 30, 5.2, "2026-07-29"),
            ],
          },
          {
            window: "previous",
            as_of: "2026-07-28",
            points: [
              ratesCurvePoint("2Y", 2, 4.26, "2026-07-28"),
              ratesCurvePoint("10Y", 10, 4.61, "2026-07-28"),
              ratesCurvePoint("30Y", 30, 5.09, "2026-07-28"),
            ],
          },
          {
            window: "1w",
            as_of: "2026-07-22",
            points: [
              ratesCurvePoint("2Y", 2, 4.28, "2026-07-22"),
              ratesCurvePoint("10Y", 10, 4.55, "2026-07-22"),
              ratesCurvePoint("30Y", 30, 5.06, "2026-07-22"),
            ],
          },
        ],
        real_snapshots: [
          {
            window: "current",
            as_of: "2026-07-29",
            points: [
              ratesCurvePoint("10Y", 10, 2.41, "2026-07-29", "treasury.daily_real_curve"),
              ratesCurvePoint("30Y", 30, 2.98, "2026-07-29", "treasury.daily_real_curve"),
            ],
          },
          {
            window: "previous",
            as_of: "2026-07-28",
            points: [
              ratesCurvePoint("10Y", 10, 2.41, "2026-07-28", "treasury.daily_real_curve"),
              ratesCurvePoint("30Y", 30, 2.92, "2026-07-28", "treasury.daily_real_curve"),
            ],
          },
        ],
        breakeven_snapshots: [
          {
            window: "current",
            as_of: "2026-07-29",
            points: [
              { tenor: "10Y", years: 10, breakeven_pct: 2.26, input_fact_ids: [] },
              { tenor: "30Y", years: 30, breakeven_pct: 2.22, input_fact_ids: [] },
            ],
          },
          {
            window: "previous",
            as_of: "2026-07-28",
            points: [
              { tenor: "10Y", years: 10, breakeven_pct: 2.2, input_fact_ids: [] },
              { tenor: "30Y", years: 30, breakeven_pct: 2.17, input_fact_ids: [] },
            ],
          },
        ],
        spreads: {
          "2s10s": [
            { date: "2026-07-28", value_bp: 35, input_fact_ids: [] },
            { date: "2026-07-29", value_bp: 45, input_fact_ids: [] },
          ],
          "10s30s": [
            { date: "2026-07-28", value_bp: 48, input_fact_ids: [] },
            { date: "2026-07-29", value_bp: 53, input_fact_ids: [] },
          ],
          "3m10s": [],
          "5s30s": [],
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
  const common = moduleBase(moduleId);
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
    ...moduleCore(moduleId),
    summary: {
      headline: null,
      interpretation: null,
      top_changes: [],
    },
    contradictions: [],
    falsifiers: [],
  };
}

function moduleCore(moduleId: MacroModuleId) {
  return {
    label: moduleLabel(moduleId),
    availability: "available" as const,
    reason: null,
    status: statusFixture(),
    latest_fact_at_ms: CUTOFF_MS - 30_000,
    evidence: {
      dataset_states: [],
      latest_facts: [],
      asset_changes: [],
      reconciliation_receipts: [],
    },
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
    summary:
      module.module_id === "rates_fed"
        ? {
            headline: module.decision.headline,
            interpretation: module.decision.explanation.bounded_assessments[0]?.statement ?? null,
          }
        : {
            headline: module.summary.headline,
            interpretation: module.summary.interpretation,
          },
    coverage_gap_count: 0,
    current_health_gap_count: 0,
    history_gap_count: 0,
    href: `/macro/${modulePath(moduleId)}`,
    thesis_context: module.thesis_context,
  };
}

function ratesTenorFixture(
  tenor: "2Y" | "10Y" | "30Y",
  yieldPct: number,
  oneDayChange: number,
): Schemas["MacroRatesTenorDecisionData"] {
  const currentDate = "2026-07-29";
  const priorDate = "2026-07-28";
  const oneWeekChange = { "2Y": -6, "10Y": 12, "30Y": 14 }[tenor];
  const mtdChange = { "2Y": -9, "10Y": 24, "30Y": 24 }[tenor];
  return {
    tenor,
    current: {
      tenor,
      reference_date: currentDate,
      yield_pct: yieldPct,
      dataset_id: "treasury.daily_nominal_curve",
      source_role: "decision_primary",
      fact_id: ratesFactId("treasury.daily_nominal_curve", tenor, currentDate),
      source_url: "https://home.treasury.gov/resource-center/data-chart-center/interest-rates",
    },
    windows: [
      {
        window: "1d",
        state: "available",
        current_date: currentDate,
        baseline_date: priorDate,
        change_bp: oneDayChange,
        selection_policy: "previous_treasury_observation",
        input_fact_ids: [
          ratesFactId("treasury.daily_nominal_curve", tenor, priorDate),
          ratesFactId("treasury.daily_nominal_curve", tenor, currentDate),
        ],
      },
      {
        window: "1w",
        state: "available",
        current_date: currentDate,
        baseline_date: "2026-07-22",
        change_bp: oneWeekChange,
        selection_policy: "bounded_previous_observation_4_calendar_days",
        input_fact_ids: [
          ratesFactId("treasury.daily_nominal_curve", tenor, "2026-07-22"),
          ratesFactId("treasury.daily_nominal_curve", tenor, currentDate),
        ],
      },
      {
        window: "mtd",
        state: "available",
        current_date: currentDate,
        baseline_date: "2026-07-01",
        change_bp: mtdChange,
        selection_policy: "first_available_treasury_observation_in_calendar_month",
        input_fact_ids: [
          ratesFactId("treasury.daily_nominal_curve", tenor, "2026-07-01"),
          ratesFactId("treasury.daily_nominal_curve", tenor, currentDate),
        ],
      },
      {
        window: "3m",
        state: "unavailable",
        current_date: currentDate,
        baseline_date: null,
        change_bp: null,
        selection_policy: "bounded_previous_observation_4_calendar_days",
        input_fact_ids: [],
      },
      {
        window: "past_30d",
        state: "unavailable",
        current_date: currentDate,
        baseline_date: null,
        change_bp: null,
        selection_policy: "bounded_previous_observation_4_calendar_days",
        input_fact_ids: [],
      },
    ],
  };
}

function ratesCurvePoint(
  tenor: string,
  years: number,
  yieldPct: number,
  referenceDate: string,
  datasetId = "treasury.daily_nominal_curve",
): Schemas["MacroCurveYieldPointData"] {
  return {
    tenor,
    years,
    yield_pct: yieldPct,
    dataset_id: datasetId,
    source_role: "decision_primary",
    fact_id: ratesFactId(datasetId, tenor, referenceDate),
    source_url: "https://home.treasury.gov/resource-center/data-chart-center/interest-rates",
  };
}

function ratesFactId(datasetId: string, tenor: string, referenceDate: string): string {
  return `fact:${datasetId}:${tenor}:${referenceDate}`;
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
  const terminalContract = status === "not_published";
  const hasCandidate = status === "published" || terminalContract;
  return {
    session_date: SESSION_DATE,
    status,
    evidence_pack_id: "macro-pack-2026-07-28",
    research_input_id: "macro-input-2026-07-28",
    attempt_count: 1,
    max_attempts: 2,
    error_code: terminalContract ? "macro_thesis_contract_binding_invalid" : null,
    gate_category: terminalContract ? "contract_validity" : null,
    candidate_hash: hasCandidate ? HASH_A : null,
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
