import { Card } from "@shared/ui/Card";
import { Metric, MetricRow } from "@shared/ui/Metric";
import * as PageState from "@shared/ui/PageState";

import {
  useTradingCasesWithToken,
  useTradingObservationsWithToken,
  useTradingSignalsWithToken,
  useTradingStatusWithToken,
} from "../api/tradingQueries";
import { caseStateLabel } from "../model/tradingCases";
import { caseClock, policyReasonLabel } from "../model/tradingLabels";

import { TradingEmptyNote, TradingShell, TradingSourceLine } from "./TradingChrome";

import "./trading.css";

/** Current C boundary: Alpha emits Signals; execution stays explicitly disabled until E. */
export function TradingPage({ token }: { token: string }) {
  const statusQuery = useTradingStatusWithToken(token);
  const casesQuery = useTradingCasesWithToken(token);
  const signalsQuery = useTradingSignalsWithToken(token);
  const observationsQuery = useTradingObservationsWithToken(token);
  const status = statusQuery.data;

  if (statusQuery.isPending && !status) {
    return <PageState.Loading label="正在读取 Signal 通道状态" layout="panel" rows={4} />;
  }
  if (statusQuery.isError && !status) {
    return <PageState.Error error={statusQuery.error} onRetry={() => void statusQuery.refetch()} />;
  }
  if (!status) return null;

  const failed = [
    casesQuery.isError ? "Case" : "",
    signalsQuery.isError ? "Signal" : "",
    observationsQuery.isError ? "Observation" : "",
  ].filter(Boolean);

  return (
    <TradingShell label="Alpha Signal 与执行观察">
      <header className="trading-page-header">
        <div className="trading-heading-copy">
          <h1>Alpha / Execution</h1>
          <p>Case 与 Signal 原子落库；账户、数量、订单和保护只属于后续 Runtime。</p>
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
          void observationsQuery.refetch();
        }}
        updating={
          statusQuery.isFetching ||
          casesQuery.isFetching ||
          signalsQuery.isFetching ||
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
            hint={`${status.alpha.policy_version} · ${status.alpha.contract_sha256.slice(0, 12)}`}
            title="Alpha 合约"
          >
            <p>
              当前边界只输出 engine-neutral <code>TradeSignalV1</code>。执行配置是独立事实：profile{" "}
              <code>{status.execution.profile_id}</code>，account slot{" "}
              <code>{status.execution.account_slot}</code>；本阶段不会把它们写入 Signal。
            </p>
            <TradingSourceLine path="GET /api/trading/status → decision · execution · alpha · counts" />
          </Card>

          <Card flush hint="每个 SIGNAL_EMITTED Case 必须且只能对应一个 Signal" title="最近 Case">
            {casesQuery.data?.cases?.length ? (
              <div className="trading-table">
                {casesQuery.data.cases?.map((item) => (
                  <article className="trading-current-row" key={item.case_id}>
                    <b>{item.base_symbol}</b>
                    <span>{caseClock(item.observed_at_ms)}</span>
                    <span>{caseStateLabel(item)}</span>
                    <code>{item.market_key ?? "—"}</code>
                    <span>{policyReasonLabel(item.policy_reason)}</span>
                    <code title={item.case_id}>{item.case_id.slice(0, 16)}</code>
                  </article>
                ))}
              </div>
            ) : (
              <TradingEmptyNote>
                {ledgerEmpty(casesQuery.isPending, casesQuery.isError, "Case")}
              </TradingEmptyNote>
            )}
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
            hint="D/E Runtime 的 venue-native 回执；C 阶段允许为空但不能伪造 ready"
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

function ledgerEmpty(pending: boolean, failed: boolean, owner: string): string {
  if (pending) return `正在读取 ${owner} 账本…`;
  if (failed) return `${owner} 账本读取失败，不能据此断言为空。`;
  return `当前 24 小时窗口没有 ${owner}。`;
}
