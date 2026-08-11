import type {
  MacroModuleId,
  MacroOverviewReadData,
  MacroTypedModuleReadData,
} from "@features/macro";
import type { components } from "@lib/types/openapi";

type Schemas = components["schemas"];

const SESSION_DATE = "2026-07-28";
const CUTOFF_MS = Date.parse("2026-07-28T12:50:00Z");
const READ_AT_MS = CUTOFF_MS + 150_000;
const MODULES: readonly MacroModuleId[] = [
  "rates_fed",
  "economy_inflation",
  "liquidity_funding",
  "credit",
  "volatility",
  "cross_asset",
];

export function macroOverviewFixture(
  overrides: Partial<MacroOverviewReadData> = {},
): MacroOverviewReadData {
  return {
    schema_version: "macro_overview_v9",
    read_at_ms: READ_AT_MS,
    transport: {
      state: "current",
      last_successful_read_at_ms: READ_AT_MS,
      reason: null,
    },
    latest_fact_at_ms: CUTOFF_MS - 30_000,
    modules: MODULES.map(moduleSummaryFixture),
    data_quality: {
      coverage_state: "complete",
      current_health_state: "current",
      history_depth_state: "complete",
      coverage_gap_count: 0,
      current_health_gap_count: 0,
      history_gap_count: 0,
    },
    ...overrides,
  };
}

export function macroModuleFixture(moduleId: MacroModuleId): MacroTypedModuleReadData {
  if (moduleId === "rates_fed") {
    return {
      ...moduleCore(moduleId),
      schema_version: "macro_rates_fed_v8",
      module_id: "rates_fed",
      document_analysis_runtime: {
        state: "disabled",
        enabled: false,
        configured: false,
        worker_active: false,
        model: "gpt-5.4-mini",
      },
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
        meeting_calendar: {
          revision_id: "fomc-calendar-2026-07-28",
          meetings: [
            {
              meeting_id: "FOMC:2026-09-15:2026-09-16",
              start_date: "2026-09-15",
              end_date: "2026-09-16",
              has_sep: true,
              calendar_published_at_ms: CUTOFF_MS - 86_400_000,
              received_at_ms: CUTOFF_MS - 30_000,
              source_url: "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
            },
          ],
        },
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
      treasury_auctions: {
        recent_results: [
          {
            auction_id: "TREASURY_AUCTION:91282CQB0:2026-07-27",
            cusip: "91282CQB0",
            security_term: "10-Year Note",
            auction_date: "2026-07-27",
            scheduled_at_ms: Date.parse("2026-07-27T17:00:00Z"),
            published_at_ms: Date.parse("2026-07-27T17:00:00Z"),
            received_at_ms: CUTOFF_MS - 30_000,
            source_url: "https://fiscal.treasury.gov/reports-statements/treasury-auctions/",
            bid_to_cover_ratio: 2.67,
            high_discount_rate_pct: null,
            high_investment_rate_pct: null,
            high_yield_pct: 4.321,
            offering_amount_usd: 42_000_000_000,
            indirect_award_share_pct: 70,
            direct_award_share_pct: 12.5,
            primary_dealer_award_share_pct: 17.5,
          },
        ],
      },
      positioning: [],
    };
  }
  const common = moduleBase(moduleId);
  if (moduleId === "economy_inflation") {
    return {
      ...common,
      schema_version: "macro_economy_inflation_v6",
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
    schema_version: "macro_cross_asset_v8",
    module_id: "cross_asset",
    assets: { return_matrix: [], normalized_groups: [], source_identity: [] },
    correlation_contract: {
      default_window: "90_daily_returns",
      minimum_common_observations: 20,
      presentation_derivation: "undirected_pairs_mirrored_with_unit_diagonal",
      supported_windows: ["30_daily_returns", "90_daily_returns", "252_daily_returns"],
    },
    correlations: correlationFixture(),
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
  };
}

function moduleSummaryFixture(moduleId: MacroModuleId): Schemas["MacroModuleSummaryData"] {
  const module = macroModuleFixture(moduleId);
  return {
    module_id: moduleId,
    label: module.label,
    availability: "available",
    reason: null,
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
      total_targets: 0,
      complete_targets: 0,
      pending_targets: 0,
      failed_targets: 0,
      next_check_at_ms: null,
      reason: null,
    },
  };
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

function correlationFixture(): Schemas["MacroCorrelationData"][] {
  const symbols = ["SPY", "QQQ", "IWM", "TLT", "HYG", "LQD", "GLD", "USO", "UUP", "EEM"];
  return symbols.flatMap((left, leftIndex) =>
    symbols.slice(leftIndex + 1).map((right, rightOffset) => ({
      correlation: Number((0.75 - (leftIndex + rightOffset) * 0.08).toFixed(2)),
      left,
      right,
      sample_count: 90,
      window: "90_daily_returns" as const,
    })),
  );
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
