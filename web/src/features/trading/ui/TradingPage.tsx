import { Metric, MetricRow } from "@shared/ui/Metric";
import * as PageState from "@shared/ui/PageState";

import {
  useTradingGateWithToken,
  useTradingOrdersWithToken,
  useTradingStatusWithToken,
} from "../api/tradingQueries";
import { holdCeiling, isActiveOrder } from "../model/tradingLabels";

import { TradingShell } from "./TradingChrome";
import { TradingClosed } from "./TradingClosed";
import { TradingExposure } from "./TradingExposure";
import { TradingAdmission, TradingEvidence } from "./TradingFunnel";
import { TradingDecisions, TradingHeadline, TradingLadder } from "./TradingReadout";

import "./trading.css";

/**
 * 模拟仓 — a real ledger against a fake exchange (#207 PR-W4, #104, #185).
 *
 * What paper proves is the execution kernel: that one source fact becomes at most one case, that a case
 * authors at most one intent, that an entry is attempted exactly once, and that an ambiguous write goes to
 * read-only reconciliation instead of a blind resend. It does not prove a return, so no number here becomes
 * an equity curve and the unrealised column says 纸面 outright.
 *
 * Every row states only the step it has proven. `ACKNOWLEDGED` is the venue answering; `OPEN` is the only
 * state with both a position and a native stop behind it; `AMBIGUOUS` carries no action at all, because the
 * one thing that must never happen from a screen is a human resending an order nobody has reconciled.
 *
 * There is no live switch on this page and no field that could become one. `live_ready` is reported as the
 * ledger's own word — `not_proven` until PR-C3's venue canary — and the surface has no write endpoint to
 * offer even if someone drew a button.
 */
export function TradingPage({ token }: { token: string }) {
  const statusQuery = useTradingStatusWithToken(token);
  const status = statusQuery.data;
  const ordersQuery = useTradingOrdersWithToken(
    token,
    undefined,
    status?.counts.funnel_day_key ?? null,
  );
  // The admission ledger, read for the frame's own four numbers. A case knows which rule it stopped
  // on; only the gate row kept what the measurement was, and `case_id` is what joins them (#273).
  const gateQuery = useTradingGateWithToken(token);
  const orders = ordersQuery.data?.orders ?? [];
  const cases = ordersQuery.data?.cases_without_orders ?? [];
  const decisions = gateQuery.data?.decisions ?? [];
  const active = orders.filter(isActiveOrder);
  const closed = orders.filter((order) => order.state === "CLOSED");
  const disabled = status != null && !status.readiness.enabled;

  return (
    <TradingShell label="交易 · 模拟仓">
      <header className="trading-page-header">
        <div className="trading-heading-copy">
          <h1>模拟仓</h1>
          <p>真实账本、假交易所：paper 证明的是执行内核，不是收益</p>
        </div>
        {status ? (
          <div
            aria-label={`MODE ${status.readiness.mode} · LIVE READY ${status.readiness.live_readiness}`}
            className="trading-heading-aside"
            data-tone={disabled ? "caution" : undefined}
          >
            <span>{status.counts.funnel_day_key || "日期未知"} · UTC 预算日</span>
            {disabled ? <small>资本通道未启用（trading.enabled=false）</small> : null}
            {!disabled && status.readiness.control !== "RUNNING" ? (
              <small>控制态 {status.readiness.control} · 不接受新入场</small>
            ) : null}
            <span aria-hidden className="trading-mobile-safety">
              <span>
                <small>MODE</small>
                <b>{status.readiness.mode}</b>
              </span>
              <span>
                <small>LIVE READY</small>
                <b>{status.readiness.live_readiness}</b>
              </span>
              <span>
                <small>今日订单</small>
                <b>
                  {status.budget.orders_today} / {status.budget.max_orders_per_day}
                </b>
              </span>
            </span>
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
        <PageState.Stale updating={statusQuery.isFetching || ordersQuery.isFetching}>
          <div className="trading-body">
            {/*
             * Before any count: whether the lane is quiet, broken, or working exactly as configured.
             * Every panel below this one is a number about something that did not happen, and without
             * a sentence naming the rule they stopped on they all read as an outage (#273).
             */}
            <TradingHeadline
              cases={cases}
              counts={status.counts}
              decisions={decisions}
              status={status}
            />
            {/*
             * The lane being off is a fact about this deployment, not an outage, and it is the first thing
             * a reader needs — otherwise every empty panel below reads as a broken page.
             */}
            <MetricRow className="trading-mandate" columns={5} label="固定交易预算">
              <Metric
                caption="每单一样大，无加仓"
                eyebrow="固定名义"
                value={`${status.budget.notional_usd} USDT`}
              />
              <Metric
                caption="交易所原生 stop"
                eyebrow="固定止损"
                value={`${status.budget.stop_loss_bps} bps`}
              />
              <Metric
                caption="从首笔成交起算"
                eyebrow="最长持有"
                value={holdCeiling(status.budget.max_hold_ms)}
              />
              <Metric
                caption="不含跳空滑点"
                eyebrow="名义日止损上限"
                value={`$${status.budget.nominal_daily_stop_loss_usd}`}
              />
              <Metric
                caption="active 期间同标的封锁"
                eyebrow="一标的一仓"
                tone="accent"
                value="1 / 1"
              />
            </MetricRow>

            <TradingExposure
              count={status.counts.active_orders}
              error={ordersQuery.isError && !ordersQuery.data ? ordersQuery.error : null}
              loading={ordersQuery.isPending}
              mode={status.readiness.mode}
              onRetry={() => void ordersQuery.refetch()}
              rows={active}
            />

            <div className="trading-columns">
              <TradingClosed count={status.counts.closed_orders_today} rows={closed} />
              <TradingLadder counts={status.counts} />
            </div>
            {/* What the next order is waiting for, one case at a time, each stating its own
                measurement against its own threshold. */}
            <TradingDecisions cases={cases} decisions={decisions} status={status} />
            {/* Source admission sits beside the funnel rather than inside the technical disclosure:
                with the lane at zero orders it is the only panel on the page with an answer (#264). */}
            <TradingAdmission counts={status.counts} />
            <TradingEvidence counts={status.counts} floors={status.floors} />
          </div>
        </PageState.Stale>
      ) : null}
    </TradingShell>
  );
}
