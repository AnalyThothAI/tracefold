import * as PageState from "@shared/ui/PageState";
import { Button } from "@shared/ui/button";
import { ArrowRight, ExternalLink, RefreshCw, ShieldAlert } from "lucide-react";
import { Link, useLocation, useNavigate } from "react-router-dom";

import { useMacroModuleQuery, useMacroOverviewQuery } from "../api/useMacroDecisionQuery";
import type {
  MacroChange,
  MacroCondition,
  MacroCoverageState,
  MacroCurrentHealthState,
  MacroHistoryDepthState,
  MacroModuleId,
  MacroOverviewReadData,
  MacroThesisState,
  MacroThesisV1,
  MacroTypedModuleReadData,
} from "../model/macroTypes";

import { MacroModuleSections } from "./MacroModuleSections";

import "./MacroDecisionOverview.css";
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
    return <PageState.Loading label="加载宏观主线" layout="route" rows={8} />;
  }
  const data = query.data;
  const transportStale = query.isError || data.transport.state === "stale";
  return (
    <PageState.Stale updating={query.isFetching && !query.isLoading}>
      <main aria-label="每日宏观主线" className="macro-decision" data-page-archetype="decision">
        <DecisionHeader
          coverage={data.data_quality.coverage_state}
          currentHealth={data.data_quality.current_health_state}
          cutoffMs={data.cutoff_ms}
          historyDepth={data.data_quality.history_depth_state}
          isFetching={query.isFetching}
          latestFactAtMs={data.latest_fact_at_ms}
          sessionDate={data.session_date}
          thesisState={data.thesis_state}
          title="每日宏观主线"
          transportStale={transportStale}
          onRefresh={() => void query.refetch()}
        />
        <MacroNavigation />
        <Overview data={data} />
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
  const module = query.data;
  return (
    <PageState.Stale updating={query.isFetching && !query.isLoading}>
      <main aria-label={module.label} className="macro-decision" data-page-archetype="decision">
        <DecisionHeader
          coverage={module.status.coverage.state}
          currentHealth={module.status.current_health.state}
          cutoffMs={null}
          historyDepth={module.status.history_depth.state}
          isFetching={query.isFetching}
          latestFactAtMs={module.latest_fact_at_ms}
          thesisState={null}
          title={module.label}
          transportStale={query.isError}
          onRefresh={() => void query.refetch()}
        />
        <MacroNavigation activeModule={moduleId} />
        <ModuleDetail module={module} />
      </main>
    </PageState.Stale>
  );
}

function DecisionHeader({
  title,
  coverage,
  currentHealth,
  historyDepth,
  thesisState,
  cutoffMs,
  sessionDate,
  latestFactAtMs,
  transportStale,
  isFetching,
  onRefresh,
}: {
  title: string;
  coverage: MacroCoverageState;
  currentHealth: MacroCurrentHealthState;
  historyDepth: MacroHistoryDepthState;
  thesisState: MacroThesisState | null;
  cutoffMs: number | null;
  sessionDate?: string;
  latestFactAtMs: number;
  transportStale: boolean;
  isFetching: boolean;
  onRefresh: () => void;
}) {
  return (
    <header className="macro-decision__header">
      <div>
        <span>MACRO THESIS · 08:50 NEW YORK</span>
        <h1>{title}</h1>
        <p>市场主线、确定性实时变化与六个事实模块来自同一条持久化链路。</p>
      </div>
      <StatusStrip
        coverage={coverage}
        currentHealth={currentHealth}
        historyDepth={historyDepth}
        thesisState={thesisState}
      />
      <dl>
        {sessionDate ? (
          <div>
            <dt>Session</dt>
            <dd>{sessionDate}</dd>
          </div>
        ) : null}
        {cutoffMs ? (
          <div>
            <dt>Thesis 截点</dt>
            <dd>{formatInstant(cutoffMs)}</dd>
          </div>
        ) : null}
        <div>
          <dt>最新事实</dt>
          <dd>{formatInstant(latestFactAtMs || null)}</dd>
        </div>
        <div>
          <dt>传输</dt>
          <dd>{transportStale ? "陈旧缓存" : "当前读取"}</dd>
        </div>
      </dl>
      <Button disabled={isFetching} onClick={onRefresh} size="sm" type="button" variant="outline">
        <RefreshCw aria-hidden="true" />
        {isFetching ? "刷新中" : "刷新"}
      </Button>
    </header>
  );
}

function StatusStrip({
  coverage,
  currentHealth,
  historyDepth,
  thesisState,
}: {
  coverage: MacroCoverageState;
  currentHealth: MacroCurrentHealthState;
  historyDepth: MacroHistoryDepthState;
  thesisState: MacroThesisState | null;
}) {
  const issues: Array<{ key: string; state: string; label: string }> = [];
  if (coverage !== "complete") {
    issues.push({ key: "coverage", state: coverage, label: `覆盖 ${coverageLabel(coverage)}` });
  }
  if (currentHealth !== "current") {
    issues.push({
      key: "current-health",
      state: currentHealth,
      label: `当前 ${currentHealthLabel(currentHealth)}`,
    });
  }
  if (historyDepth !== "complete" && historyDepth !== "not_required") {
    issues.push({
      key: "history-depth",
      state: historyDepth,
      label: `历史 ${historyDepthLabel(historyDepth)}`,
    });
  }
  if (thesisState && thesisState !== "published") {
    issues.push({
      key: "thesis",
      state: thesisState,
      label: `主线 ${thesisStateLabel(thesisState)}`,
    });
  }
  if (!issues.length) return null;
  return (
    <div aria-label="影响当前判断的异常状态" className="macro-decision__status-triplet">
      {issues.map((issue) => (
        <span data-state={issue.state} key={issue.key}>
          {issue.label}
        </span>
      ))}
    </div>
  );
}

function MacroNavigation({ activeModule }: { activeModule?: MacroModuleId }) {
  const location = useLocation();
  const navigate = useNavigate();
  return (
    <div className="macro-decision__nav-shell">
      <nav aria-label="宏观决策模块" className="macro-decision__nav">
        <Link aria-current={activeModule ? undefined : "page"} to="/macro">
          主线总览
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
        <Link to="/macro/research">主线档案</Link>
      </nav>
      <label className="macro-decision__mobile-nav">
        <span>宏观模块</span>
        <select
          aria-label="当前宏观模块"
          onChange={(event) => navigate(event.target.value)}
          value={location.pathname}
        >
          <option value="/macro">主线总览</option>
          {MODULE_ROUTES.map((route) => (
            <option key={route.id} value={route.path}>
              {route.label}
            </option>
          ))}
          <option value="/macro/research">主线档案</option>
        </select>
      </label>
    </div>
  );
}

function Overview({ data }: { data: MacroOverviewReadData }) {
  if (!data.thesis) {
    return (
      <>
        <section className="macro-decision__notice">
          <ShieldAlert aria-hidden="true" />
          <div>
            <h2>{data.session_date} Macro Thesis 尚未发布</h2>
            <p>
              当前状态为 {thesisStateLabel(data.thesis_state)}
              ；页面不会用前一交易日结论填充当前主线。
            </p>
          </div>
        </section>
        <ModuleRoleGrid data={data} />
        <DataQualityPanel data={data} />
      </>
    );
  }
  return (
    <>
      <MainlinePanel thesis={data.thesis} />
      <TensionsPanel thesis={data.thesis} />
      <ChangesPanel thesis={data.thesis} />
      <AssetViews thesis={data.thesis} />
      <DeltaAndConditions data={data} />
      <ModuleRoleGrid data={data} />
      <DataQualityPanel data={data} />
    </>
  );
}

function MainlinePanel({ thesis }: { thesis: MacroThesisV1 }) {
  return (
    <section className="macro-decision__judgment">
      <header>
        <div>
          <span>
            MAINLINE · {thesis.session_date} · {thesis.mainline.horizon}
          </span>
          <h2>{thesis.mainline.title}</h2>
          <p>{thesis.mainline.thesis}</p>
        </div>
        <small>
          {thesis.mainline.stance} · {thesis.mainline.stage} · 置信度 {thesis.mainline.confidence} ·
          独立审阅 {thesis.review.disposition}
        </small>
      </header>
      <div className="macro-decision__mainline-workbench">
        <div aria-label="宏观主线因果链" className="macro-decision__causal-chain">
          {thesis.mainline.claims.map((claim) => (
            <article key={claim.claim_id}>
              <header>
                <span>{claim.claim_id}</span>
                <strong>{claim.statement}</strong>
              </header>
              <div>
                {claim.causal_edges.map((edge) => (
                  <p key={`${claim.claim_id}:${edge.source}:${edge.target}`}>
                    <strong>{edge.source}</strong>
                    <ArrowRight aria-hidden="true" />
                    <span>{edge.mechanism}</span>
                    <ArrowRight aria-hidden="true" />
                    <strong>{edge.target}</strong>
                  </p>
                ))}
              </div>
            </article>
          ))}
        </div>
        <aside aria-label="主线有效性" className="macro-decision__validity-rail">
          <header>
            <span>VALIDITY</span>
            <strong>什么会确认或推翻主线</strong>
          </header>
          <article data-state="confirming">
            <span>支持证据</span>
            <strong>
              {thesis.mainline.claims.reduce(
                (total, claim) => total + claim.supporting_evidence_refs.length,
                0,
              )}{" "}
              条引用
            </strong>
          </article>
          <article data-state="invalidated">
            <span>失效条件</span>
            <strong>{thesis.mainline.falsifiers.length} 项</strong>
            <small>{thesis.mainline.falsifiers[0]?.rationale ?? "暂无预设失效条件"}</small>
          </article>
          <article>
            <span>下一检查点</span>
            <strong>{thesis.mainline.checkpoints.length} 项</strong>
            <small>{thesis.mainline.checkpoints[0]?.rationale ?? "按自然发布频率检查"}</small>
          </article>
          {thesis.alternative_explanation ? (
            <article data-state="weakening">
              <span>唯一备选路径</span>
              <strong>{thesis.alternative_explanation.title}</strong>
              <small>{thesis.alternative_explanation.thesis}</small>
            </article>
          ) : null}
        </aside>
      </div>
    </section>
  );
}

function TensionsPanel({ thesis }: { thesis: MacroThesisV1 }) {
  return (
    <section className="macro-decision__tensions">
      <header>
        <span>CORE TENSIONS</span>
        <h2>当前核心矛盾</h2>
      </header>
      {thesis.core_tensions.length ? (
        thesis.core_tensions.map((tension) => (
          <article key={tension.tension_id}>
            <h3>{tension.statement}</h3>
            <div className="macro-decision__tension-sides">
              <section data-leading={tension.leading_side === "side_a" || undefined}>
                <span>{tension.side_a.label}</span>
                <p>{tension.side_a.statement}</p>
              </section>
              <span aria-hidden="true">VS</span>
              <section data-leading={tension.leading_side === "side_b" || undefined}>
                <span>{tension.side_b.label}</span>
                <p>{tension.side_b.statement}</p>
              </section>
            </div>
            <footer>
              <strong>当前领先：{tension.leading_side}</strong>
              <span>滞后信号：{tension.lagging_signal}</span>
              <span>待解决：{tension.unresolved_reason}</span>
            </footer>
          </article>
        ))
      ) : (
        <p className="macro-decision__muted">本期没有额外矛盾。</p>
      )}
    </section>
  );
}

function ChangesPanel({ thesis }: { thesis: MacroThesisV1 }) {
  return (
    <section className="macro-decision__state">
      <div>
        <span>CHANGES FROM PRIOR THESIS</span>
        <h2>相较上一期最重要的变化</h2>
      </div>
      <ol>
        {thesis.changes_from_prior.length ? (
          thesis.changes_from_prior.map((change) => (
            <li key={change.change_id}>
              <strong>{change.status}</strong> · {change.statement}
            </li>
          ))
        ) : (
          <li>首期发布或没有足以改写主线的变化。</li>
        )}
      </ol>
    </section>
  );
}

function AssetViews({ thesis }: { thesis: MacroThesisV1 }) {
  return (
    <section className="macro-decision__assets">
      <header>
        <span>TWELVE ASSETS</span>
        <h2>十二资产：事实动量 vs 条件展望</h2>
        <p>表格只保留方向与收益；因果通道、证据和失效条件按资产展开。</p>
      </header>
      <div role="table">
        <div role="row">
          <span role="columnheader">资产</span>
          <span role="columnheader">1W 动量</span>
          <span role="columnheader">1W 展望</span>
          <span role="columnheader">1M 动量</span>
          <span role="columnheader">1M 展望</span>
          <span role="columnheader">因果条件</span>
        </div>
        {thesis.assets.map((asset) => (
          <div key={asset.symbol} role="row">
            <strong data-label="资产" role="cell">
              {asset.symbol}
            </strong>
            <span data-label="1W 动量" role="cell">
              {asset.momentum.momentum_1w} · {formatSigned(asset.momentum.return_1w_pct)}
            </span>
            <span data-direction={asset.outlook_1w.direction} data-label="1W 展望" role="cell">
              <strong>{asset.outlook_1w.direction}</strong>
            </span>
            <span data-label="1M 动量" role="cell">
              {asset.momentum.momentum_1m} · {formatSigned(asset.momentum.return_1m_pct)}
            </span>
            <span data-direction={asset.outlook_1m.direction} data-label="1M 展望" role="cell">
              <strong>{asset.outlook_1m.direction}</strong>
            </span>
            <details data-label="因果条件" role="cell">
              <summary>展开</summary>
              <p>
                <strong>1W：</strong>
                {asset.outlook_1w.causal_channel}
              </p>
              <p>
                <strong>1M：</strong>
                {asset.outlook_1m.causal_channel}
              </p>
              <small>
                1W 支持 {asset.outlook_1w.supporting_evidence_refs.length} / 冲突{" "}
                {asset.outlook_1w.conflicting_evidence_refs.length} · 1M 支持{" "}
                {asset.outlook_1m.supporting_evidence_refs.length} / 冲突{" "}
                {asset.outlook_1m.conflicting_evidence_refs.length}
              </small>
            </details>
          </div>
        ))}
      </div>
    </section>
  );
}

function DeltaAndConditions({ data }: { data: MacroOverviewReadData }) {
  const thesis = data.thesis!;
  return (
    <section className="macro-decision__review-grid">
      <article>
        <h2>Live Delta</h2>
        <strong>{data.live_delta ? liveDeltaLabel(data.live_delta.status) : "尚未评估"}</strong>
        {data.live_delta ? (
          <>
            <p>
              Claim {data.live_delta.matched_claim_ids.join(" · ") || "—"}；Falsifier{" "}
              {data.live_delta.matched_falsifier_ids.join(" · ") || "—"}；Checkpoint{" "}
              {data.live_delta.matched_checkpoint_ids.join(" · ") || "—"}
            </p>
            <ul>
              {data.live_delta.items.map((item) => (
                <li key={`${item.binding_type}:${item.condition_id}`}>
                  {item.binding_type} · {item.binding_id} · {item.status}
                </li>
              ))}
            </ul>
          </>
        ) : null}
      </article>
      <ConditionList title="失效条件" conditions={thesis.mainline.falsifiers} />
      <ConditionList title="下一检查点" conditions={thesis.mainline.checkpoints} />
      <article>
        <h2>Outcome Replay</h2>
        <ul>
          {data.outcome_replay?.horizons.map((horizon) => (
            <li key={horizon.horizon}>
              {horizon.horizon} · {horizon.status} ·{" "}
              {horizon.realized_return_pct == null
                ? "等待期限结束"
                : `${formatSigned(horizon.realized_return_pct)}%`}
            </li>
          )) ?? <li>尚未创建结果复盘。</li>}
        </ul>
      </article>
    </section>
  );
}

function ConditionList({ title, conditions }: { title: string; conditions: MacroCondition[] }) {
  return (
    <article>
      <h2>{title}</h2>
      <ul>
        {conditions.map((condition) => (
          <li key={condition.condition_id}>
            {condition.dataset_id}.{condition.metric_name} {condition.operator}{" "}
            {formatNumber(condition.threshold)}：{condition.rationale}
          </li>
        ))}
      </ul>
    </article>
  );
}

function ModuleRoleGrid({ data }: { data: MacroOverviewReadData }) {
  const roleAnalysis = new Map(
    (data.thesis?.module_assessments ?? []).map((item) => [item.module_id, item.analysis]),
  );
  return (
    <section aria-label="六个宏观模块" className="macro-decision__module-grid">
      {data.modules.map((module) => (
        <article className="macro-decision__module-card" key={module.module_id}>
          <StatusStrip
            coverage={module.coverage_state}
            currentHealth={module.current_health_state}
            historyDepth={module.history_depth_state}
            thesisState={null}
          />
          <span>{module.role ?? "尚未分配角色"}</span>
          <h2>{module.label}</h2>
          <p>{roleAnalysis.get(module.module_id) ?? module.summary.interpretation}</p>
          <ChangeList changes={module.summary.top_changes.slice(0, 2)} compact />
          <small>
            覆盖 {module.coverage_gap_count} · 当前 {module.current_health_gap_count} · 历史{" "}
            {module.history_gap_count}
          </small>
          <Link to={module.href}>
            打开模块 <ArrowRight aria-hidden="true" />
          </Link>
        </article>
      ))}
    </section>
  );
}

function DataQualityPanel({ data }: { data: MacroOverviewReadData }) {
  const quality = data.data_quality;
  return (
    <section className="macro-decision__research">
      <div>
        <span>DATA QUALITY</span>
        <h2>数据质量与真实缺口</h2>
        <p>Coverage、Current Health 与 History Depth 独立计算；历史回填不降级当前数据。</p>
      </div>
      <dl>
        <div>
          <dt>覆盖</dt>
          <dd>
            {coverageLabel(quality.coverage_state)} · {quality.coverage_gap_count} 缺口
          </dd>
        </div>
        <div>
          <dt>当前</dt>
          <dd>
            {currentHealthLabel(quality.current_health_state)} · {quality.current_health_gap_count}{" "}
            异常
          </dd>
        </div>
        <div>
          <dt>历史</dt>
          <dd>
            {historyDepthLabel(quality.history_depth_state)} · {quality.history_gap_count} 缺口
          </dd>
        </div>
      </dl>
      {data.thesis?.gaps.length ? (
        <details>
          <summary>影响判断的数据缺口（{data.thesis.gaps.length}）</summary>
          <ul>
            {data.thesis.gaps.map((gap) => (
              <li key={gap.gap_id}>
                {gap.module_id} · {gap.dataset_id} · {gap.axis}/{gap.state}：{gap.reason}
                {gap.affected_claim_ids.length ? `；影响 ${gap.affected_claim_ids.join("、")}` : ""}
              </li>
            ))}
          </ul>
        </details>
      ) : null}
      <Link to="/macro/research">
        查看主线档案 <ArrowRight aria-hidden="true" />
      </Link>
    </section>
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
          <span>自然频率下的最重要变化</span>
          <ChangeList changes={module.summary.top_changes.slice(0, 3)} />
        </div>
      </section>
      <MacroModuleSections module={module} />
      <section className="macro-decision__review-grid">
        <ReviewList title="矛盾" items={module.contradictions} empty="暂未识别结构性矛盾。" />
        <ReviewList title="失效条件" items={module.falsifiers} empty="暂无预设失效条件。" />
        <ReviewList
          title="下一检查点"
          items={module.next_checkpoints.map(
            (item) => `${item.label}：${item.next_check}（当前 ${item.current_health}）`,
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
              {item.label}：{item.reason ?? item.state}
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
      <summary>展开 Coverage、Current Health、History Depth 与原始事实</summary>
      <div className="macro-decision__coverage-list">
        {module.status.coverage.capabilities.map((capability) => (
          <article data-state={capability.state} key={capability.capability_id}>
            <strong>{capability.label}</strong>
            <span>{capability.state}</span>
            <small>{capability.requirement}</small>
          </article>
        ))}
      </div>
      <div aria-label="分组数据健康" className="macro-decision__coverage-list">
        {module.status.current_health.groups.map((group) => (
          <article data-state={group.current_health} key={group.group_id}>
            <strong>{group.label}</strong>
            <span>
              {currentHealthLabel(
                group.current_health === "mixed" ? "degraded" : group.current_health,
              )}{" "}
              · {marketStateLabel(group.market_state)} · {sourceStateLabel(group.source_state)}
            </span>
            <small>
              {group.current_datasets}/{group.tracked_datasets} 当前
            </small>
          </article>
        ))}
      </div>
      <div className="macro-decision__dataset-list">
        {module.evidence.dataset_states.map((dataset) => (
          <article data-state={dataset.current_health} key={dataset.dataset_id}>
            <div>
              <strong>{dataset.label}</strong>
              <small>
                {dataset.dataset_id} · {dataset.concept_id} · {dataset.source_role}
              </small>
            </div>
            <span>
              当前 {currentHealthLabel(dataset.current_health)} · 历史{" "}
              {historyDepthLabel(dataset.history_depth)} · {marketStateLabel(dataset.market_state)}{" "}
              · {sourceStateLabel(dataset.source_state)}
            </span>
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
            {change.cadence} · {formatMetrics(change.metrics, change.metric_unit)}
          </small>
          <small>{change.importance_explanation}</small>
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

function coverageLabel(value: MacroCoverageState) {
  return { complete: "完整", partial: "部分" }[value];
}

function currentHealthLabel(value: MacroCurrentHealthState) {
  return { current: "当前", degraded: "降级", unavailable: "不可用" }[value];
}

function historyDepthLabel(value: MacroHistoryDepthState) {
  return {
    complete: "完整",
    insufficient: "不足",
    not_required: "不要求",
    partial: "部分",
  }[value];
}

function thesisStateLabel(value: MacroThesisState) {
  return {
    config_error: "配置错误",
    failed: "失败",
    missing: "缺失",
    not_published: "审阅未通过，未发布",
    pending: "待运行",
    published: "已发布",
    retryable: "等待重试",
    running: "生成中",
  }[value];
}

function liveDeltaLabel(value: NonNullable<MacroOverviewReadData["live_delta"]>["status"]) {
  return {
    confirming: "确认主线",
    insufficient: "证据不足",
    invalidation_triggered: "失效条件已触发",
    unrelated: "与主线无关",
    weakening: "主线走弱",
  }[value];
}

function marketStateLabel(value: string) {
  return (
    {
      closed: "闭市",
      maintenance: "维护",
      mixed: "混合",
      not_applicable: "非市场时钟",
      open: "开市",
      unknown: "市场时钟未知",
    }[value] ?? value
  );
}

function sourceStateLabel(value: string) {
  return (
    {
      degraded: "来源降级",
      failed: "来源失败",
      healthy: "来源正常",
      mixed: "混合",
      not_applicable: "不适用",
    }[value] ?? value
  );
}

function formatInstant(value: number | null): string {
  if (!value) return "尚无事实";
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "short",
    hour12: false,
    timeStyle: "short",
  }).format(new Date(value));
}

function formatNumber(value: number): string {
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(value);
}

function formatSigned(value: number | null): string {
  if (value == null) return "—";
  return `${value > 0 ? "+" : ""}${formatNumber(value)}`;
}

function formatMetrics(metrics: Record<string, number | null>, unit: string): string {
  const values = Object.entries(metrics)
    .filter(([, value]) => value != null)
    .map(([key, value]) => `${key} ${formatSigned(value)}${unitLabel(unit)}`);
  return values.length ? values.join(" · ") : "变化不足";
}

function unitLabel(unit: string) {
  return (
    {
      basis_points: "bp",
      billions_usd: "十亿美元",
      index: "点",
      index_points: "点",
      millions_usd: "百万美元",
      percent: "%",
      percent_open_interest: "% OI",
      persons: "人",
      price: "",
      thousands_persons: "千人",
      usd_per_barrel: "美元/桶",
      usdt: "USDT",
    }[unit] ?? unit
  );
}
