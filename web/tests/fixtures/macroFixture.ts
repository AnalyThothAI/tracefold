import type {
  MacroAssetRow,
  MacroIndicator,
  MacroModuleId,
  MacroOverviewReadData,
  MacroResearchReadData,
  MacroTypedModuleReadData,
} from "@features/macro";

const NOW = 1_753_000_000_000;

export function macroOverviewFixture(): MacroOverviewReadData {
  const moduleIds: MacroModuleId[] = [
    "rates_fed",
    "economy_inflation",
    "liquidity_funding",
    "credit",
    "volatility",
    "cross_asset",
  ];
  return {
    schema_version: "macro_overview_v3",
    read_at_ms: NOW,
    judgment_cutoff_ms: NOW - 3_600_000,
    latest_fact_at_ms: NOW - 600_000,
    coverage_state: "partial",
    data_health_state: "current",
    judgment_state: "current",
    judgment_status: {
      session_date: "2026-07-27",
      judgment_cutoff_ms: NOW - 3_600_000,
      state: "current",
      reason_code: "published",
      details: {},
      attempted_at_ms: NOW - 3_000_000,
    },
    daily_judgment: {
      schema_version: "macro_daily_judgment_v2",
      session_date: "2026-07-27",
      judgment_cutoff_ms: NOW - 3_600_000,
      latest_fact_at_ms: NOW - 600_000,
      overall_state: "分项压力、尚未共振",
      dominant_pressures: [{ dimension: "credit", state: "tightening", driver: "高收益利差走阔" }],
      top_3_changes: [],
      dimensions: {
        growth: { state: "stable", driver: "增长证据未形成一致方向" },
        inflation: { state: "sticky", driver: "核心通胀仍高于目标" },
        policy: { state: "neutral", driver: "短端利率处于区间" },
        liquidity: { state: "neutral", driver: "流动性分项方向不一致" },
        credit: { state: "tightening", driver: "高收益利差走阔" },
        volatility: { state: "normal", driver: "VIX处于常态区间" },
      },
      module_judgments: [],
      asset_directions: Object.fromEntries(
        ["SPY", "QQQ", "IWM", "TLT", "IEF", "LQD", "HYG", "UUP", "GLD", "USO", "BTC", "VIX"].map(
          (asset) => [
            asset,
            {
              "1w": asset === "SPY" ? "up" : "range",
              "1m": asset === "SPY" ? "up" : "range",
              drivers: [],
              conflicts: [],
              invalidation: "方向反转",
              confidence: asset === "BTC" || asset === "VIX" ? "medium" : "low",
              dataset_id: `fixture.${asset.toLowerCase()}`,
            },
          ],
        ),
      ),
      contradictions: [],
      falsifiers: [{ module_id: "rates_fed", items: ["2年期收益率重新显著上行"] }],
      next_checkpoints: [],
      gaps: [],
      citations: [],
    },
    modules: moduleIds.map((moduleId) => {
      const module = macroModuleFixture(moduleId);
      return {
        module_id: moduleId,
        label: module.label,
        coverage_state: module.status.coverage.state,
        data_health_state: module.status.data_health.state,
        judgment_state: module.status.judgment.state,
        latest_fact_at_ms: module.latest_fact_at_ms,
        summary: module.summary,
        top_changes: module.summary.top_changes,
        coverage_gap_count: module.status.coverage.capabilities.filter(
          (item) => item.state !== "available",
        ).length,
        health_gap_count: 0,
        href: modulePath(moduleId),
      };
    }),
    changes_since_judgment: [],
    research: {
      state: "current",
      session_date: "2026-07-26",
      evidence_pack_id: "mep_fixture",
      market_cutoff_ms: NOW - 86_400_000,
      title: "完成交易日宏观研究",
      executive_summary: "冻结 Evidence Pack 内的利率、信用与跨资产证据仍有分歧。",
      reviewer_disposition: "pass",
      href: "/macro/research",
    },
  };
}

export function macroModuleFixture(moduleId: MacroModuleId): MacroTypedModuleReadData {
  const base = moduleBase(moduleId);
  if (moduleId === "rates_fed") {
    return {
      ...base,
      schema_version: "macro_rates_fed_v2",
      module_id: "rates_fed",
      curve: {
        nominal_snapshots: [
          curveSnapshot("current", "2026-07-24", [4.3, 4.1, 4.4, 4.9]),
          curveSnapshot("1w", "2026-07-17", [4.35, 4.0, 4.2, 4.7]),
        ],
        real_snapshots: [],
        breakeven_snapshots: [],
        spreads: {
          "2s10s": [{ date: "2026-07-24", value_bp: 30 }],
          "3m10s": [{ date: "2026-07-24", value_bp: 10 }],
          "5s30s": [{ date: "2026-07-24", value_bp: 50 }],
        },
        classification: {
          state: "bear_steepening",
          label: "熊市陡峭化",
          formula_version: "level_slope_curvature_classification_v2",
          inputs: {
            level_change_bp: 12,
            slope_change_bp: 10,
            curvature_change_bp: 2,
          },
        },
      },
      policy_pricing: {
        rates: [indicator("fred.effr", "有效联邦基金利率", 4.33)],
        cme_policy_probabilities: {
          state: "licensed_unavailable",
          reason: "licensed_contract_facts_not_configured",
        },
      },
      fed: {
        institutional_stance: {
          state: "no_call",
          direction: "no_call",
          change_from_prior: "no_call",
          reason: "immutable_document_analysis_not_published",
        },
        officials_distribution: {
          state: "no_call",
          window_days: 90,
          as_of: "2026-07-24",
          hawkish: 0,
          neutral: 0,
          dovish: 0,
          mixed: 0,
          not_policy_signal: 0,
          uncertain: 0,
          analyzed_events: 0,
        },
        timeline: [
          {
            document_id: "macrodoc_fixture",
            document_type: "statement",
            title: "Federal Reserve issues FOMC statement",
            effective_date: "2026-07-24",
            published_at_ms: NOW - 600_000,
            source_url: "https://www.federalreserve.gov/",
            speaker_name: null,
            official_id: null,
            role_title: null,
            fomc_voter: null,
            analysis: {
              state: "not_analyzed",
              policy_relevance: "unknown",
              stance: "no_call",
              confidence: null,
              change_from_prior: null,
              evidence: [],
              analysis_id: null,
              model_name: null,
              prompt_version: null,
              reviewer_disposition: null,
            },
          },
        ],
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
      ...base,
      schema_version: "macro_economy_inflation_v2",
      module_id: "economy_inflation",
      inflation: {
        indicators: [indicator("fred.cpiaucsl", "消费者价格指数", 320)],
        official_releases: [],
      },
      labor: { indicators: [indicator("fred.unrate", "失业率", 4.1)], official_releases: [] },
      growth: { indicators: [indicator("fred.gdpc1", "实际GDP", 24_000)] },
    };
  }
  if (moduleId === "liquidity_funding") {
    return {
      ...base,
      schema_version: "macro_liquidity_funding_v2",
      module_id: "liquidity_funding",
      balance_sheet: { indicators: [indicator("fred.walcl", "美联储总资产", 6_500_000)] },
      funding: { indicators: [indicator("fred.sofr", "SOFR", 4.32)] },
    };
  }
  if (moduleId === "credit") {
    return {
      ...base,
      schema_version: "macro_credit_v3",
      module_id: "credit",
      cycle_dimensions: [
        {
          dimension_id: "spread_level_velocity",
          label: "利差水平与速度",
          state: "tightening",
          driver: "评级梯级近月平均走阔",
          evidence_dataset_ids: ["fred.bamlc0a0cm", "fred.bamlh0a3hyc"],
          conflicts: [],
        },
        {
          dimension_id: "funding_cost",
          label: "绝对融资成本",
          state: "expensive",
          driver: "公司债绝对收益率处于实际历史高分位",
          evidence_dataset_ids: ["fred.bamlc0a0cmey"],
          conflicts: [],
        },
        {
          dimension_id: "credit_supply",
          label: "银行供给与需求",
          state: "neutral",
          driver: "银行供给与需求未形成一致压力",
          evidence_dataset_ids: ["fred.drtscilm", "fred.drsdcilm"],
          conflicts: [],
        },
        {
          dimension_id: "credit_quality",
          label: "实现信用质量",
          state: "stable",
          driver: "实现贷款质量未出现一致恶化",
          evidence_dataset_ids: ["fred.drblacbs"],
          conflicts: [],
        },
        {
          dimension_id: "market_liquidity",
          label: "市场流动性",
          state: "licensed_unavailable",
          driver: "TRACE逐笔与ETF NAV溢折价在获得合规数据前不可用",
          evidence_dataset_ids: ["licensed.credit.trace_nav"],
          conflicts: [],
        },
      ],
      spread_ladder: {
        rows: [
          indicator("fred.bamlc0a0cm", "IG OAS", 0.79),
          indicator("fred.bamlc0a4cbbb", "BBB OAS", 0.98),
          indicator("fred.bamlh0a1hybb", "BB OAS", 1.75),
          indicator("fred.bamlh0a2hyb", "B OAS", 3.2),
          indicator("fred.bamlh0a3hyc", "CCC OAS", 9.91),
        ],
        tail_gap: 816,
        tail_gap_unit: "basis_points",
      },
      funding_costs: {
        corporate_yields: [indicator("fred.bamlc0a0cmey", "IG effective yield", 5.1)],
        reference_rates: [indicator("fred.effr", "EFFR", 4.33)],
        comparisons: [
          {
            label: "IG yield − EFFR",
            corporate_dataset_id: "fred.bamlc0a0cmey",
            reference_dataset_id: "fred.effr",
            as_of: "2026-07-24",
            value_bp: 77,
            formula_version: "matched_rate_difference_v1",
            input_fact_ids: ["fact:ig", "fact:effr"],
          },
        ],
      },
      bank_lending: { indicators: [indicator("fred.drtscilm", "C&I标准", 8.1)] },
      loan_quality: { indicators: [indicator("fred.drblacbs", "商业贷款逾期率", 1.34)] },
      confirmations: {
        etfs: [asset("LQD", "yfinance.lqd.market")],
        positions: [],
        trace_nav: {
          state: "licensed_unavailable",
          reason: "licensed_security_level_facts_not_configured",
        },
      },
    };
  }
  if (moduleId === "volatility") {
    return {
      ...base,
      schema_version: "macro_volatility_v2",
      module_id: "volatility",
      term_structure: {
        spot_and_three_month: [indicator("fred.vixcls", "VIX", 18)],
        spread_history: [],
      },
      cross_asset_implied: { indicators: [indicator("fred.gvzcls", "GVZ", 16)] },
    };
  }
  return {
    ...base,
    schema_version: "macro_cross_asset_v3",
    module_id: "cross_asset",
    assets: {
      benchmarks: [
        {
          label: "WTI Cushing spot",
          asset_class: "commodity",
          dataset_id: "fred.dcoilwtico",
          evidence_kind: "official_benchmark",
          latest_value: 78,
          unit: "usd_per_barrel",
          as_of: "2026-07-24",
          change_1w: 2,
          change_1m: 4,
        },
      ],
      proxies: [
        asset("SPY", "yfinance.spy.market"),
        asset("QQQ", "yfinance.qqq.market"),
        asset("IWM", "yfinance.iwm.market"),
        asset("TLT", "yfinance.tlt.market"),
        asset("IEF", "yfinance.ief.market"),
        asset("LQD", "yfinance.lqd.market"),
        asset("HYG", "yfinance.hyg.market"),
        asset("UUP", "yfinance.dxy.market"),
        asset("GLD", "yfinance.gld.market"),
        asset("USO", "yfinance.uso.market"),
      ],
      normalized: [
        { symbol: "SPY", date: "2026-07-17", normalized_value: 100 },
        { symbol: "SPY", date: "2026-07-24", normalized_value: 102 },
      ],
    },
    correlations: [
      { left: "SPY", right: "TLT", correlation: -0.25, sample_count: 120, window: "daily" },
    ],
    futures: {
      market: [asset("ES", "yfinance.es_future.market")],
      vix_settlements: [{ contract_code: "VX/U6", settlement_price: 19.2 }],
      positions: [],
    },
  };
}

function moduleBase(moduleId: MacroModuleId) {
  const labels: Record<MacroModuleId, string> = {
    rates_fed: "利率与美联储",
    economy_inflation: "经济与通胀",
    liquidity_funding: "流动性与融资",
    credit: "信用市场",
    volatility: "波动率",
    cross_asset: "大类资产与期货",
  };
  return {
    label: labels[moduleId],
    status: {
      coverage: {
        state: "partial" as const,
        expected_capabilities: 4,
        available_capabilities: 3,
        capabilities: [
          {
            capability_id: `${moduleId}.core`,
            label: "核心事实",
            requirement: "required" as const,
            state: "available" as const,
            dataset_ids: ["fixture.dataset"],
            reason: null,
          },
          {
            capability_id: `${moduleId}.licensed`,
            label: "授权数据",
            requirement: "licensed_unavailable" as const,
            state: "licensed_unavailable" as const,
            dataset_ids: ["fixture.licensed"],
            reason: "licensed_contract_facts_not_configured",
          },
        ],
      },
      data_health: {
        state: "current" as const,
        current_datasets: 1,
        tracked_datasets: 1,
        as_of_ms: NOW,
      },
      judgment: { state: "current" as const, cutoff_ms: NOW - 3_600_000 },
    },
    latest_fact_at_ms: NOW - 600_000,
    summary: {
      headline: `${labels[moduleId]}当前证据`,
      interpretation: "实时页面只呈现可复算事实；判断只由冻结 Evidence Pack 发布。",
      top_changes: [
        {
          dataset_id: "fixture.dataset",
          label: labels[moduleId],
          as_of: "2026-07-24",
          value: 4.32,
          unit: "percent",
          change_1w: 0.12,
          change_1m: -0.08,
          magnitude: 0.12,
          source_url: "https://example.com/official",
        },
      ],
    },
    contradictions: ["短窗口与中窗口方向不一致。"],
    falsifiers: ["对应窗口事实方向反转。"],
    next_checkpoints: [{ label: labels[moduleId], next_check: "按数据时钟检查" }],
    evidence: { dataset_states: [], latest_facts: [] },
  };
}

function indicator(datasetId: string, label: string, value: number): MacroIndicator {
  return {
    dataset_id: datasetId,
    label,
    latest_value: value,
    unit: "percent",
    as_of: "2026-07-24",
    change_1w: 0.1,
    change_1m: -0.2,
    sample_count: 250,
    history_start: "2025-07-24",
    history_end: "2026-07-24",
    source_url: "https://example.com/official",
    percentile: 42,
    history: [{ date: "2026-07-24", value }],
  };
}

function asset(symbol: string, datasetId: string): MacroAssetRow {
  return {
    dataset_id: datasetId,
    symbol,
    label: `${symbol} ETF`,
    instrument_type: "etf",
    asset_class: "fixture",
    latest_value: 100,
    unit: "price",
    as_of: "2026-07-24",
    market_time_ms: NOW - 600_000,
    change_1d_pct: 0.2,
    change_1w_pct: 1.1,
    change_1m_pct: 2.4,
    trust_tier: "untrusted_proxy",
    source_url: "https://example.com/official",
  };
}

function curveSnapshot(window: "current" | "1w", asOf: string, values: number[]) {
  return {
    window,
    as_of: asOf,
    points: ["3M", "2Y", "10Y", "30Y"].map((tenor, index) => ({
      tenor,
      years: [0.25, 2, 10, 30][index],
      yield_pct: values[index],
    })),
  };
}

export function macroResearchFixture(
  state: MacroResearchReadData["state"] = "current",
): MacroResearchReadData {
  const hasPublication = state === "current" || state === "historical";
  return {
    state,
    requested_session_date: "2026-07-26",
    current_session_date: "2026-07-26",
    publication: hasPublication
      ? {
          schema_version: "macro_research_artifact_v3",
          session_date: "2026-07-26",
          market_cutoff_ms: NOW - 86_400_000,
          evidence_pack_id: "mep_fixture",
          title: "完成交易日宏观研究",
          executive_summary: "利率与信用定价仍有分歧。",
          sections: [
            {
              section_id: "rates",
              title: "利率",
              body_markdown: "曲线仍然偏紧。",
              citation_ids: ["c1"],
            },
          ],
          evidence_gaps: [],
          citations: [
            {
              citation_id: "c1",
              source_type: "module",
              source_ref: "macro-pack:mep_fixture:module:rates_fed",
              source_label: "Evidence Pack / 利率与美联储",
              observed_at: "2026-07-26",
              published_at_ms: NOW - 90_000_000,
              available_at_ms: NOW - 90_000_000,
              source_url: null,
              lineage: { evidence_pack_id: "mep_fixture" },
            },
          ],
          reviewer_disposition: "pass",
          reviewer_notes: ["已复核引用闭合。"],
          audit: { workflow_version: "deepagents_macro_research_v4_evidence_pack" },
          published_at_ms: NOW - 80_000_000,
        }
      : null,
    run:
      state === "missing"
        ? null
        : {
            session_date: "2026-07-26",
            evidence_pack_id: "mep_fixture",
            status: hasPublication ? "published" : state === "failed" ? "failed" : "running",
            attempt_count: 1,
            max_attempts: 3,
            last_error: state === "failed" ? "service unavailable" : null,
            updated_at_ms: NOW,
          },
  };
}

function modulePath(moduleId: MacroModuleId) {
  return {
    rates_fed: "/macro/rates-fed",
    economy_inflation: "/macro/economy-inflation",
    liquidity_funding: "/macro/liquidity-funding",
    credit: "/macro/credit",
    volatility: "/macro/volatility",
    cross_asset: "/macro/cross-asset",
  }[moduleId];
}
