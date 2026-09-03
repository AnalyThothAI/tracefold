import { Card } from "@shared/ui/Card";
import * as PageState from "@shared/ui/PageState";
import { useSearchParams } from "react-router-dom";

import {
  useTradingCasesWithToken,
  useTradingExecutionsWithToken,
  useTradingGateWithToken,
  useTradingStatusWithToken,
} from "../api/tradingQueries";
import { caseStateLabel } from "../model/tradingCases";
import { caseClock, policyReasonLabel } from "../model/tradingLabels";

import { TradingAccountOverview } from "./TradingAccountOverview";
import { TradingCaseDetail } from "./TradingCaseDetail";
import { TradingEmptyNote, TradingShell } from "./TradingChrome";
import { TradingControls } from "./TradingControls";
import { TradingExecutionTable } from "./TradingExecutionTable";
import { TradingFunnel } from "./TradingFunnel";

import "./trading.css";

/**
 * The operator desk: one page, six blocks, one endpoint each (#528 PR-2).
 *
 * 1 status bar and 2 account/positions read `/api/trading/status`; 3 control writes
 * `POST /api/trading/execution/commands` and reads its Command rows back from
 * `/api/trading/executions`; 4 today's executions is the rest of that same response; 5 funnel is
 * `/api/trading/gate` beside `/api/trading/cases`; 6 is the Case list and its frozen evidence.
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
  const gateQuery = useTradingGateWithToken(token);
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

  const failed = [
    statusQuery.isError ? "Status" : "",
    casesQuery.isError ? "Case" : "",
    executionsQuery.isError ? "执行" : "",
    gateQuery.isError ? "准入台账" : "",
  ].filter(Boolean);

  return (
    <TradingShell label="可操作交易台">
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
          failed.length ? `${failed.join(" / ")} 读取失败；保留其余已验证事实。` : undefined
        }
        onRetry={() => {
          void statusQuery.refetch();
          void casesQuery.refetch();
          void executionsQuery.refetch();
          void gateQuery.refetch();
        }}
        updating={
          statusQuery.isFetching ||
          casesQuery.isFetching ||
          executionsQuery.isFetching ||
          gateQuery.isFetching
        }
      >
        <div className="trading-body">
          <TradingAccountOverview execution={status.execution} stale={stale} />

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
            pending={executionsQuery.isPending}
            rows={executions}
          />

          <TradingFunnel
            cases={casesQuery.data}
            casesFailed={casesQuery.isError}
            gate={gateQuery.data}
            gateFailed={gateQuery.isError}
          />

          <Card
            flush
            hint="每个 SIGNAL_EMITTED Case 必须且只能对应一个 Signal；点一行读冻结证据"
            title="最近 Case"
          >
            {cases.length ? (
              <div className="trading-case-list">
                {cases.map((item) => (
                  <button
                    aria-expanded={item.case_id === selectedCaseId}
                    className="trading-case-card"
                    data-selected={item.case_id === selectedCaseId || undefined}
                    key={item.case_id}
                    onClick={() =>
                      selectCase(item.case_id === selectedCaseId ? null : item.case_id)
                    }
                    type="button"
                  >
                    <b>{item.base_symbol}</b>
                    <span>{caseStateLabel(item)}</span>
                    <span>{policyReasonLabel(item.policy_reason)}</span>
                    <small>{caseClock(item.observed_at_ms)}</small>
                  </button>
                ))}
              </div>
            ) : (
              <TradingEmptyNote>
                {caseLedgerEmpty(casesQuery.isPending, casesQuery.isError)}
              </TradingEmptyNote>
            )}
            {cases.length && casesQuery.data && !casesQuery.data.complete ? (
              <TradingEmptyNote>本窗口已截断；未列出的 Case 不能解释为没有发生。</TradingEmptyNote>
            ) : null}
            {selectedCase ? <TradingCaseDetail item={selectedCase} /> : null}
            {selectedCaseId && !selectedCase && cases.length ? (
              <TradingEmptyNote>
                这个案例不在当前 {casesQuery.data?.window_hours ?? "—"} 小时窗口。
              </TradingEmptyNote>
            ) : null}
          </Card>
        </div>
      </PageState.Stale>
    </TradingShell>
  );
}

function caseLedgerEmpty(pending: boolean, failed: boolean): string {
  if (pending) return "正在读取 Case 账本…";
  if (failed) return "Case 账本读取失败，不能据此断言为空。";
  return "当前 24 小时窗口没有 Case。";
}
