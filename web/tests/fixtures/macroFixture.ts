import type {
  MacroModuleId,
  MacroModuleReadData,
  MacroOverviewReadData,
  MacroResearchReadData,
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
    schema_version: "macro_overview_v1",
    read_at_ms: NOW,
    judgment_cutoff_ms: NOW - 3_600_000,
    latest_fact_at_ms: NOW - 600_000,
    overall_readiness: "degraded",
    daily_judgment: {
      schema_version: "macro_daily_judgment_v1",
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
        ["SPY", "TLT", "HYG", "DXY", "GLD", "USO", "BTC", "VIX"].map((asset) => [
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
        ]),
      ),
      contradictions: [],
      falsifiers: [{ module_id: "rates_fed", items: ["2年期收益率重新显著上行"] }],
      next_checkpoints: [
        {
          module_id: "economy_inflation",
          dataset_id: "fred.cpiaucsl",
          label: "美国CPI",
          next_check: "按月度发布时钟检查",
        },
      ],
      gaps: [
        {
          module_id: "cross_asset",
          dataset_id: "cme.rates.futures.curves",
          label: "CME利率期货曲线",
          reason: "licensed_data_not_configured",
        },
      ],
      citations: [],
    },
    modules: moduleIds.map((moduleId) => {
      const module = macroModuleFixture(moduleId);
      return {
        module_id: moduleId,
        label: module.label,
        readiness: module.readiness,
        latest_fact_at_ms: module.latest_fact_at_ms,
        current_state: module.current_state,
        top_changes: module.top_changes,
        gap_count: module.gaps.length,
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

export function macroModuleFixture(moduleId: MacroModuleId): MacroModuleReadData {
  const labels: Record<MacroModuleId, string> = {
    rates_fed: "利率与美联储",
    economy_inflation: "经济与通胀",
    liquidity_funding: "流动性与融资",
    credit: "信用市场",
    volatility: "波动率",
    cross_asset: "大类资产与期货",
  };
  const datasetId = moduleId === "cross_asset" ? "cboe.cfe.vx.settlement" : `fred.${moduleId}`;
  return {
    schema_version: "macro_module_v1",
    module_id: moduleId,
    label: labels[moduleId],
    readiness: moduleId === "cross_asset" ? "degraded" : "ready",
    judgment_cutoff_ms: NOW - 3_600_000,
    latest_fact_at_ms: NOW - 600_000,
    current_state: {
      headline: `${labels[moduleId]}当前证据`,
      dominant_change: null,
      feature_count: 2,
      interpretation: "仅陈述可复算事实；宏观判断见每日判断。",
    },
    top_changes: [
      {
        dataset_id: datasetId,
        label: moduleId === "cross_asset" ? "CFE VIX期货官方结算" : labels[moduleId],
        as_of: "2026-07-26",
        value: 4.32,
        unit: "percent",
        short_window: "5交易日",
        short_change: 0.12,
        medium_window: "21交易日",
        medium_change: -0.08,
        magnitude: 0.12,
        source_url: "https://example.com/official",
      },
    ],
    features: [],
    charts: [
      {
        chart_id: "fixture",
        title: `${labels[moduleId]}历史`,
        series: [datasetId],
        points: [
          { dataset_id: datasetId, label: labels[moduleId], x: "2026-07-24", y: 4.2, unit: "percent" },
          { dataset_id: datasetId, label: labels[moduleId], x: "2026-07-25", y: 4.25, unit: "percent" },
          { dataset_id: datasetId, label: labels[moduleId], x: "2026-07-26", y: 4.32, unit: "percent" },
        ],
      },
    ],
    contradictions: ["短窗口与中窗口方向不一致。"],
    falsifiers: ["对应窗口事实方向反转。"],
    next_checkpoints: [{ dataset_id: datasetId, label: labels[moduleId], next_check: "按数据时钟检查" }],
    gaps:
      moduleId === "cross_asset"
        ? [
            {
              dataset_id: "cme.rates.futures.curves",
              label: "CME利率期货曲线",
              state: "unavailable",
              reason: "licensed_data_not_configured",
            },
          ]
        : [],
    dataset_states: [
      {
        dataset_id: datasetId,
        label: labels[moduleId],
        state: "current",
        reason: "within_freshness_budget",
        critical: true,
        trust_tier: "official",
        source_url: "https://example.com/official",
        latest_reference: "2026-07-26",
        latest_received_at_ms: NOW - 600_000,
      },
    ],
    raw_evidence: [
      {
        dataset_id: datasetId,
        label: labels[moduleId],
        fact_ref: "fact_fixture",
        reference: "2026-07-26",
        value: 4.32,
        unit: "percent",
        published_at_ms: NOW - 900_000,
        received_at_ms: NOW - 600_000,
        source_url: "https://example.com/official",
      },
    ],
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
