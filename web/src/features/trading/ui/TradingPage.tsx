import { EmptyNote } from "@shared/ui/EmptyNote";
import { PageShell } from "@shared/ui/PageShell";
import * as PageState from "@shared/ui/PageState";
import { useSearchParams } from "react-router-dom";

import {
  useTradingCaseWithToken,
  useTradingCasesWithToken,
  useTradingExecutionsWithToken,
  useTradingStatusWithToken,
} from "../api/tradingQueries";
import { caseClock, ledgerSentence } from "../model/tradingLabels";

import { TradingCaseDetail } from "./TradingCaseDetail";
import { TradingControls } from "./TradingControls";
import { TradingFunnel } from "./TradingFunnel";
import { TradingLoopLedger } from "./TradingLoopLedger";
import { TradingExposure, TradingSafetyStrip } from "./TradingRisk";
import { TradingTally } from "./TradingTally";

import "./trading.css";

/**
 * The operator desk: six blocks in one column, and a Case drawer that opens on demand (#604 T4).
 *
 * The order is the order an operator asks the questions in. ① is it alive and armed, and if not why.
 * ② what has today's capital done. ③ what did the lane do above the account — frames, Cases, refusals.
 * ④ every entry in the window with what the venue did to it. ⑤ what is on the account right now, closed
 * while that is nothing. ⑥ the three writes, and every Command with the Runtime's answer.
 *
 * **Three independent reads, three independent failures.** `/api/trading/status` used to gate the whole
 * page: it was read first and a cold error returned one error panel, so a 5xx on the readiness projection
 * blanked a perfectly readable execution ledger. It answers ① ⑤ and half of ② now, and nothing else waits
 * on it. Each block states its own unreadable answer in the desk's one ledger vocabulary, and
 * `PageState.Stale` names which ledger broke while keeping the two that did not.
 *
 * The page runs no timer of its own and recomputes no freshness. `execution.facts_expire_at_ms` is the
 * instant the server published as the end of its own projection's budget, and one comparison against it
 * is the whole rule.
 */
export function TradingPage({ token }: { token: string }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const statusQuery = useTradingStatusWithToken(token);
  const casesQuery = useTradingCasesWithToken(token);
  const executionsQuery = useTradingExecutionsWithToken(token);
  const selectedCaseId = searchParams.get("case");
  /*
   * The drawer's own read, and the only one that ever downloads a Case. It is disabled until a reader has
   * asked for one: the desk polled up to 100 frozen Cases with their checks attached every 15 s to render
   * at most one of them (#604 T3).
   */
  const caseQuery = useTradingCaseWithToken(token, selectedCaseId);
  const status = statusQuery.data;
  const executions = executionsQuery.data?.executions ?? [];
  const commands = executionsQuery.data?.commands ?? [];

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
  const expiresAtMs = status?.execution.facts_expire_at_ms;
  const stale = expiresAtMs != null && Date.now() > expiresAtMs;

  const selectedCase = selectedCaseId
    ? caseQuery.data?.cases?.find((item) => item.case_id === selectedCaseId)
    : undefined;

  const selectCase = (caseId: string | null) => {
    const params = new URLSearchParams(searchParams);
    if (caseId) params.set("case", caseId);
    else params.delete("case");
    setSearchParams(params, { replace: true });
  };

  const failed = [
    executionsQuery.isError ? "执行" : "",
    casesQuery.isError ? "Case" : "",
    statusQuery.isError ? "执行状态" : "",
  ].filter(Boolean);

  return (
    <PageShell archetype="scan" className="trading-shell" label="可操作交易台">
      <header className="trading-page-header">
        <div className="trading-heading-copy">
          <h1>Trading Desk</h1>
          <p>先回答现有 exposure 是否安全，再决定是否允许新增 exposure。</p>
        </div>
        {/*
         * `EXECUTION paper` is a constant and no longer wears the caution colour. Amber is what the desk
         * says when something needs an operator, and spending it on a word that has not changed since the
         * lane started taught readers to ignore it (#604 T4).
         */}
        <div className="trading-heading-aside" data-tone={stale ? "caution" : undefined}>
          <span>ALPHA {caseClock(status?.decision.last_case_at_ms)}</span>
          <small>EXECUTION {status?.execution.mode ?? "UNAVAILABLE"}</small>
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
            <section aria-label="案例抽屉" className="trading-case-drawer">
              <div className="trading-case-drawer-bar">
                <code>{selectedCaseId}</code>
                <button onClick={() => selectCase(null)} type="button">
                  关闭
                </button>
              </div>
              {selectedCase ? (
                <TradingCaseDetail item={selectedCase} />
              ) : (
                <EmptyNote className="trading-empty-note">
                  {caseQuery.isPending || caseQuery.isError
                    ? ledgerSentence({
                        failed: caseQuery.isError,
                        pending: caseQuery.isPending,
                        subject: "Case",
                      })
                    : `这个案例不在当前 ${casesQuery.data?.window_hours ?? "—"} 小时窗口。`}
                </EmptyNote>
              )}
            </section>
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

          <TradingTally
            execution={status?.execution}
            executions={executions}
            executionsFailed={executionsQuery.isError}
            executionsPending={executionsQuery.isPending}
            totals={executionsQuery.data?.totals}
          />

          <TradingFunnel
            cases={casesQuery.data}
            executions={executions}
            failed={casesQuery.isError}
            pending={casesQuery.isPending}
          />

          <TradingLoopLedger
            complete={executionsQuery.data?.complete ?? true}
            failed={executionsQuery.isError}
            onOpenCase={selectCase}
            pending={executionsQuery.isPending}
            rows={executions}
            selectedCaseId={selectedCaseId}
          />

          {status ? <TradingExposure execution={status.execution} stale={stale} /> : null}

          <TradingControls
            commands={commands}
            commandsFailed={executionsQuery.isError}
            commandsPending={executionsQuery.isPending}
            entriesPaused={status?.execution.entries_paused ?? false}
            mode={status?.execution.mode ?? "disabled"}
            token={token}
          />
        </div>
      </PageState.Stale>
    </PageShell>
  );
}
