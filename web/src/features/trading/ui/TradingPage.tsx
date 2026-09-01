import { Card } from "@shared/ui/Card";
import * as PageState from "@shared/ui/PageState";
import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";

import {
  useTradingCasesWithToken,
  useTradingCommandsWithToken,
  useTradingObservationsWithToken,
  useTradingSignalsWithToken,
  useTradingStatusWithToken,
} from "../api/tradingQueries";
import { ACCOUNT_FLAT_PROOF_FRESH_MS, currentAccountFlatProof } from "../model/accountFlatProof";
import { commandProgress, signalProgress } from "../model/executionProgress";
import { caseStateLabel } from "../model/tradingCases";
import { caseClock, policyReasonLabel } from "../model/tradingLabels";

import { TradingAccountOverview } from "./TradingAccountOverview";
import { TradingCaseDetail } from "./TradingCaseDetail";
import { TradingEmptyNote, TradingShell, TradingSourceLine } from "./TradingChrome";
import { TradingControls } from "./TradingControls";
import { TradingProgress } from "./TradingProgress";

import "./trading.css";

/** Operator desk: current risk and control come first; immutable audit remains one disclosure away. */
export function TradingPage({ token }: { token: string }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const statusQuery = useTradingStatusWithToken(token);
  const casesQuery = useTradingCasesWithToken(token);
  const signalsQuery = useTradingSignalsWithToken(token);
  const commandsQuery = useTradingCommandsWithToken(token);
  const observationsQuery = useTradingObservationsWithToken(token);
  const status = statusQuery.data;
  const [, setFlatProofExpiryTick] = useState(0);
  const measuredAtMs = status?.measured_at_ms;
  const serverFlatProof = status?.execution.account_flat_proven ?? false;

  useEffect(() => {
    if (!serverFlatProof || measuredAtMs == null) return;
    const nowMs = Date.now();
    const expiresAtMs = measuredAtMs + ACCOUNT_FLAT_PROOF_FRESH_MS;
    if (measuredAtMs > nowMs || expiresAtMs < nowMs) return;
    const timer = window.setTimeout(
      () => setFlatProofExpiryTick((value) => value + 1),
      expiresAtMs - nowMs + 1,
    );
    return () => window.clearTimeout(timer);
  }, [measuredAtMs, serverFlatProof]);

  if (statusQuery.isPending && !status) {
    return <PageState.Loading label="正在读取执行账户状态" layout="panel" rows={4} />;
  }
  if (statusQuery.isError && !status) {
    return <PageState.Error error={statusQuery.error} onRetry={() => void statusQuery.refetch()} />;
  }
  if (!status) return null;

  const accountFlatProven = currentAccountFlatProof({
    accountFlatProven: status.execution.account_flat_proven,
    measuredAtMs: status.measured_at_ms,
    nowMs: Date.now(),
    queryHealthy: !statusQuery.isError,
  });
  const currentExecution = { ...status.execution, account_flat_proven: accountFlatProven };

  const cases = casesQuery.data?.cases ?? [];
  const signals = signalsQuery.data?.signals ?? [];
  const commands = commandsQuery.data?.commands ?? [];
  const observations = observationsQuery.data?.observations ?? [];
  const latestSignal = signals[0];
  const latestCommand = commands[0];
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
    signalsQuery.isError ? "Signal" : "",
    commandsQuery.isError ? "Command" : "",
    observationsQuery.isError ? "Observation" : "",
  ].filter(Boolean);
  const progressWarning = observationReadWarning(observationsQuery);

  return (
    <TradingShell label="可操作交易台">
      <header className="trading-page-header">
        <div className="trading-heading-copy">
          <h1>Trading Desk</h1>
          <p>先回答现有 exposure 是否安全，再决定是否允许新增 exposure。</p>
        </div>
        <div
          className="trading-heading-aside"
          data-tone={status.execution.execution_safe ? undefined : "caution"}
        >
          <span>ALPHA {status.decision.state}</span>
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
          void signalsQuery.refetch();
          void commandsQuery.refetch();
          void observationsQuery.refetch();
        }}
        updating={
          statusQuery.isFetching ||
          casesQuery.isFetching ||
          signalsQuery.isFetching ||
          commandsQuery.isFetching ||
          observationsQuery.isFetching
        }
      >
        <div className="trading-body">
          <TradingAccountOverview execution={currentExecution} />

          <TradingControls
            accountFlatProven={accountFlatProven}
            entriesPaused={status.execution.entries_paused}
            mode={status.execution.mode}
            token={token}
          />

          <Card
            className="trading-latest-progress-card"
            hint="persisted、Runtime、venue、fill 与 private-flat 是五个不同事实"
            title="最近 Signal / Command"
          >
            {progressWarning ? (
              <p className="trading-progress-uncertainty" data-tone="caution">
                {progressWarning}
              </p>
            ) : null}
            <div className="trading-latest-progress-grid">
              <section>
                <span className="trading-section-eyebrow">LATEST COMMAND</span>
                {latestCommand ? (
                  <>
                    <div className="trading-progress-subject">
                      <b>{commandActionLabel(latestCommand.action)}</b>
                      <span>{latestCommand.reason}</span>
                    </div>
                    <TradingProgress progress={commandProgress(latestCommand, observations)} />
                  </>
                ) : (
                  <TradingEmptyNote>
                    {ledgerEmpty(commandsQuery.isPending, commandsQuery.isError, "Command")}
                  </TradingEmptyNote>
                )}
              </section>
              <section>
                <span className="trading-section-eyebrow">LATEST SIGNAL</span>
                {latestSignal ? (
                  <>
                    <div className="trading-progress-subject">
                      <b>{latestSignal.direction.toUpperCase()}</b>
                      <span>{latestSignal.market_key}</span>
                    </div>
                    <TradingProgress progress={signalProgress(latestSignal, observations)} />
                  </>
                ) : (
                  <TradingEmptyNote>
                    {ledgerEmpty(signalsQuery.isPending, signalsQuery.isError, "Signal")}
                  </TradingEmptyNote>
                )}
              </section>
            </div>
          </Card>

          <Card
            hint="HTTP 200 只证明意图已持久化；每条进度由关联 Observation 推进"
            title="Command 进度"
          >
            {commands.length ? (
              <div className="trading-ledger-list">
                {commands.map((item) => (
                  <article className="trading-ledger-row" key={item.command_id}>
                    <div>
                      <b>{commandActionLabel(item.action)}</b>
                      <span>{item.reason}</span>
                    </div>
                    <TradingProgress progress={commandProgress(item, observations)} />
                  </article>
                ))}
              </div>
            ) : (
              <TradingEmptyNote>
                {ledgerEmpty(commandsQuery.isPending, commandsQuery.isError, "Command")}
              </TradingEmptyNote>
            )}
            <TradingSourceLine path="GET /api/trading/execution/commands + observations → durable progress" />
          </Card>

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
                {ledgerEmpty(casesQuery.isPending, casesQuery.isError, "Case")}
              </TradingEmptyNote>
            )}
            {selectedCase ? <TradingCaseDetail item={selectedCase} /> : null}
            {selectedCaseId && !selectedCase && cases.length ? (
              <TradingEmptyNote>
                这个案例不在当前 {casesQuery.data?.window_hours ?? "—"} 小时窗口。
              </TradingEmptyNote>
            ) : null}
          </Card>

          <details className="trading-advanced-audit">
            <summary>Advanced Audit</summary>
            <div className="trading-advanced-body">
              <p>
                profile <code>{status.execution.profile_id}</code> · account slot{" "}
                <code>{status.execution.account_slot}</code> · reconcile{" "}
                <code>{status.execution.reconciliation_age_ms ?? "?"}ms</code>
              </p>
              <p>
                runtime <code>{status.execution.runtime_release ?? "unavailable"}</code> · image{" "}
                <code>{status.execution.image_digest ?? "unavailable"}</code> · credential{" "}
                <code>{status.execution.credential_fingerprint ?? "unavailable"}</code>
              </p>
              <div className="trading-audit-list">
                {(status.execution.current_account?.positions ?? []).map((item) => (
                  <article key={`position:${item.position_id}`}>
                    <b>current_position</b>
                    <span>{item.side}</span>
                    <code>{item.position_id}</code>
                  </article>
                ))}
                {(status.execution.current_account?.orders ?? []).map((item) => (
                  <article key={`order:${item.client_order_id}`}>
                    <b>current_order</b>
                    <span>
                      {item.state} / {item.leg}
                    </span>
                    <code>{item.client_order_id}</code>
                  </article>
                ))}
                {observations.map((item) => (
                  <article key={item.event_id}>
                    <b>{item.normalized_kind}</b>
                    <span>seq {item.seq}</span>
                    <code>{item.event_id}</code>
                  </article>
                ))}
              </div>
              <TradingSourceLine path="GET /api/trading/status + execution observations → identities and digests" />
            </div>
          </details>
        </div>
      </PageState.Stale>
    </TradingShell>
  );
}

function commandActionLabel(action: string): string {
  return (
    {
      emergency_halt: "紧急停止",
      flatten: "Flatten account",
      manual_entry: "手动方向",
      pause_entries: "Pause entries",
      resume_entries: "Resume / Arm",
    }[action] ?? action
  );
}

function ledgerEmpty(pending: boolean, failed: boolean, owner: string): string {
  if (pending) return `正在读取 ${owner} 账本…`;
  if (failed) return `${owner} 账本读取失败，不能据此断言为空。`;
  return `当前 24 小时窗口没有 ${owner}。`;
}

function observationReadWarning(query: {
  data?: { complete: boolean };
  isError: boolean;
  isPending: boolean;
}): string | null {
  if (query.isPending) return "正在读取 Observation；persisted 之后的阶段暂时未知。";
  if (query.isError) return "Observation 账本读取失败；不能断言 Runtime、venue、fill 或完成状态。";
  if (query.data && !query.data.complete) {
    return "Observation 窗口已截断；未关联到回执不能解释为尚未发生。";
  }
  return null;
}
