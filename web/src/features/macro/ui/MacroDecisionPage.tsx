import * as PageState from "@shared/ui/PageState";
import { Button } from "@shared/ui/button";
import { RefreshCw } from "lucide-react";
import { Link } from "react-router-dom";

import { useMacroModuleQuery, useMacroOverviewQuery } from "../api/useMacroDecisionQuery";
import { formatInstant, moduleLabel } from "../model/macroPresentation";
import type {
  MacroModuleId,
  MacroModuleUnavailableReadData,
  MacroOverviewReadData,
  MacroDatasetState,
  MacroReason,
  MacroTypedModuleReadData,
} from "../model/macroTypes";

import { MacroModuleSections, RatesDecisionSummary } from "./MacroModuleSections";

import "./MacroDecisionBrief.css";
import "./MacroDecisionOverview.css";
import "./MacroDecisionPage.css";
import "./MacroDecisionPageResponsive.css";
import "./MacroDatasetStatus.css";
import "./MacroOverviewStatus.css";
import "./MacroUpdateState.css";

const MODULE_ROUTES: ReadonlyArray<{ id: MacroModuleId; path: string; label: string }> = [
  { id: "rates_fed", path: "/macro/rates-fed", label: "利率与美联储" },
  { id: "economy_inflation", path: "/macro/economy-inflation", label: "经济与通胀" },
  { id: "liquidity_funding", path: "/macro/liquidity-funding", label: "流动性与融资" },
  { id: "credit", path: "/macro/credit", label: "信用市场" },
  { id: "volatility", path: "/macro/volatility", label: "波动率" },
  { id: "cross_asset", path: "/macro/cross-asset", label: "大类资产与期货" },
];

export type MacroPageSessionProps = {
  bootstrapError: boolean;
  bootstrapLoading: boolean;
  token: string;
};

export function MacroOverviewPage({
  bootstrapError,
  bootstrapLoading,
  token,
}: MacroPageSessionProps) {
  const query = useMacroOverviewQuery(token);
  const boundary = sessionBoundary({ bootstrapError, bootstrapLoading, token });
  if (boundary) return boundary;
  if (query.isError && !query.data) {
    return <PageState.Error error={query.error} onRetry={() => void query.refetch()} />;
  }
  if (query.isPending || !query.data) {
    return <PageState.Loading label="读取当前宏观 session" layout="route" rows={6} />;
  }
  const data = query.data;
  return (
    <PageState.Stale updating={query.isFetching && !query.isPending}>
      <section aria-label="每日宏观主线" className="macro-decision" data-page-archetype="decision">
        <MacroHeader data={data} onRefresh={() => void query.refetch()} />
        <MacroNavigation />
        {query.isError ? (
          <UpdateDelayed error={query.error} onRetry={() => void query.refetch()} />
        ) : null}
        <OverviewStatus data={data} />
        <ModuleOverview data={data} />
      </section>
    </PageState.Stale>
  );
}

export function MacroModulePage({
  bootstrapError,
  bootstrapLoading,
  moduleId,
  token,
}: MacroPageSessionProps & { moduleId: MacroModuleId }) {
  const query = useMacroModuleQuery(token, moduleId);
  const boundary = sessionBoundary({ bootstrapError, bootstrapLoading, token });
  if (boundary) return boundary;
  if (query.isError && !query.data) {
    return <PageState.Error error={query.error} onRetry={() => void query.refetch()} />;
  }
  if (query.isPending || !query.data) {
    return <PageState.Loading label={`读取${moduleLabel(moduleId)}`} layout="route" rows={6} />;
  }
  if (query.data.availability === "unavailable") {
    return (
      <PageState.Stale updating={query.isFetching && !query.isPending}>
        <UnavailableModule
          delayedError={query.isError ? query.error : undefined}
          module={query.data}
          moduleId={moduleId}
          onRetry={() => void query.refetch()}
        />
      </PageState.Stale>
    );
  }
  const module = query.data;
  return (
    <PageState.Stale updating={query.isFetching && !query.isPending}>
      <section aria-label={module.label} className="macro-decision" data-page-archetype="decision">
        <header className="macro-decision__header">
          <div>
            <span>MACRO EVIDENCE WORKBENCH</span>
            <h1>{module.label}</h1>
            <p>确定性事实页 · 事实截止 {formatInstant(module.latest_fact_at_ms)}</p>
          </div>
          <Button onClick={() => void query.refetch()} size="sm" variant="outline">
            <RefreshCw aria-hidden="true" />
            刷新
          </Button>
        </header>
        <MacroNavigation activeModule={moduleId} />
        {query.isError ? (
          <UpdateDelayed error={query.error} onRetry={() => void query.refetch()} />
        ) : null}
        <ModuleDiagnostics module={module} />
        {module.module_id === "rates_fed" ? <RatesDecisionSummary module={module} /> : null}
        <MacroModuleSections module={module} />
      </section>
    </PageState.Stale>
  );
}

function MacroHeader({ data, onRefresh }: { data: MacroOverviewReadData; onRefresh: () => void }) {
  return (
    <header className="macro-decision__header">
      <div>
        <span>ONE CURRENT SESSION · NO FALLBACK</span>
        <h1>宏观事实总览</h1>
        <p>
          最新事实 {formatInstant(data.latest_fact_at_ms)} · 传输状态{" "}
          {transportLabel(data.transport.state)}
        </p>
      </div>
      <Button onClick={onRefresh} size="sm" variant="outline">
        <RefreshCw aria-hidden="true" />
        刷新
      </Button>
    </header>
  );
}

function OverviewStatus({ data }: { data: MacroOverviewReadData }) {
  return (
    <section aria-label="宏观总览状态" className="macro-decision__overview-status">
      <header>
        <span>SERVER-OWNED OVERVIEW STATUS</span>
        <h2>总览状态</h2>
      </header>
      <dl>
        <StatusDatum
          label="传输状态"
          state={data.transport.state}
          value={transportLabel(data.transport.state)}
        />
        <StatusDatum label="总览读取于" value={formatInstant(data.read_at_ms)} />
        <StatusDatum
          label="最近成功读取"
          value={formatInstant(data.transport.last_successful_read_at_ms)}
        />
        <StatusDatum label="最新事实" value={formatInstant(data.latest_fact_at_ms)} />
        <StatusDatum
          label="数据覆盖"
          state={data.data_quality.coverage_state}
          value={`${coverageLabel(data.data_quality.coverage_state)} · ${data.data_quality.coverage_gap_count} 个缺口`}
        />
        <StatusDatum
          label="当前质量"
          state={data.data_quality.current_health_state}
          value={`${healthLabel(data.data_quality.current_health_state)} · ${data.data_quality.current_health_gap_count} 个缺口`}
        />
        <StatusDatum
          label="历史质量"
          state={data.data_quality.history_depth_state}
          value={`${historyLabel(data.data_quality.history_depth_state)} · ${data.data_quality.history_gap_count} 个缺口`}
        />
      </dl>
      {data.transport.reason ? <ReasonSummary reason={data.transport.reason} /> : null}
    </section>
  );
}

function ModuleOverview({ data }: { data: MacroOverviewReadData }) {
  return (
    <section className="macro-decision__module-overview" aria-labelledby="macro-current-facts">
      <header>
        <span>CURRENT FACTS · DETERMINISTIC</span>
        <h2 id="macro-current-facts">当前事实摘要</h2>
      </header>
      <section className="macro-decision__module-grid" aria-label="六个宏观模块">
        {data.modules.map((module) => (
          <article
            key={module.module_id}
            data-health={module.current_health_state ?? "unavailable"}
          >
            <header>
              <span>CURRENT FACTS</span>
              <h3>{module.label}</h3>
            </header>
            <p>{module.summary?.headline ?? "该模块没有可用摘要。"}</p>
            <dl>
              <div>
                <dt>模块可用性</dt>
                <dd>{availabilityLabel(module.availability)}</dd>
              </div>
              <div>
                <dt>当前事实</dt>
                <dd>
                  {module.current_health_state
                    ? healthLabel(module.current_health_state)
                    : "未提供"}
                </dd>
              </div>
              <div>
                <dt>required 历史</dt>
                <dd>
                  {module.history_depth_state ? historyLabel(module.history_depth_state) : "未提供"}
                </dd>
              </div>
              <div>
                <dt>数据合同</dt>
                <dd>{module.coverage_state ? coverageLabel(module.coverage_state) : "未提供"}</dd>
              </div>
              <div>
                <dt>最新事实</dt>
                <dd>{formatInstant(module.latest_fact_at_ms)}</dd>
              </div>
              <div>
                <dt>缺口计数</dt>
                <dd>
                  覆盖 {module.coverage_gap_count} · 当前 {module.current_health_gap_count} · 历史{" "}
                  {module.history_gap_count}
                </dd>
              </div>
            </dl>
            {module.reason ? <ReasonSummary reason={module.reason} /> : null}
            <Link to={module.href}>进入模块</Link>
          </article>
        ))}
      </section>
    </section>
  );
}

function ModuleDiagnostics({ module }: { module: MacroTypedModuleReadData }) {
  const auditOpen = module.evidence.dataset_states.some(
    (dataset) =>
      (dataset.required_for_current && dataset.current_health !== "current") ||
      (dataset.required_for_history &&
        dataset.history_depth !== "complete" &&
        dataset.history_depth !== "not_required"),
  );
  return (
    <section
      aria-label="数据集状态"
      className="macro-decision__dataset-status macro-decision__diagnostic-strip"
    >
      <header>
        <div>
          <span>SERVER-OWNED DATASET STATES</span>
          <h2>数据集状态</h2>
          <p>{module.reason?.message ?? "状态、时钟与恢复信息均直接来自当前模块读模型。"}</p>
        </div>
        <dl aria-label="模块状态">
          <StatusDatum
            label="当前事实"
            state={module.status.current_health.state}
            value={healthLabel(module.status.current_health.state)}
          />
          <StatusDatum
            label="历史深度"
            state={module.status.history_depth.state}
            value={historyLabel(module.status.history_depth.state)}
          />
          <StatusDatum
            label="数据合同"
            state={module.status.coverage.state}
            value={coverageLabel(module.status.coverage.state)}
          />
        </dl>
      </header>
      {module.evidence.dataset_states.length ? (
        <details className="macro-decision__dataset-audit" open={auditOpen}>
          <summary>数据集审计 · {module.evidence.dataset_states.length} 条</summary>
          <div className="macro-decision__dataset-grid">
            {module.evidence.dataset_states.map((dataset) => (
              <DatasetStatus key={dataset.dataset_id} dataset={dataset} />
            ))}
          </div>
        </details>
      ) : (
        <p className="macro-decision__dataset-empty">服务端未返回数据集状态。</p>
      )}
    </section>
  );
}

function DatasetStatus({ dataset }: { dataset: MacroDatasetState }) {
  return (
    <article data-health={dataset.current_health}>
      <header>
        <div>
          <h3>{dataset.label}</h3>
          <small>{dataset.dataset_id}</small>
        </div>
        {dataset.critical ? <span>关键数据集</span> : null}
      </header>
      <dl>
        <StatusDatum
          label="当前健康"
          state={dataset.current_health}
          value={healthLabel(dataset.current_health)}
        />
        <StatusDatum label="当前合同" value={dataset.required_for_current ? "必需" : "支持"} />
        <StatusDatum label="历史合同" value={dataset.required_for_history ? "必需" : "不要求"} />
        <StatusDatum label="来源角色" value={dataset.source_role} />
        <StatusDatum
          label="来源状态"
          state={dataset.source_state}
          value={sourceStateLabel(dataset.source_state)}
        />
        <StatusDatum label="信任层级" value={trustTierLabel(dataset.trust_tier)} />
        <StatusDatum
          label="市场状态"
          state={dataset.market_state}
          value={marketLabel(dataset.market_state)}
        />
        <StatusDatum
          label="历史深度"
          state={dataset.history_depth}
          value={historyLabel(dataset.history_depth)}
        />
        <StatusDatum label="数据截止" value={dataset.latest_reference ?? "尚无引用时点"} />
        <StatusDatum label="接收于" value={formatInstant(dataset.latest_received_at_ms)} />
        {dataset.last_market_at_ms != null ? (
          <StatusDatum label="市场时钟" value={formatInstant(dataset.last_market_at_ms)} />
        ) : null}
        {dataset.next_open_ms != null ? (
          <StatusDatum label="下次开市" value={formatInstant(dataset.next_open_ms)} />
        ) : null}
      </dl>
      {dataset.source_url ? (
        <a href={dataset.source_url} rel="noreferrer" target="_blank">
          原始来源
        </a>
      ) : null}
      <DatasetReason label="当前原因" reason={dataset.current_reason} />
      <DatasetReason label="历史原因" reason={dataset.history_reason} />
    </article>
  );
}

function StatusDatum({ label, state, value }: { label: string; state?: string; value: string }) {
  return (
    <div data-state={state}>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function DatasetReason({ label, reason }: { label: string; reason: MacroReason }) {
  return (
    <div className="macro-decision__dataset-reason" data-impact={reason.impact}>
      <strong>{label}</strong>
      <p>{reason.message}</p>
      <small>
        {reason.code} · 恢复：{recoveryLabel(reason.recovery)}
      </small>
      {reason.next_action ? <small>{reason.next_action}</small> : null}
      <AffectedDatasets reason={reason} />
      {reason.next_check_at_ms != null ? (
        <time>下次检查 {formatInstant(reason.next_check_at_ms)}</time>
      ) : null}
    </div>
  );
}

function ReasonSummary({ reason }: { reason: MacroReason }) {
  return (
    <div className="macro-decision__reason-summary" data-impact={reason.impact}>
      <strong>{reason.message}</strong>
      <small>{reason.code}</small>
      <small>恢复：{recoveryLabel(reason.recovery)}</small>
      {reason.next_action ? <small>{reason.next_action}</small> : null}
      <AffectedDatasets reason={reason} />
      {reason.next_check_at_ms != null ? (
        <time>下次检查 {formatInstant(reason.next_check_at_ms)}</time>
      ) : null}
    </div>
  );
}

function UnavailableModule({
  delayedError,
  module,
  moduleId,
  onRetry,
}: {
  delayedError?: unknown;
  module: MacroModuleUnavailableReadData;
  moduleId: MacroModuleId;
  onRetry: () => void;
}) {
  return (
    <section aria-label={module.label} className="macro-decision" data-page-archetype="decision">
      <header className="macro-decision__header">
        <div>
          <span>MACRO EVIDENCE WORKBENCH</span>
          <h1>{module.label}</h1>
          <p>该模块 read model 不可用；不会用旧 schema 或其他模块填补。</p>
        </div>
      </header>
      <MacroNavigation activeModule={moduleId} />
      {delayedError !== undefined ? <UpdateDelayed error={delayedError} onRetry={onRetry} /> : null}
      <ReasonPanel onRetry={onRetry} reason={module.reason} title="模块不可用" />
    </section>
  );
}

function MacroNavigation({ activeModule }: { activeModule?: MacroModuleId }) {
  return (
    <nav aria-label="宏观页面" className="macro-decision__nav">
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
    </nav>
  );
}

function ReasonPanel({
  onRetry,
  reason,
  title,
}: {
  onRetry?: () => void;
  reason: MacroReason;
  title: string;
}) {
  return (
    <section className="macro-decision__notice" data-impact={reason.impact}>
      <div>
        <span>STATUS</span>
        <h2>{title}</h2>
        <p>{reason.message}</p>
        <dl className="macro-decision__reason-meta">
          <StatusDatum label="原因代码" value={reason.code} />
        </dl>
        <small>恢复：{recoveryLabel(reason.recovery)}</small>
        {reason.next_action ? <small>{reason.next_action}</small> : null}
        <AffectedDatasets reason={reason} />
        {reason.next_check_at_ms != null ? (
          <time>下次检查 {formatInstant(reason.next_check_at_ms)}</time>
        ) : null}
      </div>
      {reason.retryable && onRetry ? (
        <Button onClick={onRetry} size="sm" type="button" variant="outline">
          Retry
        </Button>
      ) : null}
    </section>
  );
}

function AffectedDatasets({ reason }: { reason: MacroReason }) {
  if (!reason.affected_dataset_ids.length) return null;
  return <small>受影响数据集：{reason.affected_dataset_ids.join("、")}</small>;
}

function UpdateDelayed({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  return (
    <aside
      aria-label="Update delayed"
      aria-live="polite"
      className="macro-decision__update-delayed"
      role="status"
    >
      <div>
        <strong>Update delayed</strong>
        <span>{errorText(error)}</span>
      </div>
      <Button onClick={onRetry} size="sm" type="button" variant="outline">
        Retry
      </Button>
    </aside>
  );
}

function sessionBoundary({ bootstrapError, bootstrapLoading, token }: MacroPageSessionProps) {
  if (bootstrapLoading) {
    return <PageState.Loading label="建立读取会话" layout="route" rows={3} />;
  }
  if (bootstrapError) {
    return <PageState.Error error={new Error("宏观读取会话建立失败。")} />;
  }
  if (!token) {
    return (
      <PageState.Empty
        title="宏观读取会话不可用"
        hint="Bootstrap 已结束但没有返回访问令牌；请刷新页面或检查服务认证。"
      />
    );
  }
  return null;
}

function healthLabel(value: string | null): string {
  return { current: "当前", degraded: "降级", unavailable: "不可用" }[value ?? ""] ?? "不可用";
}

function historyLabel(value: string | null): string {
  return (
    {
      complete: "完整",
      insufficient: "不足",
      not_required: "不要求",
      partial: "部分",
    }[value ?? ""] ?? "不足"
  );
}

function coverageLabel(value: string | null): string {
  return value === "complete" ? "完整" : value === "partial" ? "部分" : "未提供";
}

function availabilityLabel(value: "available" | "unavailable"): string {
  return value === "available" ? "可用" : "不可用";
}

function transportLabel(value: "current" | "stale"): string {
  return value === "current" ? "当前" : "陈旧";
}

function marketLabel(value: string): string {
  return (
    {
      closed: "休市",
      maintenance: "维护中",
      not_applicable: "不适用",
      open: "开市",
      unknown: "未知",
    }[value] ?? value
  );
}

function recoveryLabel(value: MacroReason["recovery"]): string {
  return {
    automatic: "自动重试",
    next_session: "下个交易时段",
    none: "无需恢复",
    operator_action: "需要操作员处理",
  }[value];
}

function sourceStateLabel(value: MacroDatasetState["source_state"]): string {
  return {
    degraded: "降级",
    failed: "失败",
    healthy: "健康",
    not_applicable: "不适用",
  }[value];
}

function trustTierLabel(value: MacroDatasetState["trust_tier"]): string {
  return {
    exchange: "交易所",
    official: "官方",
    untrusted_proxy: "未验证代理",
  }[value];
}

function errorText(error: unknown): string {
  if (error instanceof Error) return error.message;
  return typeof error === "string" ? error : "后台刷新失败；正在保留上次成功读取。";
}
