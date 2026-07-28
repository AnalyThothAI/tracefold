import * as PageState from "@shared/ui/PageState";
import { Button } from "@shared/ui/button";
import {
  ArrowDownRight,
  ArrowRight,
  ArrowUpRight,
  CircleMinus,
  Clock3,
  ExternalLink,
  RefreshCw,
  ShieldAlert,
} from "lucide-react";
import { Link } from "react-router-dom";

import { useMacroModuleQuery, useMacroOverviewQuery } from "../api/useMacroDecisionQuery";
import type {
  MacroAssetDirection,
  MacroChange,
  MacroCoverageState,
  MacroDailyJudgment,
  MacroDataHealthState,
  MacroJudgmentState,
  MacroModuleId,
  MacroOverviewReadData,
  MacroTypedModuleReadData,
} from "../model/macroTypes";

import { MacroModuleSections } from "./MacroModuleSections";

import "./MacroDecisionPage.css";
import "./MacroDecisionEvidence.css";
import "./MacroDecisionPageResponsive.css";

const MODULE_ROUTES: ReadonlyArray<{ id: MacroModuleId; path: string; label: string }> = [
  { id: "rates_fed", path: "/macro/rates-fed", label: "利率与美联储" },
  { id: "economy_inflation", path: "/macro/economy-inflation", label: "经济与通胀" },
  { id: "liquidity_funding", path: "/macro/liquidity-funding", label: "流动性与融资" },
  { id: "credit", path: "/macro/credit", label: "信用市场" },
  { id: "volatility", path: "/macro/volatility", label: "波动率" },
  { id: "cross_asset", path: "/macro/cross-asset", label: "大类资产与期货" },
];

export function MacroOverviewPage({ token }: { token: string }) {
  const query = useMacroOverviewQuery(token);
  if (query.isError && !query.data) {
    return <PageState.Error error={query.error} onRetry={() => void query.refetch()} />;
  }
  if (query.isLoading || !query.data) {
    return <PageState.Loading label="加载每日宏观决策台" layout="route" rows={8} />;
  }
  return (
    <PageState.Stale updating={query.isFetching && !query.isLoading}>
      <main aria-label="每日宏观决策台" className="macro-decision" data-page-archetype="decision">
        <DecisionHeader
          coverage={query.data.coverage_state}
          dataHealth={query.data.data_health_state}
          isFetching={query.isFetching}
          judgment={query.data.judgment_state}
          judgmentCutoffMs={query.data.judgment_cutoff_ms}
          latestFactAtMs={query.data.latest_fact_at_ms}
          title="每日宏观决策台"
          onRefresh={() => void query.refetch()}
        />
        <MacroNavigation />
        <Overview data={query.data} />
      </main>
    </PageState.Stale>
  );
}

export function MacroModulePage({ token, moduleId }: { token: string; moduleId: MacroModuleId }) {
  const query = useMacroModuleQuery(token, moduleId);
  if (query.isError && !query.data) {
    return <PageState.Error error={query.error} onRetry={() => void query.refetch()} />;
  }
  if (query.isLoading || !query.data) {
    return <PageState.Loading label="加载宏观模块" layout="route" rows={8} />;
  }
  return (
    <PageState.Stale updating={query.isFetching && !query.isLoading}>
      <main aria-label={query.data.label} className="macro-decision" data-page-archetype="decision">
        <DecisionHeader
          coverage={query.data.status.coverage.state}
          dataHealth={query.data.status.data_health.state}
          isFetching={query.isFetching}
          judgment={query.data.status.judgment.state}
          judgmentCutoffMs={query.data.status.judgment.cutoff_ms}
          latestFactAtMs={query.data.latest_fact_at_ms}
          title={query.data.label}
          onRefresh={() => void query.refetch()}
        />
        <MacroNavigation activeModule={moduleId} />
        <ModuleDetail module={query.data} />
      </main>
    </PageState.Stale>
  );
}

function DecisionHeader({
  title,
  coverage,
  dataHealth,
  judgment,
  judgmentCutoffMs,
  latestFactAtMs,
  isFetching,
  onRefresh,
}: {
  title: string;
  coverage: MacroCoverageState;
  dataHealth: MacroDataHealthState;
  judgment: MacroJudgmentState;
  judgmentCutoffMs: number | null;
  latestFactAtMs: number;
  isFetching: boolean;
  onRefresh: () => void;
}) {
  return (
    <header className="macro-decision__header">
      <div>
        <span>DAILY MACRO DECISION WORKBENCH</span>
        <h1>{title}</h1>
        <p>Coverage、实时数据健康和冻结判断分开呈现；不使用一个绿色标签掩盖缺项。</p>
      </div>
      <StatusTriplet coverage={coverage} dataHealth={dataHealth} judgment={judgment} />
      <dl>
        <div>
          <dt>判断截点</dt>
          <dd>{formatInstant(judgmentCutoffMs)}</dd>
        </div>
        <div>
          <dt>最新事实</dt>
          <dd>{formatInstant(latestFactAtMs || null)}</dd>
        </div>
      </dl>
      <Button disabled={isFetching} onClick={onRefresh} size="sm" type="button" variant="outline">
        <RefreshCw aria-hidden="true" />
        {isFetching ? "刷新中" : "刷新"}
      </Button>
    </header>
  );
}

function StatusTriplet({
  coverage,
  dataHealth,
  judgment,
}: {
  coverage: MacroCoverageState;
  dataHealth: MacroDataHealthState;
  judgment: MacroJudgmentState;
}) {
  return (
    <div aria-label="模块三类状态" className="macro-decision__status-triplet">
      <span data-state={coverage}>覆盖 {coverageLabel(coverage)}</span>
      <span data-state={dataHealth}>数据 {healthLabel(dataHealth)}</span>
      <span data-state={judgment}>判断 {judgmentLabel(judgment)}</span>
    </div>
  );
}

function MacroNavigation({ activeModule }: { activeModule?: MacroModuleId }) {
  return (
    <nav aria-label="宏观决策模块" className="macro-decision__nav">
      <Link aria-current={activeModule ? undefined : "page"} to="/macro">
        决策总览
      </Link>
      {MODULE_ROUTES.map((route) => (
        <Link
          aria-current={activeModule === route.id ? "page" : undefined}
          key={route.id}
          to={route.path}
        >
          {route.label}
        </Link>
      ))}
      <Link to="/macro/research">深度研究</Link>
    </nav>
  );
}

function Overview({ data }: { data: MacroOverviewReadData }) {
  return (
    <>
      {data.daily_judgment ? (
        <JudgmentPanel judgment={data.daily_judgment} />
      ) : (
        <section className="macro-decision__notice">
          <ShieldAlert aria-hidden="true" />
          <div>
            <h2>今日判断尚未发布</h2>
            <p>{judgmentStatusMessage(data)}</p>
          </div>
        </section>
      )}
      <section aria-label="六个宏观模块" className="macro-decision__module-grid">
        {data.modules.map((module) => (
          <article className="macro-decision__module-card" key={module.module_id}>
            <StatusTriplet
              coverage={module.coverage_state === "missing" ? "partial" : module.coverage_state}
              dataHealth={
                module.data_health_state === "missing" ? "invalid" : module.data_health_state
              }
              judgment={module.judgment_state}
            />
            <h2>{module.label}</h2>
            <p>{module.summary?.interpretation ?? "模块正在首次构建。"}</p>
            <ChangeList changes={module.top_changes.slice(0, 2)} compact />
            <small>
              {module.coverage_gap_count} 个覆盖缺口 · {module.health_gap_count} 个数据异常
            </small>
            <Link to={module.href}>
              打开模块 <ArrowRight aria-hidden="true" />
            </Link>
          </article>
        ))}
      </section>
      <ResearchStrip research={data.research} />
    </>
  );
}

function judgmentStatusMessage(data: MacroOverviewReadData): string {
  const status = data.judgment_status;
  if (!status) return "尚无发布尝试记录；事实页继续更新，但不会把普通行情刷新伪装成新判断。";
  if (status.reason_code !== "critical_evidence_blocked") {
    return `发布状态：${status.reason_code}。`;
  }
  const blockedModules = Array.isArray(status.details.blocked_modules)
    ? status.details.blocked_modules.filter((value): value is string => typeof value === "string")
    : [];
  const blockerFacts = judgmentBlockerFacts(status.details.modules);
  if (blockerFacts.length) {
    return `冻结截点缺少关键证据：${blockerFacts.join("；")}。`;
  }
  return blockedModules.length
    ? `冻结截点缺少关键证据，阻塞模块：${blockedModules.map(moduleLabel).join("、")}。`
    : "冻结截点缺少关键证据；详情保留在判断状态记录中。";
}

function judgmentBlockerFacts(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((moduleValue) => {
    if (!isRecord(moduleValue)) return [];
    const moduleId = typeof moduleValue.module_id === "string" ? moduleValue.module_id : "unknown";
    const gaps = Array.isArray(moduleValue.dataset_gaps) ? moduleValue.dataset_gaps : [];
    return gaps.slice(0, 3).flatMap((gapValue) => {
      if (!isRecord(gapValue)) return [];
      const label =
        typeof gapValue.label === "string"
          ? gapValue.label
          : typeof gapValue.dataset_id === "string"
            ? gapValue.dataset_id
            : null;
      if (!label) return [];
      const state = typeof gapValue.state === "string" ? gapValue.state : "missing";
      const reason = typeof gapValue.reason === "string" ? gapValue.reason : null;
      return [`${moduleLabel(moduleId)}—${label}（${blockerReasonLabel(reason, state)}）`];
    });
  });
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function dataStateLabel(value: string): string {
  return (
    {
      current: "当前",
      delayed: "延迟",
      stale: "陈旧",
      invalid: "无效",
      backfilling: "回填中",
      unavailable: "不可用",
      missing: "缺失",
    }[value] ?? value
  );
}

function blockerReasonLabel(reason: string | null, state: string): string {
  if (reason === "no_valid_fact") return "冻结截点前无有效事实";
  if (reason === "derived_fact_pending") return "冻结截点前派生事实未完成";
  if (reason === "backfill_required") return "冻结截点前所需回填未完成";
  return dataStateLabel(state);
}

function moduleLabel(moduleId: string): string {
  return MODULE_ROUTES.find((route) => route.id === moduleId)?.label ?? moduleId;
}

function JudgmentPanel({ judgment }: { judgment: MacroDailyJudgment }) {
  return (
    <section className="macro-decision__judgment">
      <header>
        <div>
          <span>DAILY JUDGMENT · {judgment.session_date}</span>
          <h2>{judgment.overall_state}</h2>
        </div>
        <small>Evidence Pack 已冻结，实时页面不会改写它</small>
      </header>
      <div className="macro-decision__dimensions">
        {Object.entries(judgment.dimensions).map(([dimension, state]) => (
          <article key={dimension}>
            <span>{dimensionLabel(dimension)}</span>
            <strong>{state.state}</strong>
            <p>{state.driver}</p>
          </article>
        ))}
      </div>
      <div className="macro-decision__judgment-grid">
        <div>
          <h3>主导压力</h3>
          <ul>
            {judgment.dominant_pressures.length ? (
              judgment.dominant_pressures.map((pressure, index) => (
                <li key={index}>{textValue(pressure.driver) ?? "等待证据"}</li>
              ))
            ) : (
              <li>当前没有三项以上压力共振。</li>
            )}
          </ul>
        </div>
        <AssetDirections directions={judgment.asset_directions} />
      </div>
    </section>
  );
}

function AssetDirections({ directions }: { directions: Record<string, MacroAssetDirection> }) {
  return (
    <div className="macro-decision__assets">
      <h3>固定资产方向</h3>
      <div role="table">
        <div role="row">
          <span role="columnheader">资产</span>
          <span role="columnheader">1周</span>
          <span role="columnheader">1月</span>
          <span role="columnheader">置信度</span>
        </div>
        {Object.entries(directions).map(([asset, direction]) => (
          <div key={asset} role="row">
            <strong role="cell">{asset}</strong>
            <Direction value={direction["1w"]} />
            <Direction value={direction["1m"]} />
            <span role="cell">{direction.confidence}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function Direction({ value }: { value: MacroAssetDirection["1w"] }) {
  const Icon =
    value === "up"
      ? ArrowUpRight
      : value === "down"
        ? ArrowDownRight
        : value === "range"
          ? CircleMinus
          : Clock3;
  return (
    <span data-direction={value} role="cell">
      <Icon aria-hidden="true" />
      {value}
    </span>
  );
}

function ModuleDetail({ module }: { module: MacroTypedModuleReadData }) {
  return (
    <>
      <section className="macro-decision__state">
        <div>
          <span>CURRENT FACT STATE</span>
          <h2>{module.summary.headline}</h2>
          <p>{module.summary.interpretation}</p>
        </div>
        <div>
          <span>最重要变化</span>
          <ChangeList changes={module.summary.top_changes.slice(0, 3)} />
        </div>
      </section>
      <MacroModuleSections module={module} />
      <section className="macro-decision__review-grid">
        <ReviewList title="矛盾与反证" items={module.contradictions} empty="暂未识别结构性矛盾。" />
        <ReviewList title="判断失效条件" items={module.falsifiers} empty="暂无预设失效条件。" />
        <ReviewList
          title="下一检查点"
          items={module.next_checkpoints.map(
            (item) =>
              `${textValue(item.label) ?? "数据检查"}：${textValue(item.next_check) ?? "按时钟检查"}`,
          )}
          empty="当前没有待补检查点。"
        />
        <CoverageSummary module={module} />
      </section>
      <EvidenceDetails module={module} />
    </>
  );
}

function CoverageSummary({ module }: { module: MacroTypedModuleReadData }) {
  const missing = module.status.coverage.capabilities.filter((item) => item.state !== "available");
  return (
    <article>
      <h2>覆盖缺口</h2>
      <ul>
        {missing.length ? (
          missing.map((item) => (
            <li key={item.capability_id}>
              {item.label}：{reasonLabel(item.reason ?? item.state)}
            </li>
          ))
        ) : (
          <li>Coverage Manifest 的预期能力已完整。</li>
        )}
      </ul>
    </article>
  );
}

function EvidenceDetails({ module }: { module: MacroTypedModuleReadData }) {
  return (
    <details className="macro-decision__evidence">
      <summary>展开 Coverage Manifest、Dataset 健康与原始事实</summary>
      <div className="macro-decision__coverage-list">
        {module.status.coverage.capabilities.map((capability) => (
          <article data-state={capability.state} key={capability.capability_id}>
            <strong>{capability.label}</strong>
            <span>{capability.state}</span>
            <small>{capability.requirement}</small>
          </article>
        ))}
      </div>
      <div className="macro-decision__dataset-list">
        {module.evidence.dataset_states.map((dataset) => (
          <article data-state={dataset.state} key={dataset.dataset_id}>
            <div>
              <strong>{dataset.label}</strong>
              <small>{dataset.dataset_id}</small>
            </div>
            <span>{dataset.state}</span>
            <time>{dataset.latest_reference ?? "尚无事实"}</time>
            <a href={dataset.source_url} rel="noreferrer" target="_blank">
              来源 <ExternalLink aria-hidden="true" />
            </a>
          </article>
        ))}
      </div>
      <div className="macro-decision__raw-evidence">
        <h3>当前原始事实</h3>
        {module.evidence.latest_facts.map((fact, index) => (
          <article key={fact.fact_ref ?? `${fact.dataset_id}-${index}`}>
            <strong>
              {fact.dataset_id}
              {fact.series_id ? ` / ${fact.series_id}` : ""}
            </strong>
            <span>{fact.reference ?? "—"}</span>
            <span>{fact.value == null ? "—" : String(fact.value)}</span>
            <a href={fact.source_url} rel="noreferrer" target="_blank">
              来源
            </a>
          </article>
        ))}
      </div>
    </details>
  );
}

function ChangeList({ changes, compact = false }: { changes: MacroChange[]; compact?: boolean }) {
  if (!changes.length) return <p className="macro-decision__muted">尚无足够历史计算变化。</p>;
  return (
    <div className="macro-decision__changes" data-compact={compact || undefined}>
      {changes.map((change) => (
        <article key={change.dataset_id}>
          <span>{change.label}</span>
          <strong>
            {formatNumber(change.value)} {unitLabel(change.unit)}
          </strong>
          <small>
            1周 {formatSigned(change.change_1w)} · 1月 {formatSigned(change.change_1m)}
          </small>
        </article>
      ))}
    </div>
  );
}

function ReviewList({ title, items, empty }: { title: string; items: string[]; empty: string }) {
  return (
    <article>
      <h2>{title}</h2>
      <ul>{items.length ? items.map((item) => <li key={item}>{item}</li>) : <li>{empty}</li>}</ul>
    </article>
  );
}

function ResearchStrip({ research }: { research: MacroOverviewReadData["research"] }) {
  return (
    <section className="macro-decision__research">
      <div>
        <span>ASYNC DEEP RESEARCH</span>
        <h2>{research.title ?? researchStateLabel(research.state)}</h2>
        <p>{research.executive_summary ?? "研究链独立运行，失败不会影响事实与每日判断。"}</p>
      </div>
      <dl>
        <div>
          <dt>Evidence Pack</dt>
          <dd>{research.evidence_pack_id ?? "尚未绑定"}</dd>
        </div>
        <div>
          <dt>Reviewer</dt>
          <dd>{research.reviewer_disposition ?? "尚未审阅"}</dd>
        </div>
      </dl>
      <Link to={research.href}>
        查看深度研究 <ArrowRight aria-hidden="true" />
      </Link>
    </section>
  );
}

function coverageLabel(value: MacroCoverageState) {
  return { complete: "完整", partial: "部分", licensed_unavailable: "授权缺失" }[value];
}

function healthLabel(value: MacroDataHealthState) {
  return {
    current: "当前",
    delayed: "延迟",
    stale: "陈旧",
    invalid: "无效",
    backfilling: "回填中",
    unavailable: "不可用",
  }[value];
}

function judgmentLabel(value: MacroJudgmentState) {
  return { current: "已发布", missing: "未发布", blocked: "阻塞" }[value];
}

function researchStateLabel(state: MacroOverviewReadData["research"]["state"]) {
  return {
    current: "研究已发布",
    generating: "研究生成中",
    failed: "研究失败",
    missing: "尚无研究",
  }[state];
}

function dimensionLabel(value: string) {
  return (
    {
      growth: "增长",
      inflation: "通胀",
      policy: "政策",
      liquidity: "流动性",
      credit: "信用",
      volatility: "波动率",
    }[value] ?? value
  );
}

function reasonLabel(value: string) {
  return (
    {
      licensed_contract_facts_not_configured: "缺少合规授权的合约事实",
      licensed_security_level_facts_not_configured: "缺少合规逐笔与 NAV 数据",
      ice_bofa_history_before_public_three_year_window_unavailable:
        "FRED 仅公开最近三年 ICE BofA 数据；更早历史需要授权",
    }[value] ?? value
  );
}

function textValue(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function formatInstant(value: number | null): string {
  if (!value) return "尚未发布";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

function formatNumber(value: number): string {
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(value);
}

function formatSigned(value: number | null): string {
  if (value == null) return "—";
  return `${value > 0 ? "+" : ""}${formatNumber(value)}`;
}

function unitLabel(unit: string) {
  return (
    {
      percent: "%",
      basis_points: "bp",
      billions_usd: "十亿美元",
      millions_usd: "百万美元",
      index: "点",
      index_points: "点",
      percent_open_interest: "% OI",
      price: "",
      usdt: "USDT",
      usd_per_barrel: "美元/桶",
      persons: "人",
      thousands_persons: "千人",
    }[unit] ?? unit
  );
}
