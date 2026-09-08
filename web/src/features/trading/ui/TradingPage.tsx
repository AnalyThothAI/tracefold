import { ActionButton } from "@shared/ui/ActionButton";
import { Drawer } from "@shared/ui/Drawer";
import { EmptyNote } from "@shared/ui/EmptyNote";
import { PageShell } from "@shared/ui/PageShell";
import * as PageState from "@shared/ui/PageState";
import { useRef } from "react";
import { useSearchParams } from "react-router-dom";

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
import { TradingControls } from "./TradingControls";
import { TradingDecisionSummary } from "./TradingDecisionSummary";
import { TradingLoopLedger } from "./TradingLoopLedger";
import { TradingExposure, TradingSafetyStrip } from "./TradingRisk";
import { TradingTally } from "./TradingTally";

import "./trading.css";

/** Three independent fact reads; positions, execution history and frozen decisions have distinct scopes. */
export function TradingPage({ token }: { token: string }) {
  const [searchParams, setSearchParams] = useSearchParams();
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
  const commands = executionsQuery.data?.commands ?? [];

  const stale = useTradingFactExpiry(status?.execution.facts_expire_at_ms);
  const opener = useRef<HTMLElement | null>(null);

  const coldStatus = statusQuery.isPending && !status;
  const coldExecutions = executionsQuery.isPending && !executionsQuery.data;
  if (coldStatus && coldExecutions) {
    return <PageState.Loading label="正在读取交易台" layout="panel" rows={4} />;
  }
  if (statusQuery.isError && executionsQuery.isError && !status && !executionsQuery.data) {
    return (
      <PageState.Error
        error={statusQuery.error}
        onRetry={() => {
          void statusQuery.refetch();
          void executionsQuery.refetch();
        }}
      />
    );
  }

  /*
   * The whole freshness rule. `facts_expire_at_ms` is an absolute instant, so this one comparison also
   * covers a body kept from a failed refresh. A `null` expiry is not staleness: it means there is no live
   * projection at all (mode disabled, or no Runtime state), and every safety word below is already `false`
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

  return (
    <PageShell archetype="scan" className="trading-shell" label="可操作交易台">
      <header className="trading-page-header">
        <div className="trading-heading-copy">
          <h1>交易执行</h1>
          <p>先核对当前仓位与保护，再查看执行记录和策略判定。</p>
        </div>
        {/*
         * `EXECUTION paper` is a constant and no longer wears the caution colour. Amber is what the desk
         * says when something needs an operator, and spending it on a word that has not changed since the
         * lane started taught readers to ignore it (#604 T4).
         */}
        <div className="trading-heading-aside" data-tone={stale ? "caution" : undefined}>
          <span>最近策略判定 {caseClock(status?.decision.last_case_at_ms)}</span>
          <small>
            {status?.execution.mode === "live"
              ? "实盘模式"
              : status?.execution.mode === "paper"
                ? "模拟模式"
                : status?.execution.mode === "disabled"
                  ? "执行已停用"
                  : "模式未取得"}
          </small>
        </div>
      </header>

      <PageState.Stale
        failedRefresh={
          failed.length ? `${failed.join(" / ")}账本读取失败；保留其余已验证事实。` : undefined
        }
        onRetry={() => {
          void statusQuery.refetch();
          void casesQuery.refetch();
          void executionsQuery.refetch();
        }}
        updating={statusQuery.isFetching || casesQuery.isFetching || executionsQuery.isFetching}
      >
        <div className="trading-body">
          {selectedCaseId ? (
            <Drawer
              title="策略判定依据"
              open
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
                <TradingCaseDetail item={selectedCase} />
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
          ) : null}

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
                ["executions", "执行记录"],
                ["decisions", "策略判定"],
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
                  next.delete("cursor");
                  next.delete("execution_case");
                  setSearchParams(next);
                }}
              >
                {label}
              </button>
            ))}
          </div>
          {tab === "positions" ? (
            <>
              {status ? (
                <TradingExposure execution={status.execution} stale={stale} />
              ) : (
                <EmptyNote>当前仓位与保护暂不可读。</EmptyNote>
              )}
              <TradingControls
                commands={commands}
                commandsFailed={executionsQuery.isError}
                commandsPending={executionsQuery.isPending}
                entriesPaused={status?.execution.entries_paused ?? false}
                mode={status?.execution.mode ?? "disabled"}
                token={token}
              />
              <TradingTally
                execution={status?.execution}
                executions={executions}
                executionsFailed={executionsQuery.isError}
                executionsPending={executionsQuery.isPending}
                totals={executionsQuery.data?.totals}
              />
            </>
          ) : tab === "executions" ? (
            <>
              {searchParams.get("execution_case") ? (
                <p className="source-line">
                  仅查看判定 {searchParams.get("execution_case")} 的全部保留执行。
                  <ActionButton
                    size="sm"
                    onClick={() => {
                      const next = new URLSearchParams(searchParams);
                      next.delete("execution_case");
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
            <>
              <TradingDecisionSummary
                cases={casesQuery.data}
                failed={casesQuery.isError}
                pending={casesQuery.isPending}
                onReason={(reason) => {
                  const next = new URLSearchParams(searchParams);
                  next.set("reason", reason);
                  next.delete("cursor");
                  setSearchParams(next);
                }}
              />
              <TradingCaseList token={token} onOpen={selectCase} />
            </>
          )}
        </div>
      </PageState.Stale>
    </PageShell>
  );
}
