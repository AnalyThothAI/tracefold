import { Card } from "@shared/ui/Card";
import { Metric, MetricRow } from "@shared/ui/Metric";
import * as PageState from "@shared/ui/PageState";

import {
  useTradingIntentsWithToken,
  useTradingStatusWithToken,
  type TradingIntent,
} from "../api/tradingQueries";
import { CASE_STATE_ZH, INTENT_STATE_NOTE, isActiveIntent } from "../model/tradingLabels";

import { TradingEmptyNote, TradingShell, TradingSourceLine } from "./TradingChrome";

import "./trading.css";

/** Read-only Case → Intent → Outcome workbench for the sole execution authority. */
export function TradingPage({ token }: { token: string }) {
  const statusQuery = useTradingStatusWithToken(token);
  const status = statusQuery.data;
  const intentsQuery = useTradingIntentsWithToken(
    token,
    undefined,
    status?.counts.funnel_day_key ?? null,
  );
  const intents = intentsQuery.data?.intents ?? [];
  const active = intents.filter(isActiveIntent);
  const terminal = intents.filter((intent) => intent.execution_state === "TERMINAL");
  const cases = intentsQuery.data?.cases_without_intents ?? [];

  return (
    <TradingShell label="交易 · Binance USD-M Demo">
      <header className="trading-page-header">
        <div className="trading-heading-copy">
          <h1>Case → Intent → Outcome</h1>
          <p>唯一执行权威：Nautilus · Binance USD-M Demo · capability snapshot − blacklist</p>
        </div>
        {status ? (
          <div className="trading-heading-aside" data-tone={readinessTone(status.readiness)}>
            <span>{status.counts.funnel_day_key || "日期未知"} · UTC 预算日</span>
            <small>
              {status.readiness.control} ·{" "}
              {status.readiness.engine_ready ? "ENGINE READY" : "ENGINE NOT READY"}
            </small>
          </div>
        ) : null}
      </header>

      {statusQuery.isLoading && !status ? (
        <PageState.Loading label="正在读取资本通道状态" layout="panel" rows={3} />
      ) : null}
      {statusQuery.isError && !status ? (
        <PageState.Error error={statusQuery.error} onRetry={() => void statusQuery.refetch()} />
      ) : null}

      {status ? (
        <PageState.Stale updating={statusQuery.isFetching || intentsQuery.isFetching}>
          <div className="trading-body">
            <MetricRow className="trading-mandate" columns={5} label="冻结执行契约">
              <Metric eyebrow="VENUE" value="Binance" caption="USD-M Demo" />
              <Metric
                eyebrow="CAPABILITY"
                value={`${status.readiness.active_capability_included_count} 合约`}
                caption={
                  status.readiness.active_capability_snapshot_sha256
                    ? status.readiness.active_capability_snapshot_sha256.slice(0, 12)
                    : "尚未激活快照"
                }
              />
              <Metric
                eyebrow="NOTIONAL"
                value={`$${status.budget.target_notional_usd}`}
                caption="每次固定名义"
              />
              <Metric
                eyebrow="ENTRIES"
                value={`${status.counts.entries_today} / 1`}
                caption="每 UTC 日最多一次"
              />
              <Metric
                eyebrow="ACTIVE"
                value={status.counts.active_intents}
                caption="非终态 Intent"
                tone={status.readiness.unexpected_exposure ? "caution" : "accent"}
              />
            </MetricRow>

            <IntentList
              empty="当前没有非终态 Intent；Nautilus 不持有待执行工作。"
              rows={active}
              title="Fresh / Active Intent"
            />
            <IntentList
              empty="当前窗口没有终态 Outcome。"
              rows={terminal}
              title="Terminal Outcome"
            />

            <Card
              flush
              hint="已决但未形成 Intent 的案例保留明确终态与规则"
              title="Cases without Intent"
            >
              {cases.length ? (
                <div className="trading-table">
                  {cases.map((record) => (
                    <article className="trading-case-row" key={record.case_id}>
                      <code>{record.case_id}</code>
                      <b>{record.base_symbol}</b>
                      <span>{CASE_STATE_ZH[record.state] ?? record.state}</span>
                      <span>{record.policy_reason ?? "—"}</span>
                    </article>
                  ))}
                </div>
              ) : (
                <TradingEmptyNote>当前窗口的案例均已形成 Intent，或尚无案例。</TradingEmptyNote>
              )}
              <TradingSourceLine path="GET /api/trading/intents → cases_without_intents[]" />
            </Card>
          </div>
        </PageState.Stale>
      ) : null}
    </TradingShell>
  );
}

function IntentList({
  empty,
  rows,
  title,
}: {
  empty: string;
  rows: readonly TradingIntent[];
  title: string;
}) {
  return (
    <Card flush hint="同一行只陈述账本已证明的 Case、Intent 与 Outcome" title={title}>
      {rows.length ? (
        <div className="trading-table">
          {rows.map((intent) => (
            <article className="trading-exposure-row" key={intent.intent_id}>
              <b>{intent.base_symbol}</b>
              <code>{intent.intent_id}</code>
              <span>{intent.execution_state}</span>
              <span>{intent.execution_phase ?? "等待领取"}</span>
              <span>{intent.terminal_outcome ?? INTENT_STATE_NOTE[intent.execution_state]}</span>
              <span>{outcome(intent)}</span>
            </article>
          ))}
        </div>
      ) : (
        <TradingEmptyNote>{empty}</TradingEmptyNote>
      )}
      <TradingSourceLine path="GET /api/trading/intents → intents[]" />
    </Card>
  );
}

function outcome(intent: TradingIntent): string {
  if (intent.realized_pnl_amount == null) return intent.reason_code ?? "—";
  return `${intent.realized_pnl_amount} ${intent.realized_pnl_currency ?? ""}`.trim();
}

function readinessTone(readiness: {
  engine_ready: boolean;
  unexpected_exposure: boolean;
}): "caution" | undefined {
  return readiness.engine_ready && !readiness.unexpected_exposure ? undefined : "caution";
}
