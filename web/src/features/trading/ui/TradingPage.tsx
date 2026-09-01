import { Card } from "@shared/ui/Card";
import { Metric, MetricRow } from "@shared/ui/Metric";
import * as PageState from "@shared/ui/PageState";
import { useSearchParams } from "react-router-dom";

import {
  useTradingCasesWithToken,
  useTradingCommandsWithToken,
  useTradingObservationsWithToken,
  useTradingSignalsWithToken,
  useTradingStatusWithToken,
} from "../api/tradingQueries";
import { caseStateLabel } from "../model/tradingCases";
import { caseClock, policyReasonLabel } from "../model/tradingLabels";

import { TradingCaseDetail } from "./TradingCaseDetail";
import { TradingEmptyNote, TradingShell, TradingSourceLine } from "./TradingChrome";

import "./trading.css";

/** Operator view: every execution claim remains tied to a durable Runtime or venue fact. */
export function TradingPage({ token }: { token: string }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const statusQuery = useTradingStatusWithToken(token);
  const casesQuery = useTradingCasesWithToken(token);
  const signalsQuery = useTradingSignalsWithToken(token);
  const commandsQuery = useTradingCommandsWithToken(token);
  const observationsQuery = useTradingObservationsWithToken(token);
  const status = statusQuery.data;

  if (statusQuery.isPending && !status) {
    return <PageState.Loading label="正在读取 Signal 通道状态" layout="panel" rows={4} />;
  }
  if (statusQuery.isError && !status) {
    return <PageState.Error error={statusQuery.error} onRetry={() => void statusQuery.refetch()} />;
  }
  if (!status) return null;

  /*
   * The URL owns the selection, and nothing is selected by default (#460). `/news/alpha` opened on its
   * first row; this card is one of six on a status page, so auto-expanding a Case would push five
   * ledgers below the fold to answer a question nobody asked. The three surfaces that link here with
   * `?case=` are asking about one Case, and they get it open.
   */
  const cases = casesQuery.data?.cases ?? [];
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
    casesQuery.isError ? "Case" : "",
    signalsQuery.isError ? "Signal" : "",
    commandsQuery.isError ? "Command" : "",
    observationsQuery.isError ? "Observation" : "",
  ].filter(Boolean);

  return (
    <TradingShell label="Alpha Signal 与执行观察">
      <header className="trading-page-header">
        <div className="trading-heading-copy">
          <h1>Alpha / Execution</h1>
          <p>Case 与 Signal 原子落库；账户、数量、订单、保护与恢复只属于 Nautilus Runtime。</p>
        </div>
        <div
          className="trading-heading-aside"
          data-tone={status.execution.ready ? undefined : "caution"}
        >
          <span>ALPHA {status.decision.state}</span>
          <small>
            EXECUTION {status.execution.mode} · {status.execution.reason}
          </small>
        </div>
      </header>

      <PageState.Stale
        failedRefresh={
          failed.length ? `${failed.join(" / ")} 读取失败；保留其余已验证事实。` : undefined
        }
        onRetry={() => {
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
          <MetricRow className="trading-mandate" columns={6} label="当前持久事实">
            <Metric eyebrow="ALPHA" value={status.decision.state} caption="Signal 决策面" />
            <Metric
              eyebrow="EXECUTION"
              value={status.execution.mode}
              caption={status.execution.ready ? "Runtime ready" : status.execution.reason}
              tone={status.execution.ready ? "accent" : "caution"}
            />
            <Metric eyebrow="CASES 24H" value={status.counts.cases_24h} caption="全部终局与在途" />
            <Metric eyebrow="SIGNALS 24H" value={status.counts.signals_24h} caption="Alpha 输出" />
            <Metric eyebrow="OPEN" value={status.counts.cases_open} caption="PENDING / RUNNING" />
            <Metric
              eyebrow="UNEXPIRED"
              value={status.counts.signals_unexpired}
              caption="仍在 TTL 内"
            />
          </MetricRow>

          <Card
            hint={
              status.execution.runtime_release
                ? `${status.execution.runtime_release} · reconcile ${status.execution.reconciliation_age_ms ?? "?"}ms`
                : `${status.alpha.policy_version} · ${status.alpha.contract_sha256.slice(0, 12)}`
            }
            title="Alpha / Runtime 边界"
          >
            <p>
              Alpha 只输出 engine-neutral <code>TradeSignalV1</code>。执行配置是独立事实：profile{" "}
              <code>{status.execution.profile_id}</code>，account slot{" "}
              <code>{status.execution.account_slot}</code>。Binance account flat：
              <code>
                {status.execution.account_flat ? "PROVEN" : "NOT PROVEN"}
              </code>；credential{" "}
              <code>{status.execution.credential_fingerprint?.slice(0, 12) ?? "unavailable"}</code>
              。
            </p>
            <TradingSourceLine path="GET /api/trading/status → decision · execution · alpha · counts" />
          </Card>

          <Card
            hint="HTTP 200 或 CLI ok 只证明意图已持久化，不证明 Runtime、订单、成交或账户已平"
            title="操作命令账本"
          >
            <p>
              事实层级：意图已记录 → Runtime 受理 → 订单已接受 → 成交 → Binance
              账户已平。每一级都必须有自己的持久回执，不能向后推断。
            </p>
            {commandsQuery.data?.commands?.length ? (
              <div className="trading-table">
                {commandsQuery.data.commands.map((command) => (
                  <article className="trading-current-row" key={command.command_id}>
                    <b>{commandActionLabel(command.action)}</b>
                    <span data-tone={command.disposition ? undefined : "caution"}>
                      {commandDispositionLabel(command)}
                    </span>
                    <code>{command.target_profile_id}</code>
                    <span>{command.market_key ?? command.scope}</span>
                    <span>seq {command.seq}</span>
                    <code title={command.command_id}>{command.command_id.slice(0, 16)}</code>
                  </article>
                ))}
              </div>
            ) : (
              <TradingEmptyNote>
                {ledgerEmpty(commandsQuery.isPending, commandsQuery.isError, "Command")}
              </TradingEmptyNote>
            )}
            <TradingSourceLine path="GET /api/trading/execution/commands → commands[] · disposition" />
          </Card>

          <Card
            flush
            hint="每个 SIGNAL_EMITTED Case 必须且只能对应一个 Signal；点一行读它自己冻结的判定证据"
            title="最近 Case"
          >
            {cases.length ? (
              <div className="trading-table">
                {cases.map((item) => (
                  <button
                    aria-expanded={item.case_id === selectedCaseId}
                    className="trading-current-row trading-case-row"
                    data-selected={item.case_id === selectedCaseId || undefined}
                    key={item.case_id}
                    onClick={() =>
                      selectCase(item.case_id === selectedCaseId ? null : item.case_id)
                    }
                    type="button"
                  >
                    <b>{item.base_symbol}</b>
                    <span>{caseClock(item.observed_at_ms)}</span>
                    <span>{caseStateLabel(item)}</span>
                    <code>{item.market_key ?? "—"}</code>
                    <span>{policyReasonLabel(item.policy_reason)}</span>
                    <code title={item.case_id}>{item.case_id.slice(0, 16)}</code>
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
              /* The window is the lane's published `window_hours`, never a literal: a link from the OI
                 audit can name a Case the rolling window has already dropped, and saying "24" here
                 would be wrong the first time an operator changes it. */
              <TradingEmptyNote>
                这个案例不在当前窗口的 {cases.length} 条里；账本按{" "}
                {casesQuery.data?.window_hours ?? "—"} 小时滚动。
              </TradingEmptyNote>
            ) : null}
            <TradingSourceLine path="GET /api/trading/cases → cases[] · state_counts_24h" />
          </Card>

          <Card flush hint="不含账户、凭证、数量、杠杆、订单或保护参数" title="最近 TradeSignalV1">
            {signalsQuery.data?.signals?.length ? (
              <div className="trading-table">
                {signalsQuery.data.signals?.map((signal) => (
                  <article className="trading-current-row" key={signal.signal_id}>
                    <b>{signal.direction.toUpperCase()}</b>
                    <span data-tone={signal.expired ? "caution" : undefined}>
                      {signal.expired ? "EXPIRED" : "VALID"}
                    </span>
                    <code>{signal.market_key}</code>
                    <code title={signal.case_id}>{signal.case_id.slice(0, 16)}</code>
                    <span>seq {signal.seq}</span>
                    <code title={signal.signal_id}>{signal.signal_id.slice(0, 16)}</code>
                  </article>
                ))}
              </div>
            ) : (
              <TradingEmptyNote>
                {ledgerEmpty(signalsQuery.isPending, signalsQuery.isError, "Signal")}
              </TradingEmptyNote>
            )}
            <TradingSourceLine path="GET /api/trading/signals → signals[]" />
          </Card>

          <Card
            flush
            hint="Runtime 与 venue-native 回执；为空、RUNNING 或 ready 都不能被解释成订单或成交"
            title="执行观察"
          >
            {observationsQuery.data?.observations?.length ? (
              <div className="trading-table">
                {observationsQuery.data.observations?.map((observation) => (
                  <article className="trading-current-row" key={observation.event_id}>
                    <b>{observation.normalized_kind}</b>
                    <span>{observation.runtime_profile_id}</span>
                    <code>
                      {observation.signal_id?.slice(0, 16) ??
                        observation.command_id?.slice(0, 16) ??
                        "—"}
                    </code>
                    <span>seq {observation.seq}</span>
                    <code title={observation.runtime_release}>
                      {observation.runtime_release.slice(0, 16)}
                    </code>
                    <code title={observation.event_id}>{observation.event_id.slice(0, 16)}</code>
                  </article>
                ))}
              </div>
            ) : (
              <TradingEmptyNote>
                {ledgerEmpty(observationsQuery.isPending, observationsQuery.isError, "Observation")}
              </TradingEmptyNote>
            )}
            <TradingSourceLine path="GET /api/trading/execution/observations → observations[]" />
          </Card>
        </div>
      </PageState.Stale>
    </TradingShell>
  );
}

function commandActionLabel(action: string): string {
  return (
    {
      emergency_halt: "紧急停止",
      flatten: "减仓至空",
      manual_entry: "手动方向",
      pause_entries: "暂停开仓",
      resume_entries: "恢复开仓",
    }[action] ?? action
  );
}

function commandDispositionLabel(command: {
  disposition?: string | null;
  disposition_reason?: string | null;
  expired: boolean;
}): string {
  if (command.disposition) {
    return command.disposition_reason
      ? `${command.disposition} · ${command.disposition_reason}`
      : command.disposition;
  }
  return command.expired ? "已过期 · 未见终局" : "等待 Runtime";
}

function ledgerEmpty(pending: boolean, failed: boolean, owner: string): string {
  if (pending) return `正在读取 ${owner} 账本…`;
  if (failed) return `${owner} 账本读取失败，不能据此断言为空。`;
  return `当前 24 小时窗口没有 ${owner}。`;
}
