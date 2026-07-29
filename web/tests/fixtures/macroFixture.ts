import type {
  MacroCondition,
  MacroModuleId,
  MacroOverviewReadData,
  MacroReason,
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
type CrossAssetFixture = Extract<MacroTypedModuleReadData, { module_id: "cross_asset" }>;
type RatesFedFixture = Extract<MacroTypedModuleReadData, { module_id: "rates_fed" }>;
type EconomyInflationFixture = Extract<
  MacroTypedModuleReadData,
  { module_id: "economy_inflation" }
>;
type CrossAssetReturnRowFixture = CrossAssetFixture["assets"]["return_matrix"][number];
type CrossAssetSourceFixture = CrossAssetReturnRowFixture["latest_source"];
type CrossAssetNormalizedGroupFixture = CrossAssetFixture["assets"]["normalized_groups"][number];
type CrossAssetSourceIdentityFixture = CrossAssetFixture["assets"]["source_identity"][number];
type IndicatorFixture = RatesFedFixture["policy_pricing"]["rates"][number];
type HistoryPointFixture = IndicatorFixture["history"][number];
type CurveSpreadPointFixture = RatesFedFixture["curve"]["spreads"]["2s10s"][number];
type CurveYieldSnapshotFixture = RatesFedFixture["curve"]["nominal_snapshots"][number];
type CurveBreakevenSnapshotFixture = RatesFedFixture["curve"]["breakeven_snapshots"][number];
type ReleaseFixture = EconomyInflationFixture["inflation"]["official_releases"][number];
type MarketFactFixture = Extract<
  NonNullable<CrossAssetSourceFixture["fact"]>,
  { market_time_ms: number }
>;
type ModuleBaseFixture = Omit<
  RatesFedFixture,
  "schema_version" | "module_id" | "curve" | "policy_pricing" | "fed" | "positioning"
>;

function macroReason({
  code,
  message,
  impact = "none",
  nextAction = null,
  nextCheckAtMs = null,
  affectedDatasetIds = [],
  affectedClaimIds = [],
  retryable,
  recovery,
}: {
  code: string;
  message: string;
  impact?: MacroReason["impact"];
  nextAction?: string | null;
  nextCheckAtMs?: number | null;
  affectedDatasetIds?: string[];
  affectedClaimIds?: string[];
  retryable?: boolean;
  recovery?: MacroReason["recovery"];
}): MacroReason {
  return {
    code,
    message,
    impact,
    affected_dataset_ids: affectedDatasetIds,
    affected_claim_ids: affectedClaimIds,
    retryable: retryable ?? nextAction != null,
    recovery: recovery ?? (nextAction ? "automatic" : "none"),
    next_action: nextAction,
    next_check_at_ms: nextCheckAtMs,
  };
}

function mainlineFalsifierFixture(conditionId: string): MacroCondition {
  return {
    condition_id: conditionId,
    module_id: "rates_fed",
    dataset_id: "fred.dgs2",
    metric_name: "change_1w_bp",
    operator: "lte",
    threshold: -25,
    effect: "invalidation_triggered",
    rationale: "2Y 收益率显著反向时，该方向判断失效。",
  };
}

export function macroOverviewFixture(): MacroOverviewReadData {
  const thesis = macroThesisFixture();
  return {
    schema_version: "macro_overview_v7",
    read_at_ms: NOW,
    transport: {
      state: "current",
      last_successful_read_at_ms: NOW,
      reason: null,
    },
    session_date: "2026-07-27",
    displayed_session_date: "2026-07-27",
    cutoff_ms: NOW - 3_600_000,
    latest_fact_at_ms: NOW - 600_000,
    thesis_state: "published",
    thesis_reason: null,
    thesis,
    run: {
      session_date: "2026-07-27",
      status: "published",
      evidence_pack_id: thesis.evidence_pack_id,
      attempt_count: 1,
      max_attempts: 2,
      error_code: null,
      reason: null,
      updated_at_ms: NOW,
    },
    fallback: {
      state: "none",
      reason: macroReason({
        code: "requested_publication_available",
        message: "请求交易日主线已发布。",
      }),
      publication_id: null,
      session_date: null,
      cutoff_ms: null,
    },
    mainline_presentation: macroMainlinePresentationFixture(thesis),
    asset_presentation: macroAssetPresentationFixture(thesis),
    claim_presentation: macroClaimPresentationFixture(thesis),
    alternative_presentation: macroAlternativePresentationFixture(thesis),
    live_delta: macroLiveDeltaFixture(thesis),
    outcome_replay: macroOutcomeReplayFixture(thesis),
    modules: MODULE_IDS.map((moduleId) => {
      const module = macroModuleFixture(moduleId);
      const role = thesis.module_assessments.find((item) => item.module_id === moduleId)!;
      return {
        module_id: moduleId,
        label: module.label,
        availability: "available",
        reason: null,
        role: role.role,
        coverage_state: module.status.coverage.state,
        current_health_state: module.status.current_health.state,
        history_depth_state: module.status.history_depth.state,
        backfill_execution: module.status.backfill_execution,
        latest_fact_at_ms: module.latest_fact_at_ms,
        summary: module.summary,
        coverage_gap_count: 0,
        current_health_gap_count: 0,
        history_gap_count: 0,
        href: modulePath(moduleId),
        thesis_context: module.thesis_context,
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
    assets: ASSETS.map((symbol, index) => ({
      symbol,
      momentum: {
        symbol,
        momentum_1w: index >= 8 ? "insufficient" : symbol === "QQQ" ? "down" : "up",
        momentum_1m: index >= 8 ? "insufficient" : symbol === "QQQ" ? "down" : "up",
        return_1w_pct: index >= 8 ? null : symbol === "QQQ" ? -1.2 : 0.3,
        return_1m_pct: index >= 8 ? null : symbol === "QQQ" ? -2.1 : 0.8,
        source_dataset_id: index >= 8 ? null : `fixture.${symbol.toLowerCase()}`,
        as_of: "2026-07-27",
      },
      outlook_1w: {
        horizon: "1w",
        direction: index < 4 ? "bearish" : "no_call",
        causal_channel:
          index < 4 ? "实际利率上行经贴现率压制风险偏好。" : "等待利率与信用共同确认。",
        supporting_evidence_refs: [moduleRef("rates_fed")],
        conflicting_evidence_refs: [moduleRef("credit")],
        confirmation_triggers:
          index >= 4 && index < 8
            ? [
                {
                  condition_id: `confirm-${symbol.toLowerCase()}-1w`,
                  module_id: "credit",
                  dataset_id: "fred.bamlh0a0hym2",
                  metric_name: "change_1w_bp",
                  operator: "gte",
                  threshold: 20,
                  effect: "confirming",
                  rationale: "信用利差走阔后再形成方向判断。",
                },
              ]
            : [],
        falsifiers:
          index < 4 ? [mainlineFalsifierFixture(`falsifier-${symbol.toLowerCase()}-1w`)] : [],
        checkpoints: [],
        confidence: index < 4 ? "medium" : "low",
      },
      outlook_1m: {
        horizon: "1m",
        direction: index < 4 ? "bearish" : "no_call",
        causal_channel: index < 4 ? "持续高实际利率限制中期估值扩张。" : "等待更完整的月度证据。",
        supporting_evidence_refs: [moduleRef("rates_fed")],
        conflicting_evidence_refs: [moduleRef("credit")],
        confirmation_triggers:
          index >= 4 && index < 8
            ? [
                {
                  condition_id: `confirm-${symbol.toLowerCase()}-1m`,
                  module_id: "credit",
                  dataset_id: "fred.bamlh0a0hym2",
                  metric_name: "change_1w_bp",
                  operator: "gte",
                  threshold: 30,
                  effect: "confirming",
                  rationale: "信用与利率压力共同持续后再形成中期判断。",
                },
              ]
            : [],
        falsifiers:
          index < 4 ? [mainlineFalsifierFixture(`falsifier-${symbol.toLowerCase()}-1m`)] : [],
        checkpoints: [],
        confidence: index < 4 ? "medium" : "low",
      },
    })),
    gaps: [],
    citations: MODULE_IDS.map((moduleId) => ({
      evidence_ref: moduleRef(moduleId),
      module_id: moduleId,
      dataset_id: `fixture.${moduleId}`,
      source_role: "decision_primary",
      label: moduleLabel(moduleId),
      reference: "2026-07-27",
      published_at_ms: NOW - 900_000,
      received_at_ms: NOW - 600_000,
      source_url: `https://example.com/${moduleId}`,
    })),
    narrative_sections: [
      {
        section_id: "mainline",
        title: "市场主线",
        markdown: "## 判断\n\n实际利率上行仍然主导短期跨资产定价。\n\n- 贴现率抬升\n- 信用尚未确认",
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

function macroAssetPresentationFixture(
  thesis: MacroThesisV1,
): MacroOverviewReadData["asset_presentation"] {
  return thesis.assets.map((asset, index) => {
    const group =
      index < 4
        ? ("actionable" as const)
        : index < 8
          ? ("watch" as const)
          : ("evidence_gap" as const);
    const horizons = ([asset.outlook_1w, asset.outlook_1m] as const).map((outlook) => ({
      horizon: outlook.horizon,
      momentum_state:
        outlook.horizon === "1w" ? asset.momentum.momentum_1w : asset.momentum.momentum_1m,
      momentum_value:
        outlook.horizon === "1w" ? asset.momentum.return_1w_pct : asset.momentum.return_1m_pct,
      outlook_direction: outlook.direction,
      reader_rationale: {
        text: outlook.causal_channel,
        origin: "publication" as const,
      },
      confidence: outlook.confidence,
      supporting_evidence_refs: outlook.supporting_evidence_refs,
      conflicting_evidence_refs: outlook.conflicting_evidence_refs,
      confirmation_triggers: outlook.confirmation_triggers,
      falsifiers: outlook.falsifiers,
      checkpoints: outlook.checkpoints,
      reason:
        group === "evidence_gap"
          ? macroReason({
              code: "asset_momentum_insufficient",
              message: `${asset.symbol} ${outlook.horizon} 缺少完整事实动量。`,
              impact: "blocked",
              nextAction: "等待资产数据按市场时钟自动补齐。",
              nextCheckAtMs: NOW + 86_400_000,
              affectedDatasetIds: [`fixture.${asset.symbol.toLowerCase()}`],
              affectedClaimIds: ["claim-rates"],
            })
          : null,
    }));
    return {
      symbol: asset.symbol,
      order: index + 1,
      group,
      source_dataset_id:
        asset.momentum.source_dataset_id ?? `fixture.${asset.symbol.toLowerCase()}`,
      as_of: asset.momentum.as_of,
      claim_ids: ["claim-rates"],
      horizons: [horizons[0]!, horizons[1]!],
    };
  });
}

function macroClaimPresentationFixture(
  thesis: MacroThesisV1,
): MacroOverviewReadData["claim_presentation"] {
  return thesis.mainline.claims.map((claim) => ({
    claim_id: claim.claim_id,
    statement: {
      text: claim.statement,
      origin: "publication",
    },
    causal_edges: claim.causal_edges.map((edge) => ({
      source_label: edge.source,
      mechanism: {
        text: edge.mechanism,
        origin: "publication",
      },
      target_label: edge.target,
    })),
    module_evidence: thesis.module_assessments
      .filter((assessment) => assessment.claim_ids.includes(claim.claim_id))
      .map((assessment) => ({
        module_id: assessment.module_id,
        role: assessment.role,
        reader_narrative: {
          text: assessment.analysis,
          origin: "publication",
        },
        supporting_evidence_refs: assessment.supporting_evidence_refs,
        conflicting_evidence_refs: assessment.conflicting_evidence_refs,
      })),
    supporting_evidence_refs: claim.supporting_evidence_refs,
    conflicting_evidence_refs: claim.conflicting_evidence_refs,
    conditions: claim.conditions,
    falsifiers: thesis.mainline.falsifiers,
    checkpoints: thesis.mainline.checkpoints,
    asset_implications: thesis.assets.slice(0, 4).flatMap((asset) =>
      ([asset.outlook_1w, asset.outlook_1m] as const).map((outlook) => ({
        symbol: asset.symbol,
        horizon: outlook.horizon,
        direction: outlook.direction,
        reader_rationale: {
          text: outlook.causal_channel,
          origin: "publication",
        },
        confidence: outlook.confidence,
        evidence_links: [...outlook.supporting_evidence_refs, ...outlook.conflicting_evidence_refs],
        confirmation_triggers: outlook.confirmation_triggers,
        falsifiers: outlook.falsifiers,
        checkpoints: outlook.checkpoints,
      })),
    ),
  }));
}

function macroAlternativePresentationFixture(
  thesis: MacroThesisV1,
): MacroOverviewReadData["alternative_presentation"] {
  const alternative = thesis.alternative_explanation;
  return alternative
    ? {
        title: {
          text: alternative.title,
          origin: "publication",
        },
        thesis: {
          text: alternative.thesis,
          origin: "publication",
        },
        causal_edges: alternative.causal_edges.map((edge) => ({
          source_label: edge.source,
          mechanism: {
            text: edge.mechanism,
            origin: "publication",
          },
          target_label: edge.target,
        })),
        supporting_evidence_refs: alternative.supporting_evidence_refs,
        conflicting_evidence_refs: alternative.conflicting_evidence_refs,
        trigger_conditions: alternative.trigger_conditions,
      }
    : null;
}

function macroMainlinePresentationFixture(
  thesis: MacroThesisV1,
): NonNullable<MacroOverviewReadData["mainline_presentation"]> {
  return {
    title: {
      text: thesis.mainline.title,
      origin: "publication",
    },
    thesis: {
      text: thesis.mainline.thesis,
      origin: "publication",
    },
  };
}

function macroLiveDeltaFixture(
  thesis: MacroThesisV1,
): NonNullable<MacroOverviewReadData["live_delta"]> {
  const itemReason = macroReason({
    code: "condition_threshold_matched",
    message: "观察值达到已发布条件阈值。",
  });
  const item = (
    bindingType: "claim" | "falsifier" | "checkpoint",
    bindingId: string,
    conditionId: string,
    datasetId: string,
    datasetLabel: string,
    metricName: string,
  ) => ({
    binding_type: bindingType,
    binding_id: bindingId,
    condition_id: conditionId,
    status: "confirming" as const,
    dataset_id: datasetId,
    dataset_label: datasetLabel,
    metric_name: metricName,
    unit: "bp",
    observed_value: 22,
    observed_at_ms: NOW - 60_000,
    observation_cutoff_ms: NOW - 60_000,
    operator: "gte" as const,
    threshold: 20,
    rationale: "高收益信用利差走阔，正在确认融资压力。",
    source_reason_code: "condition_threshold_matched",
    reason: itemReason,
  });
  return {
    schema_version: "macro_live_delta_read_v1",
    source_schema_version: "macro_live_delta_v1",
    live_delta_id: "mld_fixture",
    publication_id: thesis.publication_id,
    evaluated_at_ms: NOW,
    module_fact_cutoff_ms: NOW - 60_000,
    mainline_validity: "confirming",
    matched_claim_ids: ["claim-rates"],
    matched_falsifier_ids: [],
    matched_checkpoint_ids: ["checkpoint-credit"],
    scopes: [
      {
        scope: "mainline",
        scope_id: "mainline",
        label: "整体主线",
        status: "confirming",
        matched_binding_ids: ["claim-rates"],
        items: [
          item(
            "claim",
            "claim-rates",
            "checkpoint-credit",
            "fred.bamlh0a0hym2",
            "美国高收益信用利差",
            "change_1w_bp",
          ),
        ],
      },
      {
        scope: "tension",
        scope_id: "tension:tension-credit",
        label: "信用利差尚未确认利率压力。",
        status: "confirming",
        matched_binding_ids: ["tension-credit"],
        items: [
          item(
            "checkpoint",
            "tension-credit",
            "tension-credit-resolution",
            "fred.bamlh0a0hym2",
            "美国高收益信用利差",
            "change_1w_bp",
          ),
        ],
      },
      {
        scope: "asset",
        scope_id: "asset:SPY:1w",
        label: "SPY · 1 周",
        status: "confirming",
        matched_binding_ids: ["asset:SPY:1w"],
        items: [
          item(
            "falsifier",
            "asset:SPY:1w",
            "falsifier-spy-1w",
            "fred.dgs2",
            "美国 2 年期国债收益率",
            "change_1w_bp",
          ),
        ],
      },
    ],
    reason_codes: ["condition_threshold_matched"],
    input_hash: `sha256:${"3".repeat(64)}`,
  };
}

function macroOutcomeReplayFixture(
  thesis: MacroThesisV1,
): NonNullable<MacroOverviewReadData["outcome_replay"]> {
  return {
    schema_version: "macro_outcome_replay_read_v1",
    source_schema_version: "macro_outcome_replay_v1",
    replay_id: "mor_fixture",
    publication_id: thesis.publication_id,
    evaluated_at_ms: NOW,
    horizons: (["1d", "1w", "1m"] as const).map((horizon, index) => {
      const expiresAtMs = NOW + (index + 1) * 86_400_000;
      const reason = macroReason({
        code: "horizon_not_expired",
        message: `${horizon} 结果观察期尚未结束。`,
        nextAction: "到期后按市场数据时钟自动评估。",
        nextCheckAtMs: expiresAtMs,
      });
      return {
        horizon,
        expires_at_ms: expiresAtMs,
        status: "pending",
        benchmark_symbol: "SPY",
        realized_return_pct: null,
        direction_correct: null,
        source_reason_code: "horizon_not_expired",
        reason,
        asset_results: [
          {
            symbol: "SPY",
            horizon: horizon === "1m" ? ("1m" as const) : ("1w" as const),
            expires_at_ms: expiresAtMs,
            status: "pending" as const,
            published_direction: "bearish" as const,
            realized_return_pct: null,
            direction_correct: null,
            source_reason_code: "asset_horizon_not_expired",
            reason,
          },
        ],
      };
    }),
    input_hash: `sha256:${"4".repeat(64)}`,
  };
}

export function macroModuleFixture(moduleId: MacroModuleId): MacroTypedModuleReadData {
  const base = moduleBase(moduleId);
  if (moduleId === "rates_fed") {
    return {
      ...base,
      schema_version: "macro_rates_fed_v5",
      module_id: "rates_fed",
      curve: {
        classification: {
          state: "bear_steepening",
          label: "熊市陡峭化",
          formula_version: "level_slope_curvature_classification_v2",
          inputs: {
            "2y_change_bp": 15,
            "10y_change_bp": 25,
            current_2s10s_bp: 35,
            current_as_of: "2026-07-27",
            curvature_change_bp: 2,
            level_change_bp: 20,
            prior_as_of: "2026-07-20",
            slope_change_bp: 10,
          },
        },
        nominal_snapshots: curveYieldSnapshots([4.3, 4.15, 4.22, 4.4, 4.62]),
        real_snapshots: curveYieldSnapshots([1.7, 1.85, 1.94, 2.08, 2.22]),
        breakeven_snapshots: curveBreakevenSnapshots([2.6, 2.3, 2.28, 2.32, 2.4]),
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
          change_from_prior: "unchanged",
          reason: "最新已审阅的联邦公开市场委员会声明维持偏鹰立场。",
          analysis_id: "fomc-fixture",
        },
        officials_distribution: {
          state: "current",
          window_days: 90,
          as_of: "2026-07-27",
          stance_event_counts: { hawkish: 4, neutral: 2, dovish: 1, mixed: 1 },
          stance_unique_official_counts: { hawkish: 3, neutral: 2, dovish: 1, mixed: 1 },
          analyzed_event_count: 8,
          not_policy_signal_event_count: 1,
          uncertain_event_count: 0,
          unique_official_count: 7,
        },
        timeline: [
          {
            document_id: "fed-fixture",
            document_type: "statement",
            title: "Federal Reserve issues FOMC statement",
            effective_date: "2026-07-23",
            official_id: null,
            published_at_ms: NOW - 900_000,
            source_url: "https://example.com/fed",
            speaker_name: null,
            role_title: "FOMC",
            fomc_voter: null,
            analysis: {
              analysis_id: "fomc-fixture",
              change_from_prior: "unchanged",
              confidence: 0.82,
              evidence: [
                {
                  claim: "声明继续强调通胀风险。",
                  excerpt: "Inflation remains somewhat elevated.",
                },
              ],
              model_name: "fixture-model",
              policy_relevance: "policy_signal",
              prompt_version: "fed-analysis-v1",
              rationale: "最新声明没有释放明确宽松信号。",
              reviewer_disposition: "pass",
              stance: "hawkish",
              state: "analyzed",
            },
          },
        ],
        roster: { state: "current", reason: null, officials: [] },
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
      schema_version: "macro_economy_inflation_v5",
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
      schema_version: "macro_liquidity_funding_v5",
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
    const creditReturnMatrix = [
      crossAssetReturnRow(1, "credit_etf", "信用 ETF", "LQD", "lqd", [0.1, 0.4, 1.1]),
      crossAssetReturnRow(2, "credit_etf", "信用 ETF", "HYG", "hyg", [-0.1, 0.2, 0.8]),
    ];
    return {
      ...base,
      schema_version: "macro_credit_v7",
      module_id: "credit",
      cycle_dimensions: [
        {
          dimension_id: "spread_level_velocity",
          label: "利差水平与速度",
          state: "tightening",
          driver: "评级梯级近月平均走阔",
          evidence_dataset_ids: ["fred.bamlc0a0cm", "fred.bamlh0a3hyc"],
          conflicts: ["利差仍处低分位，但近月正在走阔"],
        },
        {
          dimension_id: "funding_cost",
          label: "绝对融资成本",
          state: "expensive",
          driver: "公司债绝对收益率处于实际历史高分位",
          evidence_dataset_ids: ["fred.bamlc0a0cmey", "fred.bamlh0a0hym2ey"],
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
          evidence_dataset_ids: ["fred.drblacbs", "fred.drcrelexfacbs"],
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
          {
            as_of: "2026-07-27",
            corporate_dataset_id: "fred.bamlc0a0cmey",
            formula_version: "matched_rate_difference_v1",
            input_fact_ids: ["fact:ig", "fact:effr"],
            label: "IG yield − EFFR",
            reference_dataset_id: "fred.effr",
            value_bp: 77,
          },
          {
            as_of: "2026-07-27",
            corporate_dataset_id: "fred.bamlh0a0hym2ey",
            formula_version: "matched_rate_difference_v1",
            input_fact_ids: ["fact:hy", "fact:effr"],
            label: "HY yield − EFFR",
            reference_dataset_id: "fred.effr",
            value_bp: 327,
          },
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
        return_matrix: creditReturnMatrix,
        source_identity: creditReturnMatrix.map(crossAssetIdentity),
        positions: [],
      },
    };
  }
  if (moduleId === "volatility") {
    return {
      ...base,
      schema_version: "macro_volatility_v6",
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
        normalized_groups: [
          crossAssetNormalizedGroup(1, "cross_asset_implied_volatility", "跨资产隐含波动率", [
            ["VXN", "Nasdaq 100 波动率指数", "fred.vxncls", [100, 105, 110, 116]],
            ["GVZ", "黄金波动率指数", "fred.gvzcls", [100, 107, 111, 114]],
            ["OVX", "原油波动率指数", "fred.ovxcls", [100, 104, 107, 111]],
          ]),
        ],
      },
    };
  }
  const returnMatrix = [
    crossAssetReturnRow(1, "equity", "权益", "SPY", "spy", [0.4, 1.2, 3.8]),
    crossAssetReturnRow(2, "equity", "权益", "QQQ", "qqq", [0.6, 1.8, 4.2]),
    crossAssetReturnRow(3, "equity", "权益", "IWM", "iwm", [-0.2, 0.5, 1.1]),
    crossAssetReturnRow(4, "duration_credit", "久期与信用", "TLT", "tlt", [-0.3, -1.4, -2.2]),
    crossAssetReturnRow(5, "duration_credit", "久期与信用", "IEF", "ief", [-0.1, -0.5, -0.8]),
    crossAssetReturnRow(6, "duration_credit", "久期与信用", "LQD", "lqd", [0.1, 0.4, 1.1]),
    crossAssetReturnRow(7, "duration_credit", "久期与信用", "HYG", "hyg", [-0.1, 0.2, 0.8]),
    crossAssetReturnRow(8, "dollar_commodities", "美元与商品", "UUP", "dxy", [0.2, 0.7, 1.3]),
    crossAssetReturnRow(9, "dollar_commodities", "美元与商品", "GLD", "gld", [0.2, 1.8, 4.5]),
    crossAssetReturnRow(10, "dollar_commodities", "美元与商品", "USO", "uso", [-0.5, 1.1, 2.4]),
  ];
  return {
    ...base,
    schema_version: "macro_cross_asset_v6",
    module_id: "cross_asset",
    assets: {
      return_matrix: returnMatrix,
      normalized_groups: [
        crossAssetNormalizedGroup(1, "equity", "权益", [
          ["SPY", "SPDR标普500 ETF", "nasdaq.spy.daily", [100, 102, 104, 106]],
          ["QQQ", "Invesco纳斯达克100 ETF", "nasdaq.qqq.daily", [100, 103, 105, 108]],
          ["IWM", "iShares罗素2000 ETF", "nasdaq.iwm.daily", [100, 99, 101, 102]],
        ]),
        crossAssetNormalizedGroup(2, "duration_credit", "久期与信用", [
          ["TLT", "iShares 20年以上美债 ETF", "nasdaq.tlt.daily", [100, 99, 98, 97]],
          ["IEF", "iShares 7-10年美债 ETF", "nasdaq.ief.daily", [100, 99.5, 99, 98.5]],
          ["LQD", "iShares投资级公司债 ETF", "nasdaq.lqd.daily", [100, 100.2, 100.6, 101]],
          ["HYG", "iShares高收益公司债 ETF", "nasdaq.hyg.daily", [100, 100.5, 101, 101.5]],
        ]),
        crossAssetNormalizedGroup(3, "dollar_commodities", "美元与商品", [
          ["UUP", "Invesco美元指数 ETF", "nasdaq.dxy.daily", [100, 100.5, 101, 101.5]],
          ["GLD", "SPDR黄金 ETF", "nasdaq.gld.daily", [100, 102, 104, 105]],
          ["USO", "United States Oil Fund", "nasdaq.uso.daily", [100, 99, 101, 103]],
        ]),
      ],
      source_identity: [
        ...returnMatrix.map(crossAssetIdentity),
        crossAssetStandaloneIdentity(11, "WTI", "WTI Cushing 现货", "official_benchmark", [
          crossAssetSource(
            "WTI",
            "fred.dcoilwtico",
            "WTI Cushing 现货",
            "decision_primary",
            [0.2, 1, 2],
          ),
        ]),
        crossAssetStandaloneIdentity(12, "BTC", "Bitcoin / USD", "crypto", [
          crossAssetSource(
            "BTC",
            "binance.btcusdt.spot",
            "Binance BTC/USDT 现货",
            "decision_primary",
            [1.4, 5.5, 12],
          ),
        ]),
        crossAssetStandaloneIdentity(13, "VIX", "Cboe Volatility Index", "volatility", [
          crossAssetSource(
            "VIX",
            "fred.vixcls",
            "Cboe 波动率指数",
            "decision_primary",
            [0.3, 1, 2],
          ),
        ]),
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
      return_matrix: [
        crossAssetReturnRow(
          1,
          "major_futures",
          "主要连续期货",
          "ES",
          "es_future",
          [0.3, 1.1, 3],
          "es",
        ),
      ],
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
): IndicatorFixture {
  const latestValue = values.at(-1);
  if (latestValue == null) throw new Error(`indicator fixture ${datasetId} requires values`);
  return {
    dataset_id: datasetId,
    label,
    unit,
    latest_value: latestValue,
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

function historyRows(values: number[]): HistoryPointFixture[];
function historyRows(values: number[], key: "value"): HistoryPointFixture[];
function historyRows(values: number[], key: "value_bp"): CurveSpreadPointFixture[];
function historyRows(
  values: number[],
  key: "value" | "value_bp" = "value",
): HistoryPointFixture[] | CurveSpreadPointFixture[] {
  const dates = ["2026-04-27", "2026-05-27", "2026-06-27", "2026-07-27"];
  if (key === "value_bp") {
    return values.map((value, index) => ({
      date: dates[index] ?? `2026-07-${index + 1}`,
      value_bp: value,
    }));
  }
  return values.map((value, index) => ({
    date: dates[index] ?? `2026-07-${index + 1}`,
    value,
  }));
}

function curveYieldSnapshots(currentValues: number[]): CurveYieldSnapshotFixture[] {
  const tenors = [
    ["3M", 0.25],
    ["2Y", 2],
    ["5Y", 5],
    ["10Y", 10],
    ["30Y", 30],
  ] as const;
  const snapshots: Array<{
    window: CurveYieldSnapshotFixture["window"];
    asOf: string;
    offset: number;
  }> = [
    { window: "current", asOf: "2026-07-27", offset: 0 },
    { window: "1w", asOf: "2026-07-20", offset: -0.05 },
    { window: "1m", asOf: "2026-06-27", offset: -0.1 },
    { window: "3m", asOf: "2026-04-27", offset: -0.15 },
  ];
  return snapshots.map(({ window, asOf, offset }) => ({
    window,
    as_of: asOf,
    points: tenors.map(([tenor, years], index) => ({
      tenor,
      years,
      yield_pct: currentValues[index]! + offset,
    })),
  }));
}

function curveBreakevenSnapshots(currentValues: number[]): CurveBreakevenSnapshotFixture[] {
  const tenors = [
    ["3M", 0.25],
    ["2Y", 2],
    ["5Y", 5],
    ["10Y", 10],
    ["30Y", 30],
  ] as const;
  const snapshots: Array<{
    window: CurveBreakevenSnapshotFixture["window"];
    asOf: string;
    offset: number;
  }> = [
    { window: "current", asOf: "2026-07-27", offset: 0 },
    { window: "1w", asOf: "2026-07-20", offset: -0.05 },
    { window: "1m", asOf: "2026-06-27", offset: -0.1 },
    { window: "3m", asOf: "2026-04-27", offset: -0.15 },
  ];
  return snapshots.map(({ window, asOf, offset }) => ({
    window,
    as_of: asOf,
    points: tenors.map(([tenor, years], index) => ({
      tenor,
      years,
      breakeven_pct: currentValues[index]! + offset,
    })),
  }));
}

function release(
  datasetId: string,
  label: string,
  actual: number,
  estimate: number,
  prior: number,
): ReleaseFixture {
  const observation = {
    reference_period: "2026-06",
    actual_value: actual,
    estimate_value: estimate,
    prior_value: prior,
    revised_prior_value: prior + 0.1,
    surprise: actual - estimate,
    revision: 0.1,
    unit: "percent",
    scheduled_at_ms: NOW - 1_200_000,
    published_at_ms: NOW - 900_000,
    received_at_ms: NOW - 600_000,
    source_url: "https://example.com/release",
  };
  return {
    dataset_id: datasetId,
    label,
    ...observation,
    observations: [observation],
  };
}

function assetFact(
  symbol: string,
  datasetId: string,
  changes: [number, number, number],
): MarketFactFixture {
  return {
    dataset_id: datasetId,
    latest_value: 100,
    as_of: "2026-07-27",
    market_time_ms: NOW,
    change_1d_pct: changes[0],
    change_1w_pct: changes[1],
    change_1m_pct: changes[2],
    source_url: "https://example.com/market",
    unit: symbol === "BTC" ? "usd" : "index",
  };
}

function crossAssetReturnRow(
  displayOrder: number,
  groupId: string,
  groupLabel: string,
  symbol: string,
  intradayDatasetStem: string,
  changes: [number, number, number],
  dailyDatasetStem = intradayDatasetStem,
): CrossAssetReturnRowFixture {
  return {
    display_order: displayOrder,
    group_id: groupId,
    group_label: groupLabel,
    symbol,
    label: `${symbol} 可交易代理`,
    identity_policy: "separate_source_facts_no_blend",
    selection_policy: "intraday_latest_and_daily_returns_exact",
    latest_source: crossAssetSource(
      symbol,
      `yfinance.${intradayDatasetStem}.intraday`,
      `${symbol} 盘中价格`,
      "intraday_proxy",
      changes,
    ),
    return_source: crossAssetSource(
      symbol,
      `nasdaq.${dailyDatasetStem}.daily`,
      `${symbol} 日频收益`,
      "decision_primary",
      changes,
    ),
  };
}

function crossAssetSource(
  symbol: string,
  datasetId: string,
  label: string,
  sourceRole: string,
  changes: [number, number, number],
): CrossAssetSourceFixture {
  return {
    dataset_id: datasetId,
    label,
    source_role: sourceRole,
    fact: assetFact(symbol, datasetId, changes),
  };
}

function crossAssetNormalizedGroup(
  displayOrder: number,
  groupId: string,
  label: string,
  rows: Array<readonly [string, string, string, number[]]>,
): CrossAssetNormalizedGroupFixture {
  return {
    display_order: displayOrder,
    group_id: groupId,
    label,
    series: rows.map(([symbol, seriesLabel, datasetId, values], index) => ({
      display_order: index + 1,
      symbol,
      label: seriesLabel,
      source: {
        dataset_id: datasetId,
        label: seriesLabel,
        source_role: "decision_primary",
        fact: null,
      },
      points: values.map((value, pointIndex) => ({
        date: `2026-07-${String(24 + pointIndex).padStart(2, "0")}`,
        normalized_value: value,
      })),
    })),
  };
}

function crossAssetIdentity(row: CrossAssetReturnRowFixture): CrossAssetSourceIdentityFixture {
  return crossAssetStandaloneIdentity(row.display_order, row.symbol, row.label, "etf", [
    row.latest_source,
    row.return_source,
  ]);
}

function crossAssetStandaloneIdentity(
  displayOrder: number,
  symbol: string,
  label: string,
  evidenceKind: string,
  sources: CrossAssetSourceFixture[],
): CrossAssetSourceIdentityFixture {
  return {
    display_order: displayOrder,
    symbol,
    label,
    evidence_kind: evidenceKind,
    identity_policy: "separate_source_facts_no_blend",
    selection_policy: "decision_primary_only_no_fallback",
    sources,
  };
}

export function macroResearchFixture(
  state: MacroThesisDetailReadData["state"] = "current",
): MacroThesisDetailReadData {
  const thesis = state === "current" || state === "historical" ? macroThesisFixture() : null;
  const stateReason =
    state === "current" || state === "historical"
      ? null
      : macroReason({
          code: `macro_thesis_${state}`,
          message:
            state === "failed"
              ? "研究提供方暂不可用，本次发布失败。"
              : state === "generating"
                ? "后台研究运行尚未完成。"
                : "该交易日尚无已发布主线。",
          impact: "blocked",
          nextAction:
            state === "missing"
              ? "等待 08:50 New York 后台任务。"
              : state === "failed"
                ? "检查研究提供方配置后重新生成。"
                : "后台将按运行策略继续处理。",
          nextCheckAtMs: NOW + 300_000,
          retryable: state === "failed" ? false : undefined,
          recovery: state === "failed" ? "operator_action" : undefined,
        });
  return {
    schema_version: "macro_thesis_detail_v3",
    state,
    requested_session_date: "2026-07-27",
    current_session_date: state === "historical" ? "2026-07-28" : "2026-07-27",
    displayed_session_date: thesis?.session_date ?? null,
    reason: stateReason,
    thesis,
    fallback: {
      state: "none",
      reason:
        stateReason ??
        macroReason({
          code: "requested_publication_available",
          message: "请求交易日主线已发布。",
        }),
      publication_id: null,
      session_date: null,
      cutoff_ms: null,
    },
    mainline_presentation: thesis ? macroMainlinePresentationFixture(thesis) : null,
    asset_presentation: thesis ? macroAssetPresentationFixture(thesis) : [],
    claim_presentation: thesis ? macroClaimPresentationFixture(thesis) : [],
    alternative_presentation: thesis ? macroAlternativePresentationFixture(thesis) : null,
    live_delta: thesis && state === "current" ? macroOverviewFixture().live_delta : null,
    outcome_replay: thesis && state === "current" ? macroOverviewFixture().outcome_replay : null,
    appendix: thesis ? macroPublicationAppendixFixture(thesis) : null,
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
            reason: stateReason,
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

function macroPublicationAppendixFixture(
  thesis: MacroThesisV1,
): NonNullable<MacroThesisDetailReadData["appendix"]> {
  return {
    schema_version: "macro_publication_appendix_v1",
    publication_id: thesis.publication_id,
    evidence_pack_id: thesis.evidence_pack_id,
    session_date: thesis.session_date,
    cutoff_ms: thesis.cutoff_ms,
    sealed_at_ms: thesis.cutoff_ms + 60_000,
    source_max_received_at_ms: thesis.cutoff_ms - 60_000,
    data_quality: {
      coverage_state: "complete",
      current_health_state: "current",
      history_depth_state: "complete",
      coverage_gap_count: 0,
      current_health_gap_count: 0,
      history_gap_count: 0,
      modules: MODULE_IDS.map((moduleId) => ({
        module_id: moduleId,
        label: moduleLabel(moduleId),
        coverage_state: "complete",
        current_health_state: "current",
        history_depth_state: "complete",
        expected_capabilities: 1,
        available_capabilities: 1,
        current_datasets: 1,
        tracked_datasets: 1,
        complete_history_datasets: 1,
        tracked_history_datasets: 1,
        backfill_state: "complete",
        backfill_worker_enabled: true,
        latest_fact_at_ms: thesis.cutoff_ms - 120_000,
        reasons: [],
      })),
    },
    source_lineage: MODULE_IDS.map((moduleId) => ({
      module_id: moduleId,
      module_label: moduleLabel(moduleId),
      dataset_id: `fixture.${moduleId}`,
      label: `${moduleLabel(moduleId)}核心事实`,
      value: moduleId === "rates_fed" ? 4.38 : 1,
      unit: moduleId === "rates_fed" ? "percent" : "index",
      reference: thesis.session_date,
      observed_at_ms: thesis.cutoff_ms - 240_000,
      published_at_ms: thesis.cutoff_ms - 180_000,
      received_at_ms: thesis.cutoff_ms - 120_000,
      source_url: `https://example.com/${moduleId}`,
      source_role: "decision_primary",
      current_health: "current",
      current_reason: null,
      history_depth: "complete",
      history_reason: null,
    })),
    reconciliation_receipts: [
      {
        module_id: "rates_fed",
        module_label: moduleLabel("rates_fed"),
        concept_id: "treasury_2y_yield",
        identity_policy: "same_concept_same_reference_only",
        selection_policy: "decision_primary_only_no_fallback",
        selected_dataset_id: "fred.dgs2",
        state: "complete",
        observations: [
          {
            dataset_id: "fred.dgs2",
            source_role: "decision_primary",
            value: 4.38,
            unit: "percent",
            reference: thesis.session_date,
            fact_ref: "macro-observation:fred.dgs2:2026-07-27",
          },
          {
            dataset_id: "treasury.daily_treasury_real_long_term_rate",
            source_role: "official_cross_check",
            value: 4.37,
            unit: "percent",
            reference: thesis.session_date,
            fact_ref: "macro-observation:treasury.2y:2026-07-27",
          },
        ],
        comparisons: [
          {
            left_dataset_id: "fred.dgs2",
            right_dataset_id: "treasury.daily_treasury_real_long_term_rate",
            left_value: 4.38,
            right_value: 4.37,
            difference: 0.01,
            tolerance: 0.02,
            unit: "percent",
            status: "within_tolerance",
            left_reference: thesis.session_date,
            right_reference: thesis.session_date,
            aligned_reference: thesis.session_date,
            left_fact_ref: "macro-observation:fred.dgs2:2026-07-27",
            right_fact_ref: "macro-observation:treasury.2y:2026-07-27",
          },
        ],
      },
    ],
  };
}

function moduleBase(moduleId: MacroModuleId): ModuleBaseFixture {
  return {
    availability: "available" as const,
    reason: null,
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
      backfill_execution: {
        state: "complete" as const,
        worker_enabled: true,
        total_targets: 1,
        complete_targets: 1,
        pending_targets: 0,
        failed_targets: 0,
        next_check_at_ms: null,
        reason: null,
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
        reason: macroReason({
          code: "next_release_scheduled",
          message: "下一次官方发布将重新检查该条件。",
          nextAction: "按 Dataset Registry 数据时钟自动检查。",
          nextCheckAtMs: NOW + 86_400_000,
        }),
        next_check_at_ms: NOW + 86_400_000,
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
          current_reason: macroReason({
            code: "latest_fact_within_natural_cadence",
            message: "最新事实仍在自然发布频率内。",
          }),
          history_reason: macroReason({
            code: "history_requirement_satisfied",
            message: "历史深度满足当前计算要求。",
          }),
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
          observed_at_ms: NOW - 1_200_000,
          published_at_ms: NOW - 900_000,
          received_at_ms: NOW - 600_000,
          source_url: "https://example.com/source",
        },
      ],
      asset_changes: [],
      reconciliation_receipts: [
        {
          concept_id: `${moduleId}.core`,
          identity_policy: "separate_source_facts_no_blend",
          selected_dataset_id: "fixture.dataset",
          selection_policy: "decision_primary_only_no_fallback",
          state: "complete",
          observations: [
            {
              dataset_id: "fixture.dataset",
              fact_ref: `fact:${moduleId}:decision-primary`,
              reference: "2026-07-27",
              source_role: "decision_primary",
              unit: "percent",
              value: 4.3,
            },
            {
              dataset_id: "fixture.proxy",
              fact_ref: `fact:${moduleId}:intraday-proxy`,
              reference: "2026-07-27",
              source_role: "intraday_proxy",
              unit: "percent",
              value: 4.28,
            },
          ],
          comparisons: [
            {
              aligned_reference: "2026-07-27",
              difference: 0.02,
              left_dataset_id: "fixture.dataset",
              left_fact_ref: `fact:${moduleId}:decision-primary`,
              left_reference: "2026-07-27",
              left_value: 4.3,
              right_dataset_id: "fixture.proxy",
              right_fact_ref: `fact:${moduleId}:intraday-proxy`,
              right_reference: "2026-07-27",
              right_value: 4.28,
              status: "within_tolerance",
              tolerance: 0.05,
              unit: "percent",
            },
          ],
        },
      ],
    },
    thesis_context: {
      state: "current" as const,
      session_date: "2026-07-27",
      cutoff_ms: NOW - 3_600_000,
      role: moduleId === "rates_fed" ? ("driver" as const) : ("confirming" as const),
      reader_narrative: {
        text:
          moduleId === "rates_fed"
            ? "该模块提供主线驱动证据。"
            : "该模块用于确认或反驳已发布主线。",
        origin: "publication" as const,
      },
      claim_ids: ["claim-rates"],
      supporting_evidence_refs: [`macro-module:2026-07-27:${moduleId}`],
      conflicting_evidence_refs: [],
      annotations: [],
      reason: null,
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
