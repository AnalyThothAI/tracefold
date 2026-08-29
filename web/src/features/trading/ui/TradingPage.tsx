import { newsLeveragePath } from "@shared/routing/paths";
import { Card } from "@shared/ui/Card";
import { Metric, MetricRow } from "@shared/ui/Metric";
import * as PageState from "@shared/ui/PageState";
import { Link } from "react-router-dom";

import {
  useTradingIntentsWithToken,
  useTradingStatusWithToken,
  type TradingIntent,
} from "../api/tradingQueries";
import { INTENT_STATE_NOTE, caseClock, isActiveIntent, policyLabel } from "../model/tradingLabels";

import { TradingEmptyNote, TradingShell, TradingSourceLine } from "./TradingChrome";

import "./trading.css";

/**
 * 执行与持仓 — runtime readiness, immutable Intents, and what execution did with them (#331).
 *
 * Two reads, two aggregates, and no third. The Case list this page used to carry came from
 * `/api/trading/intents.cases_without_intents`, which meant a page about execution was also the page
 * about decisions — and an empty array from a failed request rendered as "the lane produced nothing".
 * Decisions live at 资本判定; this page links there rather than restating them.
 *
 * The four states are explicit. A cold failure is an error with a retry, a failed refresh keeps the last
 * answer and says so, and 0 Intent is a *truthful* empty that names the upstream figures beside it.
 */
export function TradingPage({ token }: { token: string }) {
  const statusQuery = useTradingStatusWithToken(token);
  const intentsQuery = useTradingIntentsWithToken(token);
  const status = statusQuery.data;
  const intents = intentsQuery.data?.intents ?? [];
  const active = intents.filter(isActiveIntent);
  const terminal = intents.filter((intent) => intent.execution_state === "TERMINAL");

  const coldFailure = statusQuery.isError && !status;
  return (
    <TradingShell label="执行与持仓 · Binance USD-M Demo">
      <header className="trading-page-header">
        <div className="trading-heading-copy">
          <h1>执行与持仓</h1>
          <p>唯一执行权威：Nautilus · Binance USD-M Demo · capability snapshot − blacklist</p>
        </div>
        {status ? (
          <div className="trading-heading-aside" data-tone={readinessTone(status.readiness)}>
            <span>{status.counts.day_key || "日期未知"} · UTC 预算日</span>
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
      {coldFailure ? (
        <PageState.Error error={statusQuery.error} onRetry={() => void statusQuery.refetch()} />
      ) : null}

      {status ? (
        <PageState.Stale
          failedRefresh={
            intentsQuery.isError && !intentsQuery.data
              ? "Intent 账本读取失败，其余内容仍是上次读取。"
              : undefined
          }
          onRetry={() => void intentsQuery.refetch()}
          updating={statusQuery.isFetching || intentsQuery.isFetching}
        >
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
              empty={
                <>
                  当前没有非终态 Intent；Nautilus 不持有待执行工作。过去 24 小时资本通道成案{" "}
                  <b>{status.counts.cases_24h}</b> 个、形成 Intent{" "}
                  <b>{status.counts.intents_24h}</b> 个；判定过程在{" "}
                  <Link to={newsLeveragePath()}>资本判定</Link>。
                </>
              }
              loading={intentsQuery.isPending}
              rows={active}
              title="Fresh / Active Intent"
            />
            <IntentList
              empty={<>当前窗口没有终态 Outcome。</>}
              loading={intentsQuery.isPending}
              rows={terminal}
              title="Terminal Outcome"
            />

            <Card
              flush
              hint="执行阶段的终局分布，来自 durable 行的有界聚合"
              title="24h Outcome 分布"
            >
              {Object.entries(intentsQuery.data?.outcome_counts_24h ?? {}).length ? (
                <>
                  <Tally caption="终局状态" counts={intentsQuery.data?.outcome_counts_24h ?? {}} />
                  <Tally caption="终局原因" counts={intentsQuery.data?.reason_counts_24h ?? {}} />
                </>
              ) : (
                <TradingEmptyNote>
                  {intentsQuery.isError && !intentsQuery.data
                    ? "Intent 账本本轮不可用；不能据此断言没有终局。"
                    : "过去 24 小时没有终局 Outcome。"}
                </TradingEmptyNote>
              )}
              <TradingSourceLine path="GET /api/trading/intents → outcome_counts_24h · reason_counts_24h" />
            </Card>
          </div>
        </PageState.Stale>
      ) : null}
    </TradingShell>
  );
}

/**
 * One bounded aggregate, keyed on its own dimension.
 *
 * The two 24h tallies are independent aggregates over the same window: one keyed on the terminal state,
 * one on the reason. They were rendered as one table, with the whole reason distribution repeated in
 * every outcome row — which reads as "these are CLOSED_FLAT's reasons" and is a join the server never
 * made. Two captioned lists say what each figure is counted by, and nothing more.
 */
function Tally({ caption, counts }: { caption: string; counts: Record<string, number> }) {
  const rows = Object.entries(counts);
  return (
    <dl className="trading-tally">
      <dt>{caption}</dt>
      {rows.length ? (
        rows.map(([key, count]) => (
          <dd key={key}>
            <code>{key}</code>
            <b>{count}</b>
          </dd>
        ))
      ) : (
        <dd>
          <span>—</span>
        </dd>
      )}
    </dl>
  );
}

function IntentList({
  empty,
  loading,
  rows,
  title,
}: {
  empty: React.ReactNode;
  loading: boolean;
  rows: readonly TradingIntent[];
  title: string;
}) {
  return (
    <Card flush hint="同一行只陈述账本已证明的 Intent 与 Outcome" title={title}>
      {loading ? (
        <PageState.Loading label="正在读取 Intent" layout="inline" rows={2} />
      ) : rows.length ? (
        <div className="trading-table">
          {rows.map((intent) => (
            <article className="trading-exposure-row" key={intent.intent_id}>
              <b>{intent.base_symbol}</b>
              <code>{intent.intent_id}</code>
              <span>{intent.execution_state}</span>
              <span>{intent.execution_phase ?? "等待领取"}</span>
              <span>{intent.terminal_outcome ?? INTENT_STATE_NOTE[intent.execution_state]}</span>
              <span>{outcome(intent)}</span>
              <span>{policyLabel(intent.policy_id)}</span>
              <span>{caseClock(intent.created_at_ms)}</span>
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
