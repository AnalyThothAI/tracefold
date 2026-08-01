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
  MacroReason,
  MacroTypedModuleReadData,
} from "../model/macroTypes";

import { MacroModuleSections, RatesDecisionSummary } from "./MacroModuleSections";

import "./MacroDecisionBrief.css";
import "./MacroDecisionOverview.css";
import "./MacroDecisionPage.css";
import "./MacroDecisionPageResponsive.css";

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
        <MacroHeader
          data={data}
          stale={query.isError || data.transport.state === "stale"}
          onRefresh={() => void query.refetch()}
        />
        <MacroNavigation />
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
    return <UnavailableModule module={query.data} moduleId={moduleId} />;
  }
  const module = query.data;
  return (
    <PageState.Stale updating={query.isFetching && !query.isPending}>
      <section aria-label={module.label} className="macro-decision" data-page-archetype="decision">
        <header className="macro-decision__header">
          <div>
            <span>MACRO EVIDENCE WORKBENCH</span>
            <h1>{module.label}</h1>
            <p>
              确定性事实页 · 事实截止 {formatInstant(module.latest_fact_at_ms)}
            </p>
          </div>
          <Button onClick={() => void query.refetch()} size="sm" variant="outline">
            <RefreshCw aria-hidden="true" />
            刷新
          </Button>
        </header>
        <MacroNavigation activeModule={moduleId} />
        {module.module_id === "rates_fed" ? <RatesDecisionSummary module={module} /> : null}
        <MacroModuleSections module={module} />
        <ModuleDiagnostics module={module} />
      </section>
    </PageState.Stale>
  );
}

function MacroHeader({
  data,
  stale,
  onRefresh,
}: {
  data: MacroOverviewReadData;
  stale: boolean;
  onRefresh: () => void;
}) {
  return (
    <header className="macro-decision__header">
      <div>
        <span>ONE CURRENT SESSION · NO FALLBACK</span>
        <h1>宏观事实总览</h1>
        <p>
          最新事实 {formatInstant(data.latest_fact_at_ms)} · {stale ? "传输缓存可能陈旧" : "读取成功"}
        </p>
      </div>
      <Button onClick={onRefresh} size="sm" variant="outline">
        <RefreshCw aria-hidden="true" />
        刷新
      </Button>
    </header>
  );
}

function ModuleOverview({ data }: { data: MacroOverviewReadData }) {
  return (
    <section className="macro-decision__module-overview" aria-labelledby="macro-current-facts">
      <header>
        <span>CURRENT FACTS · DETERMINISTIC</span>
        <h2 id="macro-current-facts">当前事实摘要</h2>
      </header>
      <div className="macro-decision__module-grid" aria-label="六个宏观模块">
        {data.modules.map((module) => (
          <article
            key={module.module_id}
            data-health={module.current_health_state ?? "unavailable"}
          >
            <header>
              <span>CURRENT FACTS</span>
              <h3>{module.label}</h3>
            </header>
            <p>{module.summary?.headline ?? module.reason?.message ?? "尚无模块投影。"}</p>
            <dl>
              <div>
                <dt>当前事实</dt>
                <dd>{healthLabel(module.current_health_state)}</dd>
              </div>
              <div>
                <dt>required 历史</dt>
                <dd>{historyLabel(module.history_depth_state)}</dd>
              </div>
              <div>
                <dt>数据合同</dt>
                <dd>{module.coverage_state === "complete" ? "完整" : "部分"}</dd>
              </div>
            </dl>
            <Link to={module.href}>进入模块</Link>
          </article>
        ))}
      </div>
    </section>
  );
}

function ModuleDiagnostics({ module }: { module: MacroTypedModuleReadData }) {
  return (
    <aside className="macro-decision__diagnostic-strip" aria-label="模块诊断">
      <strong>
        当前事实 {healthLabel(module.status.current_health.state)} · required 历史{" "}
        {historyLabel(module.status.history_depth.state)} · 数据合同{" "}
        {coverageLabel(module.status.coverage.state)}
      </strong>
      <span>
        {module.reason?.message ?? "当前模块直接读取持久化事实与确定性计算结果。"}
      </span>
    </aside>
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
    <section aria-label={module.label} className="macro-decision" data-page-archetype="decision">
      <header className="macro-decision__header">
        <div>
          <span>MACRO EVIDENCE WORKBENCH</span>
          <h1>{module.label}</h1>
          <p>该模块 read model 不可用；不会用旧 schema 或其他模块填补。</p>
        </div>
      </header>
      <MacroNavigation activeModule={moduleId} />
      <ReasonPanel reason={module.reason} title="模块不可用" />
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

function ReasonPanel({ reason, title }: { reason: MacroReason; title: string }) {
  return (
    <section className="macro-decision__notice" data-impact={reason.impact}>
      <div>
        <span>STATUS</span>
        <h2>{title}</h2>
        <p>{reason.message}</p>
        {reason.next_action ? <small>{reason.next_action}</small> : null}
      </div>
    </section>
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
  return value === "complete" ? "完整" : "部分";
}
