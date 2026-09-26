import { useMediaQuery } from "@shared/hooks/useMediaQuery";
import { researchReturnPath } from "@shared/routing/researchContext";
import { ActionButton } from "@shared/ui/ActionButton";
import { Drawer } from "@shared/ui/Drawer";
import { EmptyNote } from "@shared/ui/EmptyNote";
import { PageShell } from "@shared/ui/PageShell";
import * as PageState from "@shared/ui/PageState";
import { useRef } from "react";
import { Link, useSearchParams } from "react-router-dom";

import {
  useTradingCaseWithToken,
  useTradingCasesWithToken,
  useTradingExecutionsWithToken,
  useTradingStatusWithToken,
} from "../api/tradingQueries";
import { caseClock, ledgerSentence } from "../model/tradingLabels";
import { useTradingFactExpiry } from "../state/useTradingFactExpiry";

import { TradingCaseDetail } from "./TradingCaseDetail";
import { TradingCaseList } from "./TradingCaseList";
import { TradingDecisionSummary } from "./TradingDecisionSummary";
import { TradingLoopLedger } from "./TradingLoopLedger";
import { TradingRecentCases } from "./TradingRecentCases";
import { TradingExposure, TradingSafetyStrip } from "./TradingRisk";
import { TradingTally } from "./TradingTally";

import "./trading.css";

/** Three independent fact reads; positions, execution history and frozen decisions have distinct scopes. */
export function TradingPage({ token }: { token: string }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const wide = useMediaQuery("(min-width: 1100px)");
  const researchFrom = researchReturnPath(searchParams.get("research_from"));
  const statusQuery = useTradingStatusWithToken(token);
  const casesQuery = useTradingCasesWithToken(token);
  const executionsQuery = useTradingExecutionsWithToken(
    token,
    searchParams.get("tab") === "executions"
      ? searchParams.get("execution_case") || undefined
      : undefined,
  );
  const selectedCaseId = searchParams.get("case");
  const tab =
    searchParams.get("tab") === "decisions"
      ? "decisions"
      : searchParams.get("tab") === "executions"
        ? "executions"
        : "positions";
  /*
   * The drawer's own read, and the only one that ever downloads a Case. It is disabled until a reader has
   * asked for one: the desk polled up to 100 frozen Cases with their checks attached every 15 s to render
   * at most one of them (#604 T3).
   */
  const caseQuery = useTradingCaseWithToken(token, selectedCaseId);
  const status = statusQuery.data;
  const executions = executionsQuery.data?.executions ?? [];

  const stale = useTradingFactExpiry(status?.execution.facts_expire_at_ms);
  const opener = useRef<HTMLElement | null>(null);

  const coldStatus = statusQuery.isPending && !status;
  const coldExecutions = executionsQuery.isPending && !executionsQuery.data;
  if (coldStatus && coldExecutions && casesQuery.isPending) {
    return <PageState.Loading label="正在读取交易台" layout="panel" rows={4} />;
  }
  if (
    statusQuery.isError &&
    executionsQuery.isError &&
    casesQuery.isError &&
    !status &&
    !executionsQuery.data &&
    !casesQuery.data
  ) {
    return (
      <PageState.Error
        error={statusQuery.error}
        onRetry={() => {
          void statusQuery.refetch();
          void executionsQuery.refetch();
          void casesQuery.refetch();
        }}
      />
    );
  }

  /*
   * The whole freshness rule. `facts_expire_at_ms` is an absolute instant, so this one comparison also
   * covers a body kept from a failed refresh. A `null` expiry is not staleness: it means there is no live
   * projection at all (execution disabled, or no Runtime state), and every safety word below is already `false`
   * for that reason and says so.
   */

  const selectedCase = selectedCaseId
    ? caseQuery.data?.cases?.find((item) => item.case_id === selectedCaseId)
    : undefined;

  const selectCase = (caseId: string | null) => {
    const params = new URLSearchParams(searchParams);
    if (caseId) {
      opener.current =
        document.activeElement instanceof HTMLElement ? document.activeElement : null;
      params.set("case", caseId);
    } else params.delete("case");
    setSearchParams(params, { replace: true });
  };

  const failed = [
    executionsQuery.isError ? "执行" : "",
    casesQuery.isError ? "策略判定" : "",
    statusQuery.isError ? "执行状态" : "",
  ].filter(Boolean);
  const execution = status?.execution;
  const connectionSummary = execution?.connection
    ? `Binance USD-M · ${execution.connection} · ${execution.account_slot} · 最后报告 ${caseClock(execution.connection_observed_at_ms)}${stale || execution.entry_block_reason === "runtime_heartbeat_stale" ? " · 状态过期，连接状态未知" : ""}${execution.configured_connection !== execution.connection ? ` · 配置待重启：${execution.configured_connection}` : ""}`
    : execution
      ? `已配置连接：Binance USD-M · ${execution.configured_connection} · ${execution.account_slot}；尚未连接`
      : "连接状态未取得";

  const browseDecisions = () => {
    const next = new URLSearchParams(searchParams);
    next.set("tab", "decisions");
    next.delete("cursor");
    next.delete("execution_case");
    setSearchParams(next);
  };
  const detail = selectedCaseId ? (
    <Drawer
      title="策略判定依据"
      open
      inline={wide}
      modal={false}
      width={680}
      restoreFocusTo={opener.current}
      onOpenChange={(open) => {
        if (!open) selectCase(null);
      }}
      actions={
        <ActionButton size="sm" onClick={() => selectCase(null)}>
          关闭
        </ActionButton>
      }
    >
      {selectedCase ? (
        <TradingCaseDetail item={selectedCase} token={token} />
      ) : (
        <EmptyNote className="trading-empty-note">
          {caseQuery.isPending || caseQuery.isError
            ? ledgerSentence({
                failed: caseQuery.isError,
                pending: caseQuery.isPending,
                subject: "策略判定",
              })
            : "未找到保留的策略判定。"}
        </EmptyNote>
      )}
    </Drawer>
  ) : null;

  return (
    <PageShell archetype="scan" className="trading-shell" label="交易执行监控">
      <header className="trading-page-header">
        <div className="trading-heading-copy">
          <span className="trading-eyebrow">TRADING / EXECUTION DESK</span>
          <h1>交易执行</h1>
          <p>看清当前仓位，也看清策略判断与实际执行。</p>
        </div>
        <div className="trading-heading-aside">
          <span>{connectionSummary}</span>
        </div>
      </header>
      {researchFrom ? (
        <Link className="trading-research-return" to={researchFrom}>
          ← 返回原始观察与筛选
        </Link>
      ) : null}
      <PageState.Stale
        failedRefresh={
          failed.length ? `${failed.join(" / ")}账本读取失败；保留其余已验证事实。` : undefined
        }
        onRetry={() => {
          void statusQuery.refetch();
          void casesQuery.refetch();
          void executionsQuery.refetch();
        }}
        updating={casesQuery.isFetching || executionsQuery.isFetching}
      >
        <div className="trading-body">
          {status ? (
            <TradingSafetyStrip execution={status.execution} stale={stale} />
          ) : (
            <EmptyNote className="trading-empty-note">
              {ledgerSentence({
                failed: statusQuery.isError,
                pending: statusQuery.isPending,
                subject: "执行状态",
              })}
            </EmptyNote>
          )}
          <div className="trading-research-tabs" role="group" aria-label="交易视图">
            {(
              [
                ["positions", "持仓与订单"],
                ["decisions", "策略判定"],
                ["executions", "执行记录"],
              ] as const
            ).map(([value, label]) => (
              <button
                type="button"
                key={value}
                aria-pressed={tab === value}
                data-active={tab === value || undefined}
                onClick={() => {
                  const next = new URLSearchParams(searchParams);
                  next.set("tab", value);
                  ["cursor", "execution_case", "case", "entry", "source_item_id"].forEach((key) =>
                    next.delete(key),
                  );
                  setSearchParams(next);
                }}
              >
                {label}
              </button>
            ))}
          </div>
          <div
            className="trading-workbench"
            data-split={tab === "positions" || !!selectedCaseId || undefined}
          >
            <div className="trading-workbench-main">
              {tab === "positions" ? (
                <>
                  {status ? (
                    <TradingExposure execution={status.execution} stale={stale} />
                  ) : (
                    <EmptyNote>当前仓位与保护暂不可读。</EmptyNote>
                  )}
                  <TradingTally
                    execution={status?.execution}
                    executions={executions}
                    executionsFailed={executionsQuery.isError}
                    executionsPending={executionsQuery.isPending}
                    totals={executionsQuery.data?.totals}
                    stale={stale}
                  />
                </>
              ) : tab === "executions" ? (
                <>
                  {searchParams.get("execution_case") ? (
                    <p className="trading-scope-note">
                      仅查看判定 {searchParams.get("execution_case")} 的全部保留执行。
                      <ActionButton
                        size="sm"
                        onClick={() => {
                          const next = new URLSearchParams(searchParams);
                          next.delete("execution_case");
                          next.delete("entry");
                          setSearchParams(next);
                        }}
                      >
                        返回最近 24 小时
                      </ActionButton>
                    </p>
                  ) : null}
                  <TradingLoopLedger
                    caseFiltered={!!searchParams.get("execution_case")}
                    complete={executionsQuery.data?.complete ?? true}
                    failed={executionsQuery.isError}
                    onOpenCase={selectCase}
                    pending={executionsQuery.isPending}
                    rows={executions}
                    selectedCaseId={selectedCaseId}
                  />
                </>
              ) : (
                <TradingCaseList token={token} onOpen={selectCase} />
              )}
            </div>
            {detail ??
              (tab === "positions" ? (
                <TradingRecentCases token={token} onOpen={selectCase} onBrowse={browseDecisions} />
              ) : null)}
          </div>
          <details className="trading-process-disclosure" open={tab === "decisions" || undefined}>
            <summary>策略运行与统计口径</summary>
            <div className="trading-runtime-context">
              <span>最近策略判定 {caseClock(status?.decision.last_case_at_ms)}</span>
              <span>
                分析
                {status?.decision.state === "running"
                  ? "运行中"
                  : status?.decision.state === "model_unconfigured"
                    ? "模型未配置"
                    : status?.decision.state === "disabled"
                      ? "已停用"
                      : "不可用"}
                {status?.decision.model_name ? ` · ${status.decision.model_name}` : ""}
                {status
                  ? status.decision.publish_signals
                    ? " · Signal 发布已开启"
                    : " · 只观察"
                  : " · 发布状态未取得"}
              </span>
              <span>
                {status?.decision.active_policy ?? "策略未取得"}
                {status?.decision.config_digest
                  ? ` · 配置 ${status.decision.config_digest.slice(0, 12)}`
                  : ""}
              </span>
            </div>
            <TradingDecisionSummary
              cases={casesQuery.data}
              failed={casesQuery.isError}
              pending={casesQuery.isPending}
              onBrowse={browseDecisions}
            />
          </details>
        </div>
      </PageState.Stale>
    </PageShell>
  );
}
