import { Card } from "@shared/ui/Card";
import { Metric, MetricRow } from "@shared/ui/Metric";
import * as PageState from "@shared/ui/PageState";

import { useTradingOrdersWithToken, useTradingStatusWithToken } from "../api/tradingQueries";
import { isActiveOrder } from "../model/tradingLabels";

import { TradingShell, TradingSourceLine } from "./TradingChrome";
import { TradingClosed } from "./TradingClosed";
import { TradingExposure } from "./TradingExposure";
import { TradingFunnel } from "./TradingFunnel";

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
  const ordersQuery = useTradingOrdersWithToken(token);
  const status = statusQuery.data;
  const orders = ordersQuery.data?.orders ?? [];
  const active = orders.filter(isActiveOrder);
  const closed = orders.filter((order) => order.state === "CLOSED");
  const disabled = status != null && !status.readiness.enabled;

  return (
    <TradingShell label="交易 · 模拟仓">
      <header className="trading-page-header">
        <div className="trading-heading-copy">
          <h1>模拟仓</h1>
          <p>真实账本、假交易所：paper 证明的是执行内核，不是收益。</p>
        </div>
        {status ? (
          <div className="trading-heading-aside">
            <span className="trading-stat">
              <small>MODE</small>
              <b>{status.readiness.mode}</b>
            </span>
            <span className="trading-stat" data-tone="caution">
              <small>LIVE READY</small>
              <b>{status.readiness.live_readiness}</b>
            </span>
            <span className="trading-stat" data-tone="accent">
              <small>今日订单</small>
              <b>
                {status.budget.orders_today} / {status.budget.max_orders_per_day}
              </b>
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
             * The lane being off is a fact about this deployment, not an outage, and it is the first thing
             * a reader needs — otherwise every empty panel below reads as a broken page.
             */}
            {disabled ? (
              <p className="trading-banner">
                资本通道未启用（<code>trading.enabled=false</code>
                ）。下面的账本是真的，只是从来没有运行过——空表说明的是没跑过，不是读取失败。
              </p>
            ) : status.readiness.control !== "RUNNING" ? (
              <p className="trading-banner">
                控制态 <code>{status.readiness.control}</code>
                ：不接受新入场；对账与安全平仓照常运行。
              </p>
            ) : null}

            <Card flush title="预算与不变量" titleStyle="eyebrow">
              <MetricRow columns={5} label="每单一样大，无加仓">
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
                  value={`${Math.round(status.budget.max_hold_ms / 3_600_000)} h`}
                />
                <Metric
                  caption="nominal_daily_stop_loss_usd，不含跳空滑点"
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
              <TradingSourceLine
                note="一个 source fact 至多一个案例 · 一个案例至多一笔意图 · provider_attempt_count ≤ 1 · 歧义后只读对账，永不盲重发"
                path="GET /api/trading/status → budget · readiness"
              />
            </Card>

            <TradingExposure
              error={ordersQuery.isError && !ordersQuery.data ? ordersQuery.error : null}
              loading={ordersQuery.isLoading}
              onRetry={() => void ordersQuery.refetch()}
              rows={active}
            />

            <div className="trading-columns">
              <TradingClosed rows={closed} />
              <TradingFunnel
                cases={ordersQuery.data?.cases_without_orders ?? []}
                counts={status.counts}
                floors={status.floors}
              />
            </div>
          </div>
        </PageState.Stale>
      ) : null}
    </TradingShell>
  );
}
