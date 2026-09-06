import { Card } from "@shared/ui/Card";
import { EmptyNote } from "@shared/ui/EmptyNote";
import { PageShell } from "@shared/ui/PageShell";
import * as PageState from "@shared/ui/PageState";
import { useSearchParams } from "react-router-dom";

import {
  useTradingCasesWithToken,
  useTradingExecutionsWithToken,
  useTradingStatusWithToken,
} from "../api/tradingQueries";
import { caseFigures, caseReasonRows } from "../model/tradingCases";
import { caseClock, ledgerSentence } from "../model/tradingLabels";

import { TradingCaseDetail } from "./TradingCaseDetail";
import { TradingControls } from "./TradingControls";
import { TradingExecutionTable } from "./TradingExecutionTable";
import { TradingRisk } from "./TradingRisk";

import "./trading.css";

/**
 * The operator desk: three blocks, and a Case drawer that opens on demand (#537 PR-5).
 *
 * RISK answers "is what I already have safe" from `/api/trading/status`. ACT writes the three bounded
 * Commands and reads them back from `/api/trading/executions`. CONFIRM is the rest of that same
 * response: what the venue did with every entry today. Those two reads are the desk.
 *
 * It was six blocks over four endpoints. The two that went were both funnels: a card of today's
 * admission configuration beside a status distribution, which cost a 400-row `decisions[]` download
 * every 15 s and told an operator nothing they could act on, and a list of every Case in the window
 * whose only interactive purpose was opening one of them. `/api/trading/cases` still answers the
 * drawer behind `?case=<id>` — the link this page's own Case rows publish — and the one durable 24 h
 * card beside it.
 *
 * The page runs no timer of its own and recomputes no freshness. `execution.facts_expire_at_ms` is the
 * instant the server published as the end of its own projection's budget, and one comparison against it
 * is the whole rule — the two `setTimeout` re-render clocks and the three client freshness models they
 * drove disagreed with the server about ages it had already measured.
 */
export function TradingPage({ token }: { token: string }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const statusQuery = useTradingStatusWithToken(token);
  const casesQuery = useTradingCasesWithToken(token);
  const executionsQuery = useTradingExecutionsWithToken(token);
  const status = statusQuery.data;

  if (statusQuery.isPending && !status) {
    return <PageState.Loading label="正在读取执行账户状态" layout="panel" rows={4} />;
  }
  if (statusQuery.isError && !status) {
    return <PageState.Error error={statusQuery.error} onRetry={() => void statusQuery.refetch()} />;
  }
  if (!status) return null;

  /*
   * The whole freshness rule. `facts_expire_at_ms` is an absolute instant, so this one comparison also
   * covers a body kept from a failed refresh — there is nothing for a query-health flag to add, and no
   * reason to re-derive a heartbeat age the server already measured. A `null` expiry is not staleness:
   * it means there is no live projection at all (mode disabled, or no Runtime state), and every safety
   * word below is already `false` for that reason and says so.
   */
  const expiresAtMs = status.execution.facts_expire_at_ms;
  const stale = expiresAtMs != null && Date.now() > expiresAtMs;

  const cases = casesQuery.data?.cases ?? [];
  const executions = executionsQuery.data?.executions ?? [];
  const commands = executionsQuery.data?.commands ?? [];
  const selectedCaseId = searchParams.get("case");
  const selectedCase = selectedCaseId
    ? cases.find((item) => item.case_id === selectedCaseId)
    : undefined;

  const selectCase = (caseId: string | null) => {
    const params = new URLSearchParams(searchParams);
    if (caseId) params.set("case", caseId);
    else params.delete("case");
    setSearchParams(params, { replace: true });
  };

  const failed = [executionsQuery.isError ? "执行" : "", casesQuery.isError ? "Case" : ""].filter(
    Boolean,
  );

  return (
    <PageShell archetype="scan" className="trading-shell" label="可操作交易台">
      <header className="trading-page-header">
        <div className="trading-heading-copy">
          <h1>Trading Desk</h1>
          <p>先回答现有 exposure 是否安全，再决定是否允许新增 exposure。</p>
        </div>
        <div className="trading-heading-aside" data-tone={stale ? "caution" : undefined}>
          <span>ALPHA {caseClock(status.decision.last_case_at_ms)}</span>
          <small>EXECUTION {status.execution.mode}</small>
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
                  {casesQuery.isPending || casesQuery.isError
                    ? ledgerSentence({
                        failed: casesQuery.isError,
                        pending: casesQuery.isPending,
                        subject: "Case",
                      })
                    : `这个案例不在当前 ${casesQuery.data?.window_hours ?? "—"} 小时窗口。`}
                </EmptyNote>
              )}
            </section>
          ) : null}

          <TradingRisk execution={status.execution} stale={stale} />

          <TradingControls
            commands={commands}
            commandsFailed={executionsQuery.isError}
            commandsPending={executionsQuery.isPending}
            entriesPaused={status.execution.entries_paused}
            mode={status.execution.mode}
            token={token}
          />

          <TradingExecutionTable
            complete={executionsQuery.data?.complete ?? true}
            failed={executionsQuery.isError}
            onOpenCase={selectCase}
            pending={executionsQuery.isPending}
            rows={executions}
            selectedCaseId={selectedCaseId}
          />

          {/*
           * The one durable Case figure left on the desk. Both distributions are server aggregates over
           * the same 24 h window the blocks above describe, so a zero here is a lane that decided
           * nothing rather than a page that counted the rows it happened to render (#331).
           */}
          <Card hint="冻结判定的终局与原因" title="Alpha 成案 · 24h">
            <div className="trading-fact-grid">
              {caseFigures(casesQuery.data).map((figure) => (
                <span
                  className="trading-fact"
                  data-tone={figure.tone === "plain" ? undefined : figure.tone}
                  key={figure.key}
                >
                  <small>{figure.label}</small>
                  <b>{figure.value}</b>
                </span>
              ))}
            </div>
            {caseReasonRows(casesQuery.data).length ? (
              <div className="trading-count-list">
                {caseReasonRows(casesQuery.data).map(([reason, count]) => (
                  <span className="trading-count-row" key={reason}>
                    <small>{reason}</small>
                    <b>{count}</b>
                  </span>
                ))}
              </div>
            ) : (
              <p className="trading-inline-empty">
                {ledgerSentence({
                  failed: casesQuery.isError,
                  pending: casesQuery.isPending,
                  subject: "Case",
                })}
              </p>
            )}
          </Card>
        </div>
      </PageState.Stale>
    </PageShell>
  );
}
