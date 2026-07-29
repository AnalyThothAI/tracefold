import type {
  MacroModuleId,
  MacroOverviewReadData,
  MacroThesisDetailReadData,
  MacroThesisV1,
  MacroTypedModuleReadData,
} from "@features/macro";

const NOW = 1_785_158_400_000;
const MODULE_IDS: MacroModuleId[] = [
  "rates_fed",
  "economy_inflation",
  "liquidity_funding",
  "credit",
  "volatility",
  "cross_asset",
];
const ASSETS = ["SPY", "QQQ", "IWM", "TLT", "IEF", "LQD", "HYG", "UUP", "GLD", "USO", "BTC", "VIX"];

export function macroOverviewFixture(): MacroOverviewReadData {
  const thesis = macroThesisFixture();
  return {
    schema_version: "macro_overview_v5",
    read_at_ms: NOW,
    transport: {
      state: "current",
      last_successful_read_at_ms: NOW,
      reason: null,
    },
    session_date: "2026-07-27",
    cutoff_ms: NOW - 3_600_000,
    latest_fact_at_ms: NOW - 600_000,
    thesis_state: "published",
    thesis,
    live_delta: {
      schema_version: "macro_live_delta_v1",
      live_delta_id: "mld_fixture",
      publication_id: thesis.publication_id,
      evaluated_at_ms: NOW,
      module_fact_cutoff_ms: NOW - 60_000,
      status: "confirming",
      matched_claim_ids: ["claim-rates"],
      matched_falsifier_ids: [],
      matched_checkpoint_ids: ["checkpoint-credit"],
      items: [
        {
          binding_type: "checkpoint",
          binding_id: "checkpoint-credit",
          condition_id: "checkpoint-credit",
          status: "confirming",
          dataset_id: "fred.bamlh0a0hym2",
          metric_name: "change_1w_bp",
          observed_value: 22,
          operator: "gte",
          threshold: 20,
          reason_code: "condition_threshold_matched",
        },
      ],
      reason_codes: ["condition_threshold_matched"],
      input_hash: `sha256:${"3".repeat(64)}`,
    },
    outcome_replay: {
      schema_version: "macro_outcome_replay_v1",
      replay_id: "mor_fixture",
      publication_id: thesis.publication_id,
      evaluated_at_ms: NOW,
      horizons: (["1d", "1w", "1m"] as const).map((horizon, index) => ({
        horizon,
        expires_at_ms: NOW + (index + 1) * 86_400_000,
        status: "pending",
        benchmark_symbol: "SPY",
        realized_return_pct: null,
        direction_correct: null,
        reason_code: "horizon_not_expired",
        asset_results: [],
      })),
      input_hash: `sha256:${"4".repeat(64)}`,
    },
    modules: MODULE_IDS.map((moduleId) => {
      const module = macroModuleFixture(moduleId);
      const role = thesis.module_assessments.find((item) => item.module_id === moduleId)!;
      return {
        module_id: moduleId,
        label: module.label,
        role: role.role,
        coverage_state: module.status.coverage.state,
        current_health_state: module.status.current_health.state,
        history_depth_state: module.status.history_depth.state,
        latest_fact_at_ms: module.latest_fact_at_ms,
        summary: module.summary,
        coverage_gap_count: 0,
        current_health_gap_count: 0,
        history_gap_count: 0,
        href: modulePath(moduleId),
      };
    }),
    data_quality: {
      coverage_state: "complete",
      current_health_state: "current",
      history_depth_state: "complete",
      coverage_gap_count: 0,
      current_health_gap_count: 0,
      history_gap_count: 0,
    },
  };
}

export function macroThesisFixture(): MacroThesisV1 {
  const moduleRef = (moduleId: MacroModuleId) => `macro-module:2026-07-27:${moduleId}`;
  return {
    schema_version: "macro_thesis_v1",
    publication_id: "mth_fixture",
    session_date: "2026-07-27",
    cutoff_ms: NOW - 3_600_000,
    evidence_pack_id: "mep_fixture",
    evidence_pack_hash: `sha256:${"1".repeat(64)}`,
    prior_publication_id: null,
    mainline: {
      stance: "call",
      title: "实际利率上行主导短期风险资产定价",
      thesis: "短端实际利率上行仍是当前市场主线，信用尚未全面确认压力。",
      stage: "developing",
      confidence: "medium",
      horizon: "1w",
      claims: [
        {
          claim_id: "claim-rates",
          statement: "实际利率上行压制长久期风险资产估值。",
          causal_edges: [
            {
              source: "实际利率上行",
              mechanism: "贴现率抬升",
              target: "长久期资产承压",
              evidence_refs: [moduleRef("rates_fed")],
              conflicting_evidence_refs: [moduleRef("credit")],
            },
          ],
          supporting_evidence_refs: [moduleRef("rates_fed")],
          conflicting_evidence_refs: [moduleRef("credit")],
          conditions: [],
        },
      ],
      supporting_evidence_refs: [moduleRef("rates_fed")],
      conflicting_evidence_refs: [moduleRef("credit")],
      falsifiers: [
        {
          condition_id: "falsifier-rates",
          module_id: "rates_fed",
          dataset_id: "fred.dgs2",
          metric_name: "change_1w_bp",
          operator: "lte",
          threshold: -25,
          effect: "invalidation_triggered",
          rationale: "2Y 收益率周变化明显反向。",
        },
      ],
      checkpoints: [
        {
          condition_id: "checkpoint-credit",
          module_id: "credit",
          dataset_id: "fred.bamlh0a0hym2",
          metric_name: "change_1w_bp",
          operator: "gte",
          threshold: 20,
          effect: "confirming",
          rationale: "高收益信用利差确认融资压力。",
        },
      ],
    },
    alternative_explanation: {
      title: "增长重新加速吸收利率冲击",
      thesis: "若增长与盈利预期改善，风险资产可在高实际利率下保持韧性。",
      causal_edges: [
        {
          source: "增长重新加速",
          mechanism: "盈利预期上修",
          target: "风险资产韧性",
          evidence_refs: [moduleRef("economy_inflation")],
          conflicting_evidence_refs: [moduleRef("rates_fed")],
        },
      ],
      supporting_evidence_refs: [moduleRef("economy_inflation")],
      conflicting_evidence_refs: [moduleRef("rates_fed")],
      trigger_conditions: [
        {
          condition_id: "alternative-growth",
          module_id: "rates_fed",
          dataset_id: "fred.dgs2",
          metric_name: "change_1w_bp",
          operator: "lte",
          threshold: 0,
          effect: "weakening",
          rationale: "利率压力停止上升。",
        },
      ],
    },
    core_tensions: [
      {
        tension_id: "tension-credit",
        statement: "信用利差尚未确认利率压力。",
        side_a: {
          label: "利率压力",
          statement: "实际利率持续抬升。",
          evidence_refs: [moduleRef("rates_fed")],
        },
        side_b: {
          label: "信用韧性",
          statement: "信用利差仍然稳定。",
          evidence_refs: [moduleRef("credit")],
        },
        leading_side: "side_a",
        lagging_signal: "高收益信用利差",
        unresolved_reason: "融资与违约压力尚未共振。",
        resolution_triggers: [
          {
            condition_id: "tension-credit-resolution",
            module_id: "credit",
            dataset_id: "fred.bamlh0a0hym2",
            metric_name: "change_1w_bp",
            operator: "gte",
            threshold: 20,
            effect: "confirming",
            rationale: "信用确认利率压力。",
          },
        ],
      },
    ],
    changes_from_prior: [
      {
        change_id: "change-real-rates",
        status: "new",
        statement: "2Y 实际利率的一周上行幅度进入本期最重要变化。",
        evidence_refs: [moduleRef("rates_fed")],
      },
    ],
    module_assessments: MODULE_IDS.map((moduleId) => ({
      module_id: moduleId,
      role: moduleId === "rates_fed" ? "driver" : "confirming",
      analysis:
        moduleId === "rates_fed"
          ? "利率模块定义当前主线。"
          : `${moduleLabel(moduleId)}用于确认或反驳利率主线。`,
      claim_ids: ["claim-rates"],
      supporting_evidence_refs: [moduleRef(moduleId)],
      conflicting_evidence_refs: [],
    })),
    assets: ASSETS.map((symbol) => ({
      symbol,
      momentum: {
        symbol,
        momentum_1w: symbol === "QQQ" ? "down" : "up",
        momentum_1m: symbol === "QQQ" ? "down" : "up",
        return_1w_pct: symbol === "QQQ" ? -1.2 : 0.3,
        return_1m_pct: symbol === "QQQ" ? -2.1 : 0.8,
        source_dataset_id: `fixture.${symbol.toLowerCase()}`,
        as_of: "2026-07-27",
      },
      outlook_1w: {
        horizon: "1w",
        direction: "no_call",
        causal_channel: "等待利率与信用共同确认。",
        supporting_evidence_refs: [moduleRef("rates_fed")],
        conflicting_evidence_refs: [moduleRef("credit")],
        confirmation_triggers: [],
        falsifiers: [],
        checkpoints: [],
        confidence: "low",
      },
      outlook_1m: {
        horizon: "1m",
        direction: "no_call",
        causal_channel: "等待更完整的月度证据。",
        supporting_evidence_refs: [moduleRef("rates_fed")],
        conflicting_evidence_refs: [moduleRef("credit")],
        confirmation_triggers: [],
        falsifiers: [],
        checkpoints: [],
        confidence: "low",
      },
    })),
    gaps: [],
    citations: MODULE_IDS.map((moduleId) => ({
      evidence_ref: moduleRef(moduleId),
      module_id: moduleId,
      dataset_id: null,
      source_role: null,
      label: moduleLabel(moduleId),
      reference: "2026-07-27",
      published_at_ms: null,
      received_at_ms: NOW - 600_000,
      source_url: null,
    })),
    narrative_sections: [
      {
        section_id: "mainline",
        title: "市场主线",
        markdown: "实际利率上行仍然主导短期跨资产定价。",
        evidence_refs: [moduleRef("rates_fed"), moduleRef("credit")],
      },
    ],
    review: {
      draft_hash: `sha256:${"2".repeat(64)}`,
      disposition: "pass",
      findings: ["证据引用、反证与资产条件已独立复核。"],
      required_changes: [],
      invocation_id: "review-fixture",
      model_name: "openai/gpt-5.4-mini",
      prompt_version: "macro-thesis-review-v1",
    },
    provenance: {
      draft_hash: `sha256:${"2".repeat(64)}`,
      research_invocation_id: "research-fixture",
      research_model: "openai/gpt-5.4-mini",
      reviewer_model: "openai/gpt-5.4-mini",
      research_prompt_version: "macro-thesis-research-v1",
      reviewer_prompt_version: "macro-thesis-review-v1",
      workflow_version: "macro_thesis_workflow_v1",
    },
    published_at_ms: NOW - 3_000_000,
  };
}

export function macroModuleFixture(moduleId: MacroModuleId): MacroTypedModuleReadData {
  const base = moduleBase(moduleId);
  if (moduleId === "rates_fed") {
    return {
      ...base,
      schema_version: "macro_rates_fed_v4",
      module_id: "rates_fed",
      curve: {
        classification: {
          state: "bear_steepening",
          label: "熊市陡峭化",
          formula_version: "curve-shape-v1",
        },
        nominal_snapshots: curveSnapshots("yield_pct", [4.3, 4.15, 4.22, 4.4, 4.62]),
        real_snapshots: curveSnapshots("yield_pct", [1.7, 1.85, 1.94, 2.08, 2.22]),
        breakeven_snapshots: curveSnapshots("breakeven_pct", [2.6, 2.3, 2.28, 2.32, 2.4]),
        spreads: {
          "2s10s": historyRows([5, 12, 22, 35], "value_bp"),
          "3m10s": historyRows([-25, -18, -6, 10], "value_bp"),
          "5s30s": historyRows([18, 22, 30, 40], "value_bp"),
        },
      },
      policy_pricing: {
        rates: [
          indicator("fred.effr", "有效联邦基金利率", "percent", [4.33, 4.33, 4.33, 4.33]),
          indicator("fred.dfedtaru", "目标区间上限", "percent", [4.5, 4.5, 4.5, 4.5]),
          indicator("fred.dfedtarl", "目标区间下限", "percent", [4.25, 4.25, 4.25, 4.25]),
          indicator("fred.sofr", "SOFR", "percent", [4.31, 4.32, 4.34, 4.35]),
          indicator("fred.dgs2", "2Y Treasury", "percent", [4.06, 4.14, 4.2, 4.3]),
          indicator("fred.dgs10", "10Y Treasury", "percent", [4.1, 4.24, 4.36, 4.4]),
        ],
      },
      fed: {
        institutional_stance: {
          state: "current",
          direction: "hawkish",
          reason: "analysis:fomc-fixture",
        },
        officials_distribution: {
          state: "current",
          window_days: 90,
          as_of: "2026-07-27",
          stance_event_counts: { hawkish: 4, neutral: 2, dovish: 1, mixed: 1 },
          stance_unique_official_counts: { hawkish: 3, neutral: 2, dovish: 1, mixed: 1 },
        },
        timeline: [
          {
            document_id: "fed-fixture",
            document_type: "statement",
            title: "Federal Reserve issues FOMC statement",
            effective_date: "2026-07-23",
            source_url: "https://example.com/fed",
            speaker_name: null,
            role_title: "FOMC",
            fomc_voter: null,
            analysis: { stance: "hawkish" },
          },
        ],
        roster: { state: "current", officials: [] },
      },
      positioning: [
        {
          contract_code: "TY",
          contract_name: "10-Year Treasury Note",
          report_date: "2026-07-21",
          leveraged_net_pct_oi: -14,
          asset_manager_net_pct_oi: 28,
          dealer_net_pct_oi: 0,
          source_url: "https://example.com/cftc",
        },
      ],
    };
  }
  if (moduleId === "economy_inflation") {
    return {
      ...base,
      schema_version: "macro_economy_inflation_v4",
      module_id: "economy_inflation",
      inflation: {
        indicators: [
          indicator("fred.cpiaucsl", "CPI", "index", [308, 309, 310, 311]),
          indicator("fred.cpilfesl", "核心 CPI", "index", [315, 316, 317, 318]),
          indicator("fred.pcepi", "PCE", "index", [121, 122, 123, 124]),
          indicator("fred.pcepilfe", "核心 PCE", "index", [119, 120, 121, 122]),
        ],
        official_releases: [release("bls.cpi.release", "CPI 官方发布", 3.1, 3, 3)],
      },
      labor: {
        indicators: [
          indicator(
            "fred.payems",
            "非农就业",
            "thousands_persons",
            [158_000, 159_000, 160_000, 161_000],
          ),
          indicator("fred.unrate", "失业率", "percent", [4, 4, 4.1, 4.1]),
          indicator("fred.icsa", "初请失业金", "thousands_persons", [231, 228, 225, 227]),
        ],
        official_releases: [release("bls.payrolls.release", "非农官方发布", 185, 175, 170)],
      },
      growth: {
        indicators: [
          indicator("fred.gdpc1", "实际 GDP", "billions_usd", [23_500, 23_700, 23_850, 24_000]),
          indicator("fred.rsafs", "零售销售", "millions_usd", [710_000, 714_000, 717_000, 720_000]),
          indicator("fred.indpro", "工业生产", "index", [102, 102.4, 102.2, 102.8]),
        ],
        official_releases: [release("bea.gdp.release", "GDP 官方发布", 2.4, 2.2, 2.1)],
      },
    };
  }
  if (moduleId === "liquidity_funding") {
    return {
      ...base,
      schema_version: "macro_liquidity_funding_v4",
      module_id: "liquidity_funding",
      balance_sheet: {
        indicators: [
          indicator("fred.walcl", "Fed 总资产", "billions_usd", [6_600, 6_570, 6_530, 6_500]),
          indicator("fred.wrbwfrbl", "准备金余额", "billions_usd", [3_350, 3_320, 3_300, 3_280]),
          indicator("fred.wtregen", "TGA", "billions_usd", [720, 750, 780, 810]),
          indicator("fred.rrpontsyd", "隔夜逆回购", "billions_usd", [130, 110, 90, 70]),
        ],
      },
      funding: {
        indicators: [
          indicator("fred.sofr", "SOFR", "percent", [4.3, 4.32, 4.34, 4.35]),
          indicator("fred.iorb", "IORB", "percent", [4.4, 4.4, 4.4, 4.4]),
        ],
        sofr_minus_iorb_bp_history: historyRows([-10, -8, -6, -5]),
      },
    };
  }
  if (moduleId === "credit") {
    return {
      ...base,
      schema_version: "macro_credit_v5",
      module_id: "credit",
      cycle_dimensions: [
        {
          dimension_id: "spread_level_velocity",
          label: "利差水平与速度",
          state: "tightening",
          driver: "评级梯级近月平均走阔",
          conflicts: ["利差仍处低分位，但近月正在走阔"],
        },
        {
          dimension_id: "funding_cost",
          label: "绝对融资成本",
          state: "expensive",
          driver: "公司债绝对收益率处于实际历史高分位",
          conflicts: [],
        },
        {
          dimension_id: "credit_supply",
          label: "银行供给与需求",
          state: "neutral",
          driver: "银行供给与需求未形成一致压力",
          conflicts: [],
        },
        {
          dimension_id: "credit_quality",
          label: "实现信用质量",
          state: "stable",
          driver: "实现贷款质量未出现一致恶化",
          conflicts: [],
        },
      ],
      spread_ladder: {
        rows: [
          indicator("fred.bamlc0a0cm", "美国公司债 OAS", "percent", [0.72, 0.74, 0.77, 0.79]),
          indicator("fred.bamlc0a4cbbb", "BBB OAS", "percent", [1.02, 1.05, 1.08, 1.1]),
          indicator("fred.bamlh0a1hybb", "BB OAS", "percent", [1.8, 1.9, 2, 2.1]),
          indicator("fred.bamlh0a2hyb", "B OAS", "percent", [2.8, 2.95, 3.1, 3.2]),
          indicator("fred.bamlh0a3hyc", "CCC OAS", "percent", [7.4, 7.8, 8.1, 8.4]),
        ],
        tail_gap: 630,
        tail_gap_unit: "basis_points",
      },
      funding_costs: {
        corporate_yields: [
          indicator("fred.bamlc0a0cmey", "IG 有效收益率", "percent", [4.8, 4.9, 5, 5.1]),
          indicator("fred.bamlh0a0hym2ey", "HY 有效收益率", "percent", [7.2, 7.35, 7.5, 7.6]),
        ],
        reference_rates: [
          indicator("fred.effr", "EFFR", "percent", [4.33, 4.33, 4.33, 4.33]),
          indicator("fred.dgs10", "10Y Treasury", "percent", [4.1, 4.2, 4.3, 4.4]),
        ],
        comparisons: [
          { label: "IG yield − EFFR", value_bp: 77 },
          { label: "HY yield − EFFR", value_bp: 327 },
        ],
      },
      bank_lending: {
        indicators: [
          indicator("fred.drtscilm", "C&I 贷款标准", "percent", [5, 7, 8, 8.1]),
          indicator("fred.drsdcilm", "C&I 贷款需求", "percent", [-8, -7, -6, -5]),
          indicator("fred.sublpdrcsn", "CRE 贷款标准", "percent", [12, 13, 14, 15]),
          indicator("fred.sublpdrcdn", "CRE 贷款需求", "percent", [-20, -18, -17, -16]),
        ],
      },
      loan_quality: {
        indicators: [
          indicator("fred.drblacbs", "商业贷款逾期率", "percent", [1.2, 1.25, 1.3, 1.34]),
          indicator("fred.drcrelexfacbs", "CRE 逾期率", "percent", [1.1, 1.2, 1.35, 1.5]),
          indicator("fred.drcclacbs", "信用卡逾期率", "percent", [2.8, 2.9, 3, 3.1]),
        ],
      },
      confirmations: {
        etfs: [
          combinedAsset("LQD", "nasdaq.lqd.daily", "yfinance.lqd.intraday", [0.1, 0.4, 1.1]),
          combinedAsset("HYG", "nasdaq.hyg.daily", "yfinance.hyg.intraday", [-0.1, 0.2, 0.8]),
        ],
        positions: [],
      },
    };
  }
  if (moduleId === "volatility") {
    return {
      ...base,
      schema_version: "macro_volatility_v4",
      module_id: "volatility",
      term_structure: {
        spot_and_three_month: [
          indicator("fred.vixcls", "VIX", "index", [16, 17, 18, 18]),
          indicator("fred.vxvcls", "3M VIX", "index", [19, 19.5, 20, 20]),
        ],
        spread_history: historyRows([-3, -2.5, -2, -2]),
      },
      cross_asset_implied: {
        indicators: [
          indicator("fred.vxncls", "VXN", "index", [19, 20, 21, 22]),
          indicator("fred.gvzcls", "GVZ", "index", [14, 15, 15.5, 16]),
          indicator("fred.ovxcls", "OVX", "index", [28, 29, 30, 31]),
        ],
        normalized: [
          ...normalizedRows("VXN", [100, 105, 110, 116]),
          ...normalizedRows("GVZ", [100, 107, 111, 114]),
          ...normalizedRows("OVX", [100, 104, 107, 111]),
        ],
      },
    };
  }
  return {
    ...base,
    schema_version: "macro_cross_asset_v5",
    module_id: "cross_asset",
    assets: {
      benchmarks: [
        benchmark("S&P 500", "equity", "SPY", "nasdaq.spy.daily", [0.4, 1.2, 3.8]),
        benchmark("Treasury duration", "rates", "TLT", "nasdaq.tlt.daily", [-0.3, -1.4, -2.2]),
        benchmark("HY credit", "credit", "HYG", "nasdaq.hyg.daily", [0.1, 0.6, 1.4]),
        benchmark("Gold", "commodity", "GLD", "nasdaq.gld.daily", [0.2, 1.8, 4.5]),
        benchmark("Bitcoin", "crypto", "BTC", "binance.btcusdt.spot", [1.4, 5.5, 12]),
      ],
      proxies: [
        combinedAsset("SPY", "nasdaq.spy.daily", "yfinance.spy.intraday", [0.4, 1.2, 3.8]),
        combinedAsset("TLT", "nasdaq.tlt.daily", "yfinance.tlt.intraday", [-0.3, -1.4, -2.2]),
      ],
      normalized: [
        ...normalizedRows("SPY", [100, 102, 104, 106]),
        ...normalizedRows("TLT", [100, 99, 98, 97]),
        ...normalizedRows("HYG", [100, 100.5, 101, 101.5]),
        ...normalizedRows("GLD", [100, 102, 104, 105]),
        ...normalizedRows("BTC", [100, 106, 112, 118]),
      ],
    },
    correlations: [
      {
        left: "SPY",
        right: "TLT",
        correlation: -0.25,
        sample_count: 120,
        window: "up_to_120_daily_returns",
      },
      {
        left: "SPY",
        right: "HYG",
        correlation: 0.72,
        sample_count: 120,
        window: "up_to_120_daily_returns",
      },
      {
        left: "TLT",
        right: "HYG",
        correlation: -0.1,
        sample_count: 120,
        window: "up_to_120_daily_returns",
      },
    ],
    futures: {
      market: [combinedAsset("ES", "nasdaq.es.daily", "yfinance.es.intraday", [0.3, 1.1, 3])],
      positions: [
        {
          contract_code: "ES",
          contract_name: "E-mini S&P 500",
          report_date: "2026-07-21",
          leveraged_net_pct_oi: -3,
          asset_manager_net_pct_oi: 12,
          dealer_net_pct_oi: 0,
          source_url: "https://example.com/cftc",
        },
      ],
      vix_settlements: [
        {
          trade_date: "2026-07-25",
          contract_code: "VXQ6",
          settlement_price: 19.2,
          open_interest: 42_000,
          volume: 18_000,
          source_url: "https://example.com/cboe",
        },
      ],
    },
  };
}

function indicator(
  datasetId: string,
  label: string,
  unit: string,
  values: number[],
): Record<string, unknown> {
  return {
    dataset_id: datasetId,
    label,
    unit,
    latest_value: values.at(-1) ?? null,
    as_of: "2026-07-27",
    change_1w: values.length > 1 ? values.at(-1)! - values.at(-2)! : null,
    change_1m: values.length > 1 ? values.at(-1)! - values[0]! : null,
    sample_count: values.length,
    history_start: "2026-04-27",
    history_end: "2026-07-27",
    source_url: "https://example.com/source",
    history: historyRows(values),
  };
}

function historyRows(values: number[], key = "value"): Array<Record<string, unknown>> {
  const dates = ["2026-04-27", "2026-05-27", "2026-06-27", "2026-07-27"];
  return values.map((value, index) => ({
    date: dates[index] ?? `2026-07-${index + 1}`,
    [key]: value,
  }));
}

function curveSnapshots(
  key: "yield_pct" | "breakeven_pct",
  currentValues: number[],
): Array<Record<string, unknown>> {
  const tenors = [
    ["3M", 0.25],
    ["2Y", 2],
    ["5Y", 5],
    ["10Y", 10],
    ["30Y", 30],
  ] as const;
  return [
    ["current", "2026-07-27", 0],
    ["1w", "2026-07-20", -0.05],
    ["1m", "2026-06-27", -0.1],
    ["3m", "2026-04-27", -0.15],
  ].map(([window, asOf, offset]) => ({
    window,
    as_of: asOf,
    points: tenors.map(([tenor, years], index) => ({
      tenor,
      years,
      [key]: currentValues[index]! + Number(offset),
    })),
  }));
}

function release(
  datasetId: string,
  label: string,
  actual: number,
  estimate: number,
  prior: number,
): Record<string, unknown> {
  return {
    dataset_id: datasetId,
    label,
    reference_period: "2026-06",
    actual_value: actual,
    estimate_value: estimate,
    prior_value: prior,
    revised_prior_value: prior + 0.1,
    surprise: actual - estimate,
    revision: 0.1,
    unit: "percent",
    source_url: "https://example.com/release",
  };
}

function combinedAsset(
  symbol: string,
  historyDatasetId: string,
  intradayDatasetId: string,
  changes: [number, number, number],
): Record<string, unknown> {
  return {
    symbol,
    label: symbol,
    asset_class: "market",
    selection_policy: "decision_primary_only_no_fallback",
    sources: {
      identity_policy: "separate_source_facts_no_blend",
      decision_primary: assetFact(symbol, historyDatasetId, changes),
      intraday_proxy: assetFact(symbol, intradayDatasetId, changes),
      history: assetFact(symbol, historyDatasetId, changes),
    },
  };
}

function benchmark(
  label: string,
  assetClass: string,
  symbol: string,
  datasetId: string,
  changes: [number, number, number],
): Record<string, unknown> {
  return {
    label,
    symbol,
    asset_class: assetClass,
    dataset_id: datasetId,
    evidence_kind: "tradable_proxy_reference",
    selection_policy: "decision_primary_only_no_fallback",
    sources: {
      identity_policy: "separate_source_facts_no_blend",
      decision_primary: assetFact(symbol, datasetId, changes),
      intraday_proxy: null,
      history: assetFact(symbol, datasetId, changes),
    },
  };
}

function assetFact(
  symbol: string,
  datasetId: string,
  changes: [number, number, number],
): Record<string, unknown> {
  return {
    dataset_id: datasetId,
    symbol,
    label: symbol,
    latest_value: 100,
    as_of: "2026-07-27",
    market_time_ms: NOW,
    change_1d_pct: changes[0],
    change_1w_pct: changes[1],
    change_1m_pct: changes[2],
    source_url: "https://example.com/market",
  };
}

function normalizedRows(symbol: string, values: number[]): Array<Record<string, unknown>> {
  return historyRows(values).map((row) => ({
    symbol,
    date: row.date,
    normalized_value: row.value,
  }));
}

export function macroResearchFixture(
  state: MacroThesisDetailReadData["state"] = "current",
): MacroThesisDetailReadData {
  const thesis = state === "current" || state === "historical" ? macroThesisFixture() : null;
  return {
    state,
    requested_session_date: "2026-07-27",
    current_session_date: state === "historical" ? "2026-07-28" : "2026-07-27",
    thesis,
    live_delta: thesis ? macroOverviewFixture().live_delta : null,
    outcome_replay: thesis ? macroOverviewFixture().outcome_replay : null,
    run:
      state === "missing"
        ? null
        : {
            session_date: "2026-07-27",
            status:
              state === "failed"
                ? "config_error"
                : state === "generating"
                  ? "running"
                  : "published",
            evidence_pack_id: "mep_fixture",
            attempt_count: 1,
            max_attempts: 2,
            error_code: state === "failed" ? "macro_thesis_configuration_error" : null,
            error_message: state === "failed" ? "service unavailable" : null,
            updated_at_ms: NOW,
          },
    history: [
      {
        publication_id: "mth_fixture",
        session_date: "2026-07-27",
        cutoff_ms: NOW - 3_600_000,
        published_at_ms: NOW - 3_000_000,
        title: "实际利率上行主导短期风险资产定价",
        stance: "call",
        confidence: "medium",
        horizon: "1w",
      },
    ],
  };
}

function moduleBase(moduleId: MacroModuleId) {
  return {
    label: moduleLabel(moduleId),
    status: {
      coverage: {
        state: "complete" as const,
        expected_capabilities: 1,
        available_capabilities: 1,
        capabilities: [
          {
            capability_id: `${moduleId}.core`,
            label: "核心事实",
            requirement: "required" as const,
            state: "available" as const,
            dataset_ids: ["fixture.dataset"],
            reason: null,
          },
        ],
      },
      current_health: {
        state: "current" as const,
        current_datasets: 1,
        tracked_datasets: 1,
        as_of_ms: NOW,
        groups: [
          {
            group_id: "fixture",
            label: "Fixture",
            current_health: "current" as const,
            market_state: "not_applicable" as const,
            source_state: "healthy" as const,
            current_datasets: 1,
            tracked_datasets: 1,
          },
        ],
      },
      history_depth: {
        state: "complete" as const,
        complete_datasets: 1,
        tracked_datasets: 1,
      },
    },
    latest_fact_at_ms: NOW - 600_000,
    summary: {
      headline: `${moduleLabel(moduleId)}当前事实`,
      interpretation: "模块只呈现可复算事实，主线判断由冻结 Macro Thesis 发布。",
      top_changes: [
        {
          dataset_id: "fixture.dataset",
          concept_id: `${moduleId}.core`,
          source_role: "decision_primary",
          label: "核心变化",
          as_of: "2026-07-27",
          value: 4.3,
          unit: "percent",
          cadence: "daily",
          metrics: { change_1w_bp: 30, change_1m_bp: 42 },
          metric_unit: "basis_points",
          primary_change: 30,
          importance_rank: 1,
          importance_factors: {
            standardized_magnitude: 1.2,
            surprise_magnitude: 0,
            revision_magnitude: 0,
            decision_relevance: 2,
            trust_tier: "official" as const,
            fact_clock_ms: NOW - 600_000,
          },
          importance_explanation: "周变化幅度与当前健康共同决定排序。",
          source_url: "https://example.com/source",
        },
      ],
    },
    contradictions: ["信用尚未完全确认。"],
    falsifiers: ["若核心指标显著反向则主线失效。"],
    next_checkpoints: [
      {
        dataset_id: "fixture.dataset",
        label: "下一次官方更新",
        current_health: "current",
        history_depth: "complete",
        next_check: "按数据自然发布频率检查",
      },
    ],
    evidence: {
      dataset_states: [
        {
          dataset_id: "fixture.dataset",
          concept_id: `${moduleId}.core`,
          source_role: "decision_primary",
          label: "核心事实",
          current_health: "current" as const,
          history_depth: "complete" as const,
          market_state: "not_applicable" as const,
          source_state: "healthy" as const,
          current_reason: "latest_fact_within_natural_cadence",
          history_reason: "history_requirement_satisfied",
          critical: true,
          trust_tier: "official" as const,
          source_url: "https://example.com/source",
          latest_reference: "2026-07-27",
          latest_received_at_ms: NOW - 600_000,
          last_market_at_ms: null,
          next_open_ms: null,
          health_group: "official_state",
        },
      ],
      latest_facts: [
        {
          dataset_id: "fixture.dataset",
          series_id: "FIXTURE",
          fact_ref: "fact:fixture",
          reference: "2026-07-27",
          value: 4.3,
          unit: "percent",
          published_at_ms: NOW - 900_000,
          received_at_ms: NOW - 600_000,
          source_url: "https://example.com/source",
        },
      ],
      reconciliation_receipts: [],
    },
  };
}

function moduleLabel(moduleId: MacroModuleId): string {
  return {
    credit: "信用市场",
    cross_asset: "大类资产与期货",
    economy_inflation: "经济与通胀",
    liquidity_funding: "流动性与融资",
    rates_fed: "利率与美联储",
    volatility: "波动率",
  }[moduleId];
}

function modulePath(moduleId: MacroModuleId): string {
  return {
    credit: "/macro/credit",
    cross_asset: "/macro/cross-asset",
    economy_inflation: "/macro/economy-inflation",
    liquidity_funding: "/macro/liquidity-funding",
    rates_fed: "/macro/rates-fed",
    volatility: "/macro/volatility",
  }[moduleId];
}
