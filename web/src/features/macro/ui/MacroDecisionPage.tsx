import * as PageState from "@shared/ui/PageState";
import { Button } from "@shared/ui/button";
import {
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  Clock3,
  ExternalLink,
  PauseCircle,
  RefreshCw,
  ShieldAlert,
  XCircle,
} from "lucide-react";
import { useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";

import { useMacroModuleQuery, useMacroOverviewQuery } from "../api/useMacroDecisionQuery";
import {
  assetDirectionLabel,
  changeStatusLabel,
  conditionEffectLabel,
  confidenceLabel,
  formatInstant,
  formatNumber,
  formatSigned,
  gapAxisLabel,
  horizonLabel,
  leadingSideLabel,
  liveDeltaLabel,
  moduleLabel,
  moduleRoleLabel,
  momentumLabel,
  operatorLabel,
  outcomeStatusLabel,
  stageLabel,
  stanceLabel,
} from "../model/macroPresentation";
import type {
  MacroAssetPresentation,
  MacroBackfillExecution,
  MacroChange,
  MacroCondition,
  MacroCoverageState,
  MacroCurrentHealthState,
  MacroHistoryDepthState,
  JsonObject,
  MacroModuleId,
  MacroModuleUnavailableReadData,
  MacroOverviewReadData,
  MacroReason,
  MacroThesisState,
  MacroThesisV1,
  MacroTypedModuleReadData,
} from "../model/macroTypes";
import { asRecords, readNumber, readText } from "../model/macroViewModels";

import { MacroModuleSections } from "./MacroModuleSections";

import "./MacroDecisionBrief.css";
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

type DecisionStatusTone = "positive" | "caution" | "negative" | "neutral";

function DecisionStatusGlyph({ tone }: { tone: DecisionStatusTone }) {
  return (
    <span aria-hidden="true" className="macro-decision__status-glyph" data-tone={tone}>
      {tone === "positive" ? "✓" : tone === "negative" ? "!" : tone === "caution" ? "◆" : "•"}
    </span>
  );
}

function assetDirectionTone(
  value: MacroAssetPresentation["horizons"][number]["outlook_direction"],
): DecisionStatusTone {
  if (value === "bullish") return "positive";
  if (value === "bearish") return "negative";
  if (value === "no_call") return "caution";
  return "neutral";
}

function liveDeltaTone(
  value: NonNullable<MacroOverviewReadData["live_delta"]>["mainline_validity"],
): DecisionStatusTone {
  if (value === "confirming") return "positive";
  if (value === "invalidation_triggered") return "negative";
  if (value === "weakening" || value === "insufficient") return "caution";
  return "neutral";
}

function outcomeTone(
  value: NonNullable<MacroOverviewReadData["outcome_replay"]>["horizons"][number]["status"],
): DecisionStatusTone {
  if (value === "evaluated") return "positive";
  if (value === "insufficient") return "negative";
  return "caution";
}

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
          cutoffMs={data.thesis?.cutoff_ms ?? data.cutoff_ms}
          displayedSessionDate={data.displayed_session_date}
          historyDepth={data.data_quality.history_depth_state}
          isFetching={query.isFetching}
          latestFactAtMs={data.latest_fact_at_ms}
          lastSuccessfulReadAtMs={data.transport.last_successful_read_at_ms}
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
  if (module.availability === "unavailable") {
    return <UnavailableModule module={module} moduleId={moduleId} />;
  }
  const datasetLabels = new Map(
    module.evidence.dataset_states.map((dataset) => [dataset.dataset_id, dataset.label]),
  );
  return (
    <PageState.Stale updating={query.isFetching && !query.isLoading}>
      <main aria-label={module.label} className="macro-decision" data-page-archetype="decision">
        <DecisionHeader
          coverage={module.status.coverage.state}
          currentHealth={module.status.current_health.state}
          cutoffMs={module.thesis_context.cutoff_ms}
          historyDepth={module.status.history_depth.state}
          isFetching={query.isFetching}
          latestFactAtMs={module.latest_fact_at_ms}
          lastSuccessfulReadAtMs={query.dataUpdatedAt || null}
          thesisState={null}
          title={module.label}
          transportStale={query.isError}
          onRefresh={() => void query.refetch()}
        />
        <MacroNavigation activeModule={moduleId} />
        {module.reason ? (
          <ReasonPanel
            datasetLabels={datasetLabels}
            reason={module.reason}
            title="模块证据状态需要注意"
          />
        ) : null}
        <ModuleDetail module={module} />
      </main>
    </PageState.Stale>
  );
}

function UnavailableModule({
  module,
  moduleId,
}: {
  module: MacroModuleUnavailableReadData;
  moduleId: MacroModuleId;
}) {
  return (
    <main aria-label={module.label} className="macro-decision" data-page-archetype="decision">
      <header className="macro-decision__header">
        <div>
          <span>MACRO EVIDENCE WORKBENCH</span>
          <h1>{module.label}</h1>
          <p>该模块暂不可用；其他模块、主线总览与已发布档案不受影响。</p>
        </div>
      </header>
      <MacroNavigation activeModule={moduleId} />
      <ReasonPanel reason={module.reason} title="模块证据暂不可用" />
    </main>
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
  displayedSessionDate,
  latestFactAtMs,
  lastSuccessfulReadAtMs,
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
  displayedSessionDate?: string | null;
  latestFactAtMs: number;
  lastSuccessfulReadAtMs?: number | null;
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
            <dt>请求交易日</dt>
            <dd>{sessionDate}</dd>
          </div>
        ) : null}
        {displayedSessionDate && displayedSessionDate !== sessionDate ? (
          <div>
            <dt>实际展示</dt>
            <dd>{displayedSessionDate}</dd>
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
        {transportStale && lastSuccessfulReadAtMs ? (
          <div>
            <dt>最后成功读取</dt>
            <dd>{formatInstant(lastSuccessfulReadAtMs)}</dd>
          </div>
        ) : null}
      </dl>
      <Button disabled={isFetching} onClick={onRefresh} size="sm" type="button" variant="outline">
        <RefreshCw aria-hidden="true" />
        {isFetching ? "刷新中" : "刷新"}
      </Button>
    </header>
  );
}

function ReasonPanel({
  reason,
  title,
  datasetLabels,
}: {
  reason: MacroReason;
  title: string;
  datasetLabels?: ReadonlyMap<string, string>;
}) {
  return (
    <section className="macro-decision__notice" data-impact={reason.impact}>
      <ShieldAlert aria-hidden="true" />
      <div>
        <h2>{title}</h2>
        <ReasonDetails datasetLabels={datasetLabels} reason={reason} />
      </div>
    </section>
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
          <StatusIssueIcon state={issue.state} />
          {issue.label}
        </span>
      ))}
    </div>
  );
}

function StatusIssueIcon({ state }: { state: string }) {
  if (["config_error", "failed", "not_published", "unavailable"].includes(state)) {
    return <XCircle aria-hidden="true" />;
  }
  return <AlertTriangle aria-hidden="true" />;
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
        {data.thesis_reason ? (
          <ReasonPanel
            reason={data.thesis_reason}
            title={`${data.session_date} Macro Thesis ${thesisStateLabel(data.thesis_state)}`}
          />
        ) : (
          <section className="macro-decision__notice">
            <ShieldAlert aria-hidden="true" />
            <div>
              <h2>{data.session_date} Macro Thesis 尚未发布</h2>
              <p>当前状态为 {thesisStateLabel(data.thesis_state)}。</p>
            </div>
          </section>
        )}
        <ModuleEvidenceIndex data={data} />
        <DataQualityPanel data={data} />
      </>
    );
  }
  return (
    <>
      {data.fallback.state === "available" ? <FallbackBoundary data={data} /> : null}
      <MainlinePanel liveDelta={data.live_delta} thesis={data.thesis} />
      <TensionsPanel thesis={data.thesis} />
      <ChangesPanel thesis={data.thesis} />
      <AssetViews assets={data.asset_presentation} thesis={data.thesis} />
      <DeltaAndConditions data={data} />
      <ModuleEvidenceIndex data={data} />
      <DataQualityPanel data={data} />
    </>
  );
}

function FallbackBoundary({ data }: { data: MacroOverviewReadData }) {
  const requestedReason = data.thesis_reason ?? data.fallback.reason;
  return (
    <section className="macro-decision__notice" data-impact={data.fallback.reason.impact}>
      <ShieldAlert aria-hidden="true" />
      <div>
        <h2>
          请求交易日 {data.session_date} 尚未发布，当前展示最近已发布主线{" "}
          {data.displayed_session_date}
        </h2>
        <p>{requestedReason.message}</p>
        <p>
          历史主线截点：
          {formatInstant(data.fallback.cutoff_ms)}
        </p>
        {requestedReason.next_action ? (
          <p>
            <strong>恢复动作：</strong>
            {requestedReason.next_action}
          </p>
        ) : null}
      </div>
    </section>
  );
}

function MainlinePanel({
  thesis,
  liveDelta,
}: {
  thesis: MacroThesisV1;
  liveDelta: MacroOverviewReadData["live_delta"];
}) {
  return (
    <section className="macro-decision__judgment">
      <header>
        <div>
          <span>
            MAINLINE · {thesis.session_date} · {horizonLabel(thesis.mainline.horizon)}
          </span>
          <h2>{thesis.mainline.title}</h2>
          <p>{thesis.mainline.thesis}</p>
        </div>
        <small>
          {stanceLabel(thesis.mainline.stance)} · {stageLabel(thesis.mainline.stage)} ·{" "}
          {confidenceLabel(thesis.mainline.confidence)} · 独立审阅通过
        </small>
      </header>
      <div className="macro-decision__mainline-workbench">
        <div aria-label="宏观主线因果链" className="macro-decision__causal-chain">
          {thesis.mainline.claims.map((claim, index) => (
            <article key={claim.claim_id}>
              <header>
                <span>论点 {index + 1}</span>
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
              <CitationPreview
                label="论点最强支持证据"
                refs={claim.supporting_evidence_refs}
                thesis={thesis}
              />
            </article>
          ))}
        </div>
        <aside aria-label="主线有效性" className="macro-decision__validity-rail">
          <header>
            <span>VALIDITY</span>
            <strong
              className="macro-decision__status-label"
              data-tone={liveDelta ? liveDeltaTone(liveDelta.mainline_validity) : "neutral"}
            >
              <DecisionStatusGlyph
                tone={liveDelta ? liveDeltaTone(liveDelta.mainline_validity) : "neutral"}
              />
              {liveDelta ? liveDeltaLabel(liveDelta.mainline_validity) : "等待新增事实评估"}
            </strong>
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
          {thesis.mainline.falsifiers.length ? (
            <article data-state="invalidated">
              <span>失效条件</span>
              <strong>{thesis.mainline.falsifiers.length} 项</strong>
              <ConditionThresholdSummary condition={thesis.mainline.falsifiers[0]!} />
            </article>
          ) : null}
          {thesis.mainline.checkpoints.length ? (
            <article>
              <span>下一检查点</span>
              <strong>{thesis.mainline.checkpoints.length} 项</strong>
              <ConditionThresholdSummary condition={thesis.mainline.checkpoints[0]!} />
            </article>
          ) : null}
          {thesis.alternative_explanation ? (
            <article data-state="weakening">
              <span>唯一备选路径</span>
              <strong>{thesis.alternative_explanation.title}</strong>
              <small>{thesis.alternative_explanation.thesis}</small>
              <InlineCitationLabels
                label="支持"
                refs={thesis.alternative_explanation.supporting_evidence_refs}
                thesis={thesis}
              />
              <InlineCitationLabels
                label="反证"
                refs={thesis.alternative_explanation.conflicting_evidence_refs}
                thesis={thesis}
              />
            </article>
          ) : null}
        </aside>
      </div>
    </section>
  );
}

function CitationPreview({
  refs,
  thesis,
  label,
  limit = 3,
}: {
  refs: string[];
  thesis: MacroThesisV1;
  label: string;
  limit?: number;
}) {
  const citations = new Map(thesis.citations.map((citation) => [citation.evidence_ref, citation]));
  const evidence = refs
    .map((ref) => citations.get(ref))
    .filter((citation): citation is MacroThesisV1["citations"][number] => citation != null)
    .slice(0, limit);
  if (!evidence.length) return null;
  return (
    <ul aria-label={label} className="macro-decision__claim-evidence-preview">
      {evidence.map((citation) => (
        <li key={citation.evidence_ref}>
          <div>
            <strong>{citation.label}</strong>
            <span>
              {citation.reference ?? "当前证据"} ·{" "}
              {citation.received_at_ms ? formatInstant(citation.received_at_ms) : "未提供接收时间"}
            </span>
          </div>
          {citation.source_url ? (
            <a href={citation.source_url} rel="noreferrer" target="_blank">
              来源 <ExternalLink aria-hidden="true" />
            </a>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

function InlineCitationLabels({
  refs,
  thesis,
  label,
}: {
  refs: string[];
  thesis: MacroThesisV1;
  label: string;
}) {
  const citations = new Map(thesis.citations.map((citation) => [citation.evidence_ref, citation]));
  const labels = refs
    .map((ref) => citations.get(ref)?.label)
    .filter((value): value is string => Boolean(value))
    .slice(0, 2);
  if (!labels.length) return null;
  return (
    <small>
      {label} {refs.length} 条：{labels.join("、")}
    </small>
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
                <CitationPreview
                  label={`${tension.side_a.label}证据`}
                  limit={2}
                  refs={tension.side_a.evidence_refs}
                  thesis={thesis}
                />
              </section>
              <span aria-hidden="true">VS</span>
              <section data-leading={tension.leading_side === "side_b" || undefined}>
                <span>{tension.side_b.label}</span>
                <p>{tension.side_b.statement}</p>
                <CitationPreview
                  label={`${tension.side_b.label}证据`}
                  limit={2}
                  refs={tension.side_b.evidence_refs}
                  thesis={thesis}
                />
              </section>
            </div>
            <footer>
              <strong>
                当前领先：
                {leadingSideLabel(tension.leading_side, tension.side_a.label, tension.side_b.label)}
              </strong>
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
              <strong>{changeStatusLabel(change.status)}</strong> · {change.statement}
            </li>
          ))
        ) : (
          <li>首期发布或没有足以改写主线的变化。</li>
        )}
      </ol>
    </section>
  );
}

function AssetViews({
  assets,
  thesis,
}: {
  assets: MacroAssetPresentation[];
  thesis: MacroThesisV1;
}) {
  const groups = [
    {
      id: "actionable" as const,
      label: "Actionable",
      description: "方向、因果条件与证据足以形成条件判断。",
    },
    {
      id: "watch" as const,
      label: "Watch",
      description: "已有方向线索，但仍需等待确认或矛盾消解。",
    },
    {
      id: "evidence_gap" as const,
      label: "Evidence gap",
      description: "证据不足；原因、影响与恢复动作按 horizon 展示。",
    },
  ];
  return (
    <section className="macro-decision__assets">
      <header>
        <span>TWELVE ASSETS</span>
        <h2>十二资产：事实动量 vs 条件展望</h2>
        <p>表格只保留方向与收益；因果通道、证据和失效条件按资产展开。</p>
      </header>
      <div className="macro-decision__asset-groups">
        {groups.map((group) => {
          const rows = assets.filter((asset) => asset.group === group.id);
          return (
            <section data-group={group.id} key={group.id}>
              <header>
                <div>
                  <h3>{group.label}</h3>
                  <p>{group.description}</p>
                </div>
                <strong>{rows.length}</strong>
              </header>
              {rows.length ? (
                <div aria-label={`${group.label} 资产`} role="table">
                  <div role="row">
                    <span role="columnheader">资产</span>
                    <span role="columnheader">1W 动量</span>
                    <span role="columnheader">1W 展望</span>
                    <span role="columnheader">1M 动量</span>
                    <span role="columnheader">1M 展望</span>
                    <span role="columnheader">原因与条件</span>
                  </div>
                  {rows.map((asset) => (
                    <AssetRow asset={asset} key={asset.symbol} thesis={thesis} />
                  ))}
                </div>
              ) : (
                <p>本期没有资产归入该组。</p>
              )}
            </section>
          );
        })}
      </div>
    </section>
  );
}

function AssetRow({ asset, thesis }: { asset: MacroAssetPresentation; thesis: MacroThesisV1 }) {
  const [hasOpened, setHasOpened] = useState(false);
  const oneWeek = asset.horizons[0];
  const oneMonth = asset.horizons[1];
  return (
    <div data-asset-group={asset.group} role="row">
      <strong data-label="资产" role="cell">
        {asset.symbol}
      </strong>
      <span data-label="1W 动量" role="cell">
        {momentumLabel(oneWeek.momentum_state)} · {formatSigned(oneWeek.momentum_value ?? null)}
      </span>
      <HorizonCell group={asset.group} horizon={oneWeek} label="1W 展望" />
      <span data-label="1M 动量" role="cell">
        {momentumLabel(oneMonth.momentum_state)} · {formatSigned(oneMonth.momentum_value ?? null)}
      </span>
      <HorizonCell group={asset.group} horizon={oneMonth} label="1M 展望" />
      <div data-label="原因与条件" role="cell">
        <details
          onToggle={(event) => {
            if (event.currentTarget.open) setHasOpened(true);
          }}
        >
          <summary>查看原因与条件</summary>
          {hasOpened ? (
            <>
              <AssetHorizonReason asset={asset} horizon={oneWeek} thesis={thesis} />
              <AssetHorizonReason asset={asset} horizon={oneMonth} thesis={thesis} />
            </>
          ) : null}
        </details>
      </div>
    </div>
  );
}

function HorizonCell({
  group,
  horizon,
  label,
}: {
  group: MacroAssetPresentation["group"];
  horizon: MacroAssetPresentation["horizons"][number];
  label: string;
}) {
  const trigger = horizon.confirmation_triggers[0];
  const falsifier = horizon.falsifiers[0];
  return (
    <span
      className="macro-decision__asset-outlook"
      data-direction={horizon.outlook_direction}
      data-label={label}
      role="cell"
    >
      <strong
        className="macro-decision__status-label"
        data-tone={assetDirectionTone(horizon.outlook_direction)}
      >
        <DecisionStatusGlyph tone={assetDirectionTone(horizon.outlook_direction)} />
        {assetDirectionLabel(horizon.outlook_direction)}
      </strong>
      <small>{confidenceLabel(horizon.confidence)}</small>
      {group === "actionable" ? (
        <small className="macro-decision__asset-cause">理由：{horizon.causal_channel}</small>
      ) : null}
      {horizon.reason ? <small>{horizon.reason.message}</small> : null}
      {group === "evidence_gap" && horizon.reason ? (
        <small>影响：{reasonImpactLabel(horizon.reason.impact)}</small>
      ) : null}
      {group === "watch" && trigger ? <small>待确认：{trigger.rationale}</small> : null}
      {group === "actionable" && falsifier ? <small>失效：{falsifier.rationale}</small> : null}
      {group === "evidence_gap" && horizon.reason?.next_action ? (
        <small>恢复：{horizon.reason.next_action}</small>
      ) : null}
    </span>
  );
}

function AssetHorizonReason({
  asset,
  horizon,
  thesis,
}: {
  asset: MacroAssetPresentation;
  horizon: MacroAssetPresentation["horizons"][number];
  thesis: MacroThesisV1;
}) {
  return (
    <section className="macro-decision__asset-horizon">
      <p>
        <strong>{horizonLabel(horizon.horizon)}：</strong>
        {horizon.causal_channel}
      </p>
      <div className="macro-decision__asset-source">
        <strong>事实动量来源</strong>
        <span>
          {asset.symbol} {assetSourceSeriesLabel(asset.source_dataset_id)} ·{" "}
          {assetSourceProviderLabel(asset.source_dataset_id)}
        </span>
        <span>
          截至 {asset.as_of ?? "尚无可用事实"} · {momentumLabel(horizon.momentum_state)}
          {horizon.momentum_value == null ? "" : ` ${formatSigned(horizon.momentum_value)}`}
        </span>
        <details>
          <summary>查看事实技术身份</summary>
          <small>Dataset {asset.source_dataset_id}</small>
        </details>
      </div>
      <AssetEvidenceReferences
        emptyLabel="本期没有附加可读的支持证据。"
        label="支持当前展望"
        refs={horizon.supporting_evidence_refs}
        thesis={thesis}
      />
      <AssetEvidenceReferences
        emptyLabel="本期没有附加可读的冲突证据。"
        label="冲突与反证"
        refs={horizon.conflicting_evidence_refs}
        thesis={thesis}
      />
      <small>{confidenceLabel(horizon.confidence)}</small>
      {horizon.reason ? (
        <ReasonDetails
          claimLabels={
            new Map(thesis.mainline.claims.map((claim) => [claim.claim_id, claim.statement]))
          }
          datasetLabels={
            new Map([
              [
                asset.source_dataset_id,
                `${asset.symbol} ${assetSourceSeriesLabel(asset.source_dataset_id)}`,
              ],
            ])
          }
          reason={horizon.reason}
        />
      ) : null}
      <ConditionRationales
        conditions={[
          ...horizon.confirmation_triggers,
          ...horizon.falsifiers,
          ...horizon.checkpoints,
        ]}
      />
    </section>
  );
}

function AssetEvidenceReferences({
  refs,
  thesis,
  label,
  emptyLabel,
}: {
  refs: string[];
  thesis: MacroThesisV1;
  label: string;
  emptyLabel: string;
}) {
  const citations = new Map(thesis.citations.map((citation) => [citation.evidence_ref, citation]));
  if (!refs.length) {
    return (
      <section className="macro-decision__asset-evidence">
        <strong>{label}</strong>
        <span>{emptyLabel}</span>
      </section>
    );
  }
  return (
    <section className="macro-decision__asset-evidence">
      <strong>{label}</strong>
      <ul>
        {refs.map((ref) => {
          const citation = citations.get(ref);
          return (
            <li key={ref}>
              <div>
                <strong>{citation?.label ?? "已发布引用缺少可读证据标签"}</strong>
                <span>
                  {citation?.source_role ? sourceRoleLabel(citation.source_role) : "来源角色未提供"}{" "}
                  · 参考期 {citation?.reference ?? "未提供"}
                </span>
                <span>
                  来源发布{" "}
                  {citation?.published_at_ms
                    ? formatInstant(citation.published_at_ms)
                    : "未提供时间"}{" "}
                  · 系统接收{" "}
                  {citation?.received_at_ms ? formatInstant(citation.received_at_ms) : "未提供时间"}
                </span>
              </div>
              {citation?.source_url ? (
                <a href={citation.source_url} rel="noreferrer" target="_blank">
                  原始来源 <ExternalLink aria-hidden="true" />
                </a>
              ) : null}
              <details>
                <summary>查看引用技术身份</summary>
                <small>Evidence ref {ref}</small>
                {citation?.dataset_id ? <small>Dataset {citation.dataset_id}</small> : null}
              </details>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function ReasonDetails({
  reason,
  datasetLabels,
  claimLabels,
  showNextCheck = true,
}: {
  reason: MacroReason;
  datasetLabels?: ReadonlyMap<string, string>;
  claimLabels?: ReadonlyMap<string, string>;
  showNextCheck?: boolean;
}) {
  const affectedDatasets = readerAffectedLabels(
    reason.affected_dataset_ids,
    datasetLabels,
    "Dataset",
  );
  const affectedClaims = readerAffectedLabels(reason.affected_claim_ids, claimLabels, "论点");
  return (
    <div className="macro-decision__reason" data-impact={reason.impact}>
      <strong>{reason.message}</strong>
      <span>影响：{reasonImpactLabel(reason.impact)}</span>
      {affectedDatasets ? <span>受影响事实：{affectedDatasets}</span> : null}
      {affectedClaims ? <span>受影响论点：{affectedClaims}</span> : null}
      {reason.recovery !== "none" || reason.retryable ? (
        <>
          <span>
            恢复方式：{reasonRecoveryLabel(reason.recovery)} ·{" "}
            {reason.retryable ? "允许重试" : "不可重试"}
          </span>
          <span>恢复动作：{reason.next_action ?? "未提供恢复动作。"}</span>
        </>
      ) : null}
      {showNextCheck && reason.next_check_at_ms ? (
        <span>下次检查：{formatInstant(reason.next_check_at_ms)}</span>
      ) : null}
      {reason.affected_dataset_ids.length || reason.affected_claim_ids.length ? (
        <details>
          <summary>查看受影响技术身份</summary>
          {reason.affected_dataset_ids.map((datasetId) => (
            <small key={datasetId}>Dataset {datasetId}</small>
          ))}
          {reason.affected_claim_ids.map((claimId) => (
            <small key={claimId}>Claim {claimId}</small>
          ))}
        </details>
      ) : null}
    </div>
  );
}

function DeltaAndConditions({ data }: { data: MacroOverviewReadData }) {
  const thesis = data.thesis!;
  const scopes = (data.live_delta?.scopes ?? []).filter(
    (scope) => scope.items.length > 0 || scope.matched_binding_ids.length > 0,
  );
  const mainlineScopes = scopes.filter((scope) => scope.scope === "mainline");
  const tensionScopes = scopes.filter(
    (scope) => scope.scope === "tension" || scope.scope === "alternative",
  );
  const assetScopes = scopes.filter((scope) => scope.scope === "asset");
  return (
    <section aria-labelledby="macro-delta-title" className="macro-decision__delta-workbench">
      <header>
        <div>
          <span>LIVE DELTA</span>
          <h2 id="macro-delta-title">主线、矛盾与资产的新增事实</h2>
        </div>
        <strong
          className="macro-decision__status-label"
          data-tone={data.live_delta ? liveDeltaTone(data.live_delta.mainline_validity) : "neutral"}
        >
          主线：
          <DecisionStatusGlyph
            tone={data.live_delta ? liveDeltaTone(data.live_delta.mainline_validity) : "neutral"}
          />
          {data.live_delta ? liveDeltaLabel(data.live_delta.mainline_validity) : "尚未形成可用评估"}
        </strong>
      </header>
      {data.live_delta ? (
        <p className="macro-decision__delta-summary">
          命中论点 {data.live_delta.matched_claim_ids.length} 项 · 失效条件{" "}
          {data.live_delta.matched_falsifier_ids.length} 项 · 检查点{" "}
          {data.live_delta.matched_checkpoint_ids.length} 项 · 评估于{" "}
          {formatInstant(data.live_delta.evaluated_at_ms)}
        </p>
      ) : null}
      <div className="macro-decision__delta-layers">
        <LiveDeltaLayer empty="主线暂无直接新增事实。" scopes={mainlineScopes} title="主线" />
        {tensionScopes.length ? (
          <LiveDeltaLayer scopes={tensionScopes} title="矛盾与备选解释" />
        ) : null}
        {assetScopes.length ? <LiveDeltaLayer scopes={assetScopes} title="资产" /> : null}
      </div>
      <div className="macro-decision__delta-followups">
        <details>
          <summary>主线失效条件与检查点</summary>
          <div>
            <ConditionList conditions={thesis.mainline.falsifiers} title="失效条件" />
            <ConditionList conditions={thesis.mainline.checkpoints} title="下一检查点" />
          </div>
        </details>
        <article>
          <h3>Outcome Replay</h3>
          <ul>
            {data.outcome_replay?.horizons.map((horizon) => {
              const benchmarkResult = horizon.asset_results.find(
                (asset) => asset.symbol === horizon.benchmark_symbol,
              );
              return (
                <li key={horizon.horizon}>
                  <strong>
                    {horizonLabel(horizon.horizon)} · {horizon.benchmark_symbol}
                  </strong>
                  <span
                    className="macro-decision__status-label"
                    data-tone={outcomeTone(horizon.status)}
                  >
                    <DecisionStatusGlyph tone={outcomeTone(horizon.status)} />
                    {outcomeStatusLabel(horizon.status)} · 到期{" "}
                    {formatInstant(horizon.expires_at_ms)}
                  </span>
                  {benchmarkResult ? (
                    <span
                      className="macro-decision__status-label"
                      data-tone={assetDirectionTone(benchmarkResult.published_direction)}
                    >
                      <DecisionStatusGlyph
                        tone={assetDirectionTone(benchmarkResult.published_direction)}
                      />
                      发布方向：{assetDirectionLabel(benchmarkResult.published_direction)}
                    </span>
                  ) : null}
                  {horizon.realized_return_pct == null ? null : (
                    <span>实现收益 {formatSigned(horizon.realized_return_pct)}%</span>
                  )}
                  <ReasonDetails reason={horizon.reason} />
                </li>
              );
            }) ?? <li>尚未创建结果复盘。</li>}
          </ul>
        </article>
      </div>
    </section>
  );
}

function LiveDeltaLayer({
  title,
  scopes,
  empty,
}: {
  title: string;
  scopes: NonNullable<MacroOverviewReadData["live_delta"]>["scopes"];
  empty?: string;
}) {
  return (
    <section>
      <h3>{title}</h3>
      {scopes.length ? (
        <ul>
          {scopes.map((scope) => (
            <li data-state={scope.status} key={`${scope.scope}:${scope.scope_id}`}>
              <div>
                <strong>{scope.label}</strong>
                <span
                  className="macro-decision__status-label"
                  data-tone={liveDeltaTone(scope.status)}
                >
                  <DecisionStatusGlyph tone={liveDeltaTone(scope.status)} />
                  {liveDeltaLabel(scope.status)}
                </span>
              </div>
              {scope.items.map((item) => (
                <article
                  className="macro-decision__delta-fact"
                  key={`${item.binding_type}:${item.condition_id}`}
                >
                  <strong>
                    {item.dataset_label} · {conditionMetricLabel(item.metric_name)}
                  </strong>
                  <span>
                    {item.observed_value == null
                      ? "当前值不可用"
                      : `观察值 ${formatNumber(item.observed_value)}${unitLabel(item.unit ?? "")}`}
                    ；阈值 {operatorLabel(item.operator)} {formatNumber(item.threshold)}
                    {unitLabel(item.unit ?? "")}
                  </span>
                  <small>{item.rationale}</small>
                  <small>
                    {item.observed_at_ms
                      ? `事实时间 ${formatInstant(item.observed_at_ms)}`
                      : `证据截点 ${formatInstant(item.observation_cutoff_ms)}`}
                  </small>
                  <details>
                    <summary>证据身份</summary>
                    <small>Dataset {item.dataset_id}</small>
                    <small>Metric {item.metric_name}</small>
                  </details>
                  <ReasonDetails reason={item.reason} />
                </article>
              ))}
            </li>
          ))}
        </ul>
      ) : (
        <p>{empty}</p>
      )}
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
            <strong>{conditionEffectLabel(condition.effect)}</strong>：{condition.rationale}（
            {conditionMetricLabel(condition.metric_name)} {operatorLabel(condition.operator)}{" "}
            {formatNumber(condition.threshold)}
            {conditionMetricUnit(condition.metric_name)}）
          </li>
        ))}
      </ul>
    </article>
  );
}

function ConditionThresholdSummary({ condition }: { condition: MacroCondition }) {
  return (
    <>
      <small>{condition.rationale}</small>
      <small>
        观察：{conditionMetricLabel(condition.metric_name)} {operatorLabel(condition.operator)}{" "}
        {formatNumber(condition.threshold)}
        {conditionMetricUnit(condition.metric_name)}
      </small>
      <details>
        <summary>查看条件技术身份</summary>
        <small>Dataset {condition.dataset_id}</small>
        <small>Metric {condition.metric_name}</small>
      </details>
    </>
  );
}

function ModuleEvidenceIndex({ data }: { data: MacroOverviewReadData }) {
  const modules = new Map(data.modules.map((module) => [module.module_id, module]));
  return (
    <section aria-labelledby="macro-evidence-title" className="macro-decision__evidence-index">
      <header>
        <div>
          <span>THESIS EVIDENCE</span>
          <h2 id="macro-evidence-title">主线证据入口</h2>
          <p>模块只解释它如何支持或反驳已发布论点；详细事实在专属工作台中查看。</p>
        </div>
        <strong>{data.modules.length} 个证据模块</strong>
      </header>
      {data.thesis ? (
        <div className="macro-decision__claim-evidence">
          {data.thesis.mainline.claims.map((claim, index) => {
            const assessments = data.thesis!.module_assessments.filter((assessment) =>
              assessment.claim_ids.includes(claim.claim_id),
            );
            return (
              <article key={claim.claim_id}>
                <header>
                  <span>论点 {index + 1}</span>
                  <h3>{claim.statement}</h3>
                </header>
                <ul>
                  {assessments.map((assessment) => {
                    const module = modules.get(assessment.module_id);
                    if (!module) return null;
                    return (
                      <li data-role={assessment.role} key={assessment.module_id}>
                        <div>
                          <strong>
                            {module.label} · {moduleRoleLabel(assessment.role)}
                          </strong>
                          <p>{assessment.analysis}</p>
                          {module.summary?.top_changes[0] ? (
                            <small>
                              最新变化：{module.summary.top_changes[0].label}{" "}
                              {formatSigned(module.summary.top_changes[0].primary_change)}
                              {unitLabel(module.summary.top_changes[0].metric_unit)}
                            </small>
                          ) : null}
                        </div>
                        <Link to={module.href}>
                          查看证据 <ArrowRight aria-hidden="true" />
                        </Link>
                      </li>
                    );
                  })}
                </ul>
              </article>
            );
          })}
        </div>
      ) : null}
      <EvidenceHealth modules={data.modules} />
    </section>
  );
}

function EvidenceHealth({ modules }: { modules: MacroOverviewReadData["modules"] }) {
  return (
    <div className="macro-decision__evidence-health">
      <header>
        <h3>Evidence Health</h3>
        <span>正常状态保持安静，仅突出影响判断的缺口。</span>
      </header>
      <div role="table" aria-label="六模块证据健康">
        {modules.map((module) => {
          const gapCount =
            module.coverage_gap_count + module.current_health_gap_count + module.history_gap_count;
          const state =
            module.availability === "unavailable"
              ? "unavailable"
              : (module.current_health_state ?? "unavailable");
          const needsHealthExplanation =
            module.availability === "unavailable" ||
            module.coverage_state === "partial" ||
            module.current_health_state !== "current";
          return (
            <div data-state={state} key={module.module_id} role="row">
              <span role="cell">
                <Link to={module.href}>{module.label}</Link>
              </span>
              <span role="cell">{moduleRoleLabel(module.role ?? "uncertain")}</span>
              <span className="macro-decision__status-label" role="cell">
                <HealthStateIcon state={state} />
                {module.availability === "unavailable"
                  ? "模块不可用"
                  : module.current_health_state === "current"
                    ? "证据当前"
                    : currentHealthLabel(module.current_health_state ?? "unavailable")}
              </span>
              <div className="macro-decision__health-explanation" role="cell">
                {module.reason ? (
                  <ReasonDetails reason={module.reason} />
                ) : needsHealthExplanation ? (
                  <MissingReasonExplanation />
                ) : (
                  <small>{gapCount ? `${gapCount} 个历史缺口` : "无影响判断的当前缺口"}</small>
                )}
              </div>
              <BackfillExecutionStatus compact execution={module.backfill_execution} />
            </div>
          );
        })}
      </div>
    </div>
  );
}

function MissingReasonExplanation() {
  return (
    <div className="macro-decision__reason" data-impact="limited">
      <strong>该健康状态的结构化解释缺失。</strong>
      <span>影响：未提供影响范围，不能据此扩大或缩小当前判断。</span>
      <span>恢复动作：未提供恢复动作。</span>
    </div>
  );
}

function HealthStateIcon({ state }: { state: MacroCurrentHealthState | "unavailable" }) {
  if (state === "current") return <CheckCircle2 aria-hidden="true" />;
  if (state === "unavailable") return <XCircle aria-hidden="true" />;
  return <AlertTriangle aria-hidden="true" />;
}

function BackfillExecutionStatus({
  execution,
  compact = false,
}: {
  execution: MacroBackfillExecution | null;
  compact?: boolean;
}) {
  if (!execution) {
    return (
      <div
        className="macro-decision__backfill-status"
        data-compact={compact || undefined}
        data-state="unavailable"
        role={compact ? "cell" : undefined}
      >
        <span className="macro-decision__status-label">
          <XCircle aria-hidden="true" />
          <strong>历史回填：状态不可用</strong>
        </span>
        <small>模块未返回回填执行状态。</small>
      </div>
    );
  }
  const abnormal = !["complete", "not_required"].includes(execution.state);
  return (
    <div
      className="macro-decision__backfill-status"
      data-compact={compact || undefined}
      data-state={execution.state}
      role={compact ? "cell" : undefined}
    >
      <span className="macro-decision__status-label">
        <BackfillStateIcon state={execution.state} />
        <strong>历史回填：{backfillStateLabel(execution.state)}</strong>
      </span>
      {execution.state === "not_required" ? (
        <small>当前模块不要求历史回填。</small>
      ) : (
        <small>
          完成 {execution.complete_targets}/{execution.total_targets} · 待处理{" "}
          {execution.pending_targets} · 失败 {execution.failed_targets}
        </small>
      )}
      <small>{execution.worker_enabled ? "执行器已启用" : "执行器未启用"}</small>
      {execution.reason ? (
        <ReasonDetails reason={execution.reason} showNextCheck={false} />
      ) : abnormal ? (
        <small>该回填状态的结构化解释缺失。</small>
      ) : null}
      {execution.next_check_at_ms ? (
        <time>下次检查：{formatInstant(execution.next_check_at_ms)}</time>
      ) : null}
    </div>
  );
}

function BackfillStateIcon({ state }: { state: MacroBackfillExecution["state"] }) {
  if (state === "complete" || state === "not_required") {
    return <CheckCircle2 aria-hidden="true" />;
  }
  if (state === "paused") return <PauseCircle aria-hidden="true" />;
  if (state === "failed") return <XCircle aria-hidden="true" />;
  if (state === "retry_wait") return <Clock3 aria-hidden="true" />;
  return <RefreshCw aria-hidden="true" />;
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
                {moduleLabel(gap.module_id)} · {gapAxisLabel(gap.axis)}：{gap.reason}
                {gap.affected_claim_ids.length
                  ? `；影响 ${gap.affected_claim_ids.length} 个已发布论点`
                  : ""}
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
  const interpretation = module.summary.interpretation;
  const hasCurrentState =
    Boolean(module.summary.headline) ||
    Boolean(interpretation) ||
    module.summary.top_changes.length > 0;
  return (
    <>
      {hasCurrentState ? (
        <section className="macro-decision__state">
          {module.summary.headline || interpretation ? (
            <div>
              <span>CURRENT FACT STATE</span>
              {module.summary.headline ? <h2>{module.summary.headline}</h2> : null}
              {interpretation ? <p>{interpretation}</p> : null}
            </div>
          ) : null}
          {module.summary.top_changes.length ? (
            <div>
              <span>自然频率下的最重要变化</span>
              <ChangeList changes={module.summary.top_changes.slice(0, 3)} />
            </div>
          ) : null}
        </section>
      ) : null}
      <MacroModuleSections module={module} />
      <EvidenceDetails module={module} />
    </>
  );
}

function EvidenceDetails({ module }: { module: MacroTypedModuleReadData }) {
  const datasetById = new Map(
    module.evidence.dataset_states.map((dataset) => [dataset.dataset_id, dataset]),
  );
  const formulaVersions = collectFormulaVersions(module);
  const units = collectReaderUnits(module);
  return (
    <details className="macro-decision__evidence">
      <summary>展开 Coverage、Current Health、History Depth 与原始事实</summary>
      <div className="macro-decision__evidence-reader">
        <section>
          <h3>覆盖能力</h3>
          <div className="macro-decision__coverage-list">
            {module.status.coverage.capabilities.map((capability) => (
              <article data-state={capability.state} key={capability.capability_id}>
                <span className="macro-decision__status-label">
                  {capability.state === "available" ? (
                    <CheckCircle2 aria-hidden="true" />
                  ) : (
                    <AlertTriangle aria-hidden="true" />
                  )}
                  <strong>{capability.label}</strong>
                </span>
                <span>{capability.state === "available" ? "可用" : "缺失"}</span>
                <small>{capability.requirement === "required" ? "判断必需" : "辅助证据"}</small>
                {capability.reason ? <ReasonDetails reason={capability.reason} /> : null}
                <details>
                  <summary>查看能力技术身份</summary>
                  <small>Capability {capability.capability_id}</small>
                  {capability.dataset_ids.map((datasetId) => (
                    <small key={datasetId}>Dataset {datasetId}</small>
                  ))}
                </details>
              </article>
            ))}
          </div>
        </section>
        <section>
          <h3>分组数据健康</h3>
          <div aria-label="分组数据健康" className="macro-decision__coverage-list">
            {module.status.current_health.groups.map((group) => (
              <article data-state={group.current_health} key={group.group_id}>
                <span className="macro-decision__status-label">
                  <HealthStateIcon
                    state={group.current_health === "mixed" ? "degraded" : group.current_health}
                  />
                  <strong>{group.label}</strong>
                </span>
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
        </section>
        <section>
          <h3>历史回填执行</h3>
          <BackfillExecutionStatus execution={module.status.backfill_execution} />
        </section>
        <CalculationMethodology formulaVersions={formulaVersions} units={units} />
        <section>
          <h3>Dataset 健康与来源</h3>
          <div className="macro-decision__dataset-list">
            {module.evidence.dataset_states.map((dataset) => (
              <article data-state={dataset.current_health} key={dataset.dataset_id}>
                <div>
                  <span className="macro-decision__status-label">
                    <HealthStateIcon state={dataset.current_health} />
                    <strong>{dataset.label}</strong>
                  </span>
                  <small>
                    {sourceRoleLabel(dataset.source_role)} · {trustTierLabel(dataset.trust_tier)}
                  </small>
                </div>
                <span>
                  当前 {currentHealthLabel(dataset.current_health)} · 历史{" "}
                  {historyDepthLabel(dataset.history_depth)} ·{" "}
                  {marketStateLabel(dataset.market_state)} ·{" "}
                  {sourceStateLabel(dataset.source_state)}
                </span>
                <time>
                  参考期 {dataset.latest_reference ?? "尚无事实"} · 系统最近接收{" "}
                  {dataset.latest_received_at_ms
                    ? formatInstant(dataset.latest_received_at_ms)
                    : "未提供时间"}
                </time>
                {dataset.current_health !== "current" ? (
                  <ReasonDetails reason={dataset.current_reason} />
                ) : null}
                {!["complete", "not_required"].includes(dataset.history_depth) ? (
                  <ReasonDetails reason={dataset.history_reason} />
                ) : null}
                <a href={dataset.source_url} rel="noreferrer" target="_blank">
                  原始来源 <ExternalLink aria-hidden="true" />
                </a>
                <details>
                  <summary>查看 Dataset 技术身份</summary>
                  <small>Dataset {dataset.dataset_id}</small>
                  <small>Concept {dataset.concept_id}</small>
                  <small>Source role {dataset.source_role}</small>
                </details>
              </article>
            ))}
          </div>
        </section>
        <section className="macro-decision__raw-evidence">
          <h3>最新事实、单位与来源时钟</h3>
          {module.evidence.latest_facts.length ? (
            module.evidence.latest_facts.map((fact, index) => {
              const dataset = datasetById.get(fact.dataset_id);
              return (
                <article key={fact.fact_ref ?? `${fact.dataset_id}-${index}`}>
                  <div>
                    <strong>{dataset?.label ?? "已登记宏观事实"}</strong>
                    <small>
                      {dataset ? sourceRoleLabel(dataset.source_role) : "来源角色未提供"}
                    </small>
                  </div>
                  <span>参考期 {fact.reference ?? "未提供"}</span>
                  <span>
                    {fact.value == null ? "数值不可用" : String(fact.value)}
                    {readerUnitLabel(fact.unit)}
                  </span>
                  <div className="macro-decision__source-clocks">
                    <time>
                      事实观测{" "}
                      {fact.observed_at_ms ? formatInstant(fact.observed_at_ms) : "未提供时间"}
                    </time>
                    <time>
                      来源发布{" "}
                      {fact.published_at_ms ? formatInstant(fact.published_at_ms) : "未提供时间"}
                    </time>
                    <time>系统接收 {formatInstant(fact.received_at_ms)}</time>
                  </div>
                  <a href={fact.source_url} rel="noreferrer" target="_blank">
                    原始来源 <ExternalLink aria-hidden="true" />
                  </a>
                  <details>
                    <summary>查看事实技术身份</summary>
                    <small>Dataset {fact.dataset_id}</small>
                    {fact.series_id ? <small>Series {fact.series_id}</small> : null}
                    {fact.contract_code ? <small>Contract {fact.contract_code}</small> : null}
                    {fact.fact_ref ? <small>Fact ref {fact.fact_ref}</small> : null}
                    <small>Unit {fact.unit}</small>
                  </details>
                </article>
              );
            })
          ) : (
            <p>当前模块没有可展示的最新事实。</p>
          )}
        </section>
        <ReconciliationLedger receipts={module.evidence.reconciliation_receipts} />
      </div>
    </details>
  );
}

function CalculationMethodology({
  formulaVersions,
  units,
}: {
  formulaVersions: string[];
  units: string[];
}) {
  return (
    <section className="macro-decision__methodology">
      <h3>计算口径与单位</h3>
      <dl>
        <div>
          <dt>可读单位</dt>
          <dd>{units.length ? units.join("、") : "接口未提供单位元数据。"}</dd>
        </div>
        <div>
          <dt>确定性计算</dt>
          <dd>
            {formulaVersions.length
              ? formulaVersions.map(calculationFormulaLabel).join("、")
              : "接口未提供公式版本；不据此推断计算口径。"}
          </dd>
        </div>
      </dl>
      {formulaVersions.length ? (
        <details>
          <summary>查看公式版本审计</summary>
          {formulaVersions.map((formula) => (
            <small key={formula}>Formula {formula}</small>
          ))}
        </details>
      ) : null}
    </section>
  );
}

function ReconciliationLedger({ receipts }: { receipts: JsonObject[] }) {
  if (!receipts.length) return null;
  return (
    <section className="macro-decision__reconciliation">
      <h3>多来源一致性收据</h3>
      <p>同一概念的不同来源保持独立，只核对参考期和差异，不混合为单一事实。</p>
      {receipts.map((receipt, index) => {
        const comparisons = asRecords(receipt.comparisons);
        const observations = asRecords(receipt.observations);
        const state = readText(receipt, "state");
        return (
          <details key={readText(receipt, "concept_id") ?? index}>
            <summary>
              对照 {index + 1} ·{" "}
              {state === "complete" ? "来源齐全" : state === "partial" ? "部分来源" : "证据不足"}
            </summary>
            <p>
              选择规则：主来源仅用于决策，代理来源独立保留；本次记录 {observations.length} 个来源、
              {comparisons.length} 组可比较事实。
            </p>
            {comparisons.length ? (
              <ul>
                {comparisons.map((comparison, comparisonIndex) => {
                  const status = readText(comparison, "status");
                  return (
                    <li key={`${readText(comparison, "left_fact_ref") ?? comparisonIndex}`}>
                      <strong>
                        {status === "within_tolerance"
                          ? "参考期一致且差异在容差内"
                          : status === "reference_mismatch"
                            ? "参考期不一致"
                            : status === "divergent"
                              ? "差异超过容差"
                              : "尚未形成比较结论"}
                      </strong>
                      <span>
                        差异 {formatAuditNumber(readNumber(comparison, "difference"))}{" "}
                        {unitLabel(readText(comparison, "unit") ?? "")} · 容差{" "}
                        {formatAuditNumber(readNumber(comparison, "tolerance"))}
                      </span>
                    </li>
                  );
                })}
              </ul>
            ) : (
              <p>当前来源无法在相同参考期和单位下直接比较。</p>
            )}
            <details>
              <summary>查看来源身份审计</summary>
              <p>{readText(receipt, "concept_id") ?? "未提供 concept identity"}</p>
              <ul>
                {observations.map((observation, observationIndex) => (
                  <li key={readText(observation, "fact_ref") ?? observationIndex}>
                    {readText(observation, "dataset_id") ?? "未知 Dataset"} ·{" "}
                    {readText(observation, "source_role") ?? "未知来源角色"} ·{" "}
                    {readText(observation, "reference") ?? "无参考期"} ·{" "}
                    {readNumber(observation, "value") == null
                      ? "无数值"
                      : formatAuditNumber(readNumber(observation, "value"))}
                    {unitLabel(readText(observation, "unit") ?? "")}
                  </li>
                ))}
              </ul>
            </details>
          </details>
        );
      })}
    </section>
  );
}

function formatAuditNumber(value: number | null): string {
  return value == null ? "—" : formatNumber(value);
}

function ChangeList({ changes, compact = false }: { changes: MacroChange[]; compact?: boolean }) {
  if (!changes.length) return null;
  return (
    <div className="macro-decision__changes" data-compact={compact || undefined}>
      {changes.map((change) => (
        <article key={change.dataset_id}>
          <span>{change.label}</span>
          <strong>
            {formatNumber(change.value)} {unitLabel(change.unit)}
          </strong>
          <small>
            {cadenceLabel(change.cadence)} · {formatMetrics(change.metrics, change.metric_unit)}
            {change.as_of ? ` · 截至 ${change.as_of}` : ""}
          </small>
          <small>{change.importance_explanation}</small>
        </article>
      ))}
    </div>
  );
}

function ConditionRationales({ conditions }: { conditions: MacroCondition[] }) {
  if (!conditions.length) return null;
  const unique = [
    ...new Map(conditions.map((condition) => [condition.condition_id, condition])).values(),
  ];
  return (
    <ul>
      {unique.map((condition) => (
        <li key={condition.condition_id}>
          <strong>{conditionEffectLabel(condition.effect)}</strong>：{condition.rationale}
        </li>
      ))}
    </ul>
  );
}

function reasonImpactLabel(value: MacroReason["impact"]) {
  return { blocked: "阻断当前判断", limited: "限制判断范围", none: "不影响已发布判断" }[value];
}

function reasonRecoveryLabel(value: MacroReason["recovery"]) {
  return {
    automatic: "后台自动恢复",
    next_session: "下一交易时段重新检查",
    none: "无需恢复",
    operator_action: "需要操作员处理",
  }[value];
}

function readerAffectedLabels(
  ids: string[],
  labels: ReadonlyMap<string, string> | undefined,
  identityLabel: string,
): string | null {
  if (!ids.length) return null;
  if (!labels) return `${ids.length} 个${identityLabel}`;
  const readerLabels = ids.flatMap((id) => {
    const label = labels.get(id);
    return label ? [label] : [];
  });
  const missingCount = ids.length - readerLabels.length;
  if (!readerLabels.length) return `${ids.length} 个${identityLabel}（可读标签缺失）`;
  return [
    readerLabels.join("、"),
    missingCount ? `另有 ${missingCount} 个${identityLabel}缺少可读标签` : null,
  ]
    .filter((value): value is string => value != null)
    .join("；");
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

function backfillStateLabel(value: MacroBackfillExecution["state"]) {
  return {
    complete: "已完成",
    failed: "失败",
    not_required: "不需要",
    paused: "已暂停",
    queued: "已排队",
    retry_wait: "等待重试",
    running: "执行中",
  }[value];
}

function assetSourceSeriesLabel(datasetId: string): string {
  if (datasetId === "fred.vixcls") return "波动率指数日值";
  if (datasetId.startsWith("binance.")) return "现货日线";
  return "日线收盘";
}

function assetSourceProviderLabel(datasetId: string): string {
  if (datasetId.startsWith("fred.")) return "美国圣路易斯联储 FRED";
  if (datasetId.startsWith("binance.")) return "Binance 现货市场";
  if (datasetId.startsWith("nasdaq.")) return "Nasdaq Data Link";
  return "已登记资产数据源";
}

function sourceRoleLabel(value: string): string {
  return (
    {
      decision_primary: "决策主来源",
      derived: "确定性计算",
      history: "历史序列",
      intraday_proxy: "盘中代理来源",
      official_document: "官方文件",
      reconciliation_only: "仅用于来源核对",
      release: "官方发布事件",
    }[value] ?? "已登记来源角色"
  );
}

function trustTierLabel(value: string): string {
  return (
    {
      exchange: "交易所来源",
      official: "官方来源",
      untrusted_proxy: "未验证代理来源",
    }[value] ?? "信任等级未提供"
  );
}

function conditionMetricLabel(value: string): string {
  if (/\p{Script=Han}/u.test(value)) return value;
  return (
    {
      change_1d_bp: "1 日变化",
      change_1d_pct: "1 日变化",
      change_1m_bp: "1 个月变化",
      change_1m_pct: "1 个月变化",
      change_1w_bp: "1 周变化",
      change_1w_pct: "1 周变化",
      change_3m_bp: "3 个月变化",
      change_3m_pct: "3 个月变化",
      change_4w_bp: "4 周变化",
      change_4w_pct: "4 周变化",
      level: "当前水平",
      return_1d_pct: "1 日回报",
      return_1m_pct: "1 个月回报",
      return_1w_pct: "1 周回报",
      spread_bp: "利差",
      value: "当前水平",
      yield_pct: "收益率",
    }[value] ?? "已登记观察指标"
  );
}

function conditionMetricUnit(value: string): string {
  if (value.endsWith("_bp")) return "bp";
  if (value.endsWith("_pct")) return "%";
  return "";
}

function collectFormulaVersions(value: unknown): string[] {
  return collectNamedStrings(value, new Set(["formula", "formula_version"]));
}

function collectReaderUnits(value: unknown): string[] {
  return collectNamedStrings(value, new Set(["metric_unit", "unit"]))
    .map(readerUnitLabel)
    .filter((unit) => unit.length > 0)
    .filter((unit, index, values) => values.indexOf(unit) === index);
}

function collectNamedStrings(value: unknown, keys: ReadonlySet<string>): string[] {
  const output = new Set<string>();
  const visit = (current: unknown, depth: number) => {
    if (depth > 12 || current == null) return;
    if (Array.isArray(current)) {
      for (const item of current) visit(item, depth + 1);
      return;
    }
    if (typeof current !== "object") return;
    for (const [key, item] of Object.entries(current as Record<string, unknown>)) {
      if (keys.has(key) && typeof item === "string" && item) output.add(item);
      else visit(item, depth + 1);
    }
  };
  visit(value, 0);
  return [...output];
}

function calculationFormulaLabel(value: string): string {
  return (
    {
      basis_point_difference_v1: "基点差",
      difference_v1: "两项事实之差",
      fed_assets_minus_tga_rrp_v1: "联储资产减 TGA 与 RRP",
      first_difference_v1: "一阶变化",
      identity_v1: "原值直读",
      level_slope_curvature_classification_v2: "收益率曲线水平、斜率与曲率分类",
      matched_nominal_minus_real_v1: "同期限名义收益率减实际收益率",
      matched_rate_difference_v1: "同参考期利率差",
      natural_monthly_change_v1: "月度自然频率变化",
      natural_quarterly_change_v1: "季度自然频率变化",
      normalized_to_100_v1: "基期归一化为 100",
      pearson_daily_returns_v1: "日回报皮尔逊相关",
      percent_change_v1: "百分比变化",
      percent_return_v1: "百分比回报",
      series_statistics_v2: "序列水平与自然频率统计",
      year_over_year_pct_v1: "同比变化",
    }[value] ?? "已登记确定性计算口径"
  );
}

function readerUnitLabel(value: string): string {
  return (
    {
      basis_points: "bp",
      billions_chained_2017_usd: "十亿美元（2017 年不变价）",
      billions_usd: "十亿美元",
      index: "指数点",
      index_points: "指数点",
      millions_usd: "百万美元",
      percent: "%",
      percent_open_interest: "占未平仓量 %",
      persons: "人",
      price: "价格",
      thousands_persons: "千人",
      usd_per_barrel: "美元/桶",
      usdt: "USDT",
    }[value] ?? (value ? "单位未本地化" : "")
  );
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
    }[value] ?? "市场时钟未解释"
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
    }[value] ?? "来源状态未解释"
  );
}

function formatMetrics(metrics: Record<string, number | null>, unit: string): string {
  const values = Object.entries(metrics)
    .filter(([, value]) => value != null)
    .map(([key, value]) => `${metricLabel(key)} ${formatSigned(value)}${unitLabel(unit)}`);
  return values.length ? values.join(" · ") : "变化不足";
}

function metricLabel(value: string) {
  return (
    {
      change_1d_bp: "1日",
      change_1m_bp: "1月",
      change_1w_bp: "1周",
      change_3m_bp: "3个月",
      change_4w_bp: "4周",
      change_4w_pct: "4周",
      change_wow_bp: "周环比",
      change_wow_pct: "周环比",
      mom_bp: "月环比",
      mom_pct: "月环比",
      qoq_annualized_pct: "季环比年化",
      qoq_bp: "季环比",
      return_1d_pct: "1日回报",
      return_1m_pct: "1月回报",
      return_1w_pct: "1周回报",
      revision: "前值修订",
      surprise: "相对预期",
      three_month_annualized_pct: "3个月年化",
      yoy_bp: "同比",
      yoy_pct: "同比",
    }[value] ?? "登记指标"
  );
}

function cadenceLabel(value: string) {
  return (
    {
      daily: "日频",
      intraday: "盘中",
      monthly: "月度",
      quarterly: "季度",
      release: "发布事件",
      weekly: "周频",
    }[value] ?? "登记频率"
  );
}

function unitLabel(unit: string) {
  return (
    {
      basis_points: "bp",
      billions_chained_2017_usd: "十亿 2017 年不变价美元",
      billions_usd: "十亿美元",
      bp: "bp",
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
    }[unit] ?? "（单位未解释）"
  );
}
