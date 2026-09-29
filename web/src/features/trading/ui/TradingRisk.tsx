import { Card } from "@shared/ui/Card";
import { EmptyNote } from "@shared/ui/EmptyNote";
import { SourceLine } from "@shared/ui/SourceLine";
import type { ReactNode } from "react";

import type { TradingExecutionReadiness } from "../api/tradingQueries";
import { caseClock, entryBlockReasonLabel, moneyLabel } from "../model/tradingLabels";

import { TradingPriceRange } from "./TradingPriceRange";

type SignedAccount = NonNullable<TradingExecutionReadiness["signed_account"]>;

export function TradingSafetyStrip({
  execution,
  stale,
}: {
  execution: TradingExecutionReadiness;
  stale: boolean;
}) {
  const connectionSummary = execution.connection
    ? `Binance USD-M · ${execution.connection} · ${execution.account_slot} · 最后报告 ${caseClock(execution.connection_observed_at_ms)}${stale ? " · 状态过期" : ""}`
    : `已配置连接：Binance USD-M · ${execution.configured_connection} · ${execution.account_slot}；尚未连接`;
  return (
    <div className="trading-risk" data-block="safety">
      <div className="trading-safety-grid" aria-label="执行安全状态">
        <div
          className="trading-safety-fact"
          data-tone={!stale && execution.alive ? "ready" : "caution"}
        >
          <span>执行器心跳</span>
          <b>{safety(execution.alive, stale)}</b>
          <small>PostgreSQL 持久心跳</small>
        </div>
        <div
          className="trading-safety-fact"
          data-tone={!stale && execution.entries_armed ? "ready" : "caution"}
        >
          <span>允许新增仓位</span>
          <b>{safety(execution.entries_armed, stale)}</b>
          <small>
            {stale ? "等待新状态" : entryBlockReasonLabel(execution.entry_block_reason)}
          </small>
        </div>
        <span className="trading-connection-tag">{execution.connection ?? "尚未连接"}</span>
      </div>
      {stale ? (
        <p role="status" className="trading-alert-line" data-tone="caution">
          状态通道失联：未取得有效期内的新状态；下方保留上次签名读取。
        </p>
      ) : null}
      {!stale && execution.last_error ? (
        <p role="status" className="trading-alert-line" data-tone="caution">
          执行器错误：{execution.last_error}；新增仓位已关闭。
        </p>
      ) : null}
      {!stale && execution.entry_block_reason === "account_reconcile_stale" ? (
        <p role="status" className="trading-alert-line" data-tone="caution">
          DEMO 签名账户读取已过期；新增仓位待重新核实。
        </p>
      ) : null}
      <p className="trading-connection-summary">{connectionSummary}</p>
      <p className="trading-routes-line">
        上次全账户读取 {observedTime(execution.last_full_reconcile_at_ms)} · 账户槽位{" "}
        <code>{execution.account_slot}</code>
      </p>
    </div>
  );
}

export function TradingExposure({
  execution,
  stale,
}: {
  execution: TradingExecutionReadiness;
  stale: boolean;
}) {
  const account = execution.signed_account;
  const unconfirmed =
    stale ||
    execution.entry_block_reason === "account_reconcile_stale" ||
    Boolean(execution.last_error) ||
    !execution.alive;
  const open =
    (account?.positions_total ?? 0) > 0 ||
    (account?.orders_total ?? 0) + (account?.algos_total ?? 0) > 0 ||
    execution.unexpected_exposure;
  return (
    <Card data-block="exposure" flush title={unconfirmed ? "上次签名账户读取" : "最近签名账户读取"}>
      <details className="trading-exposure" open={open}>
        <summary>
          <span>
            仓位 {account?.positions_total ?? "—"} · 普通挂单 {account?.orders_total ?? "—"} · Algo
            挂单 {account?.algos_total ?? "—"}
          </span>
          <small>{unconfirmed ? "场所当前状态待确认" : "Binance DEMO 签名 REST"}</small>
        </summary>
        {execution.unexpected_exposure ? (
          <p className="trading-alert-line" data-tone="alert">
            发现未认领的场所仓位或订单；新增仓位已暂停，请按场所身份核实。
          </p>
        ) : null}
        {account ? (
          <SignedExposure account={account} unconfirmed={unconfirmed} />
        ) : (
          <EmptyNote className="trading-empty-note">
            未取得 DEMO 签名账户快照，不能据此断言没有仓位。
          </EmptyNote>
        )}
      </details>
      <SourceLine path="GET /api/trading/status → execution.signed_account（DEMO 签名 REST）" />
    </Card>
  );
}

function SignedExposure({
  account,
  unconfirmed,
}: {
  account: SignedAccount;
  unconfirmed: boolean;
}) {
  return (
    <>
      {!account.complete ? (
        <p className="trading-alert-line" data-tone="caution">
          列表超过显示上限；上述总数来自完整场所读取。
        </p>
      ) : null}
      <p className="trading-routes-line">
        场所读取 {observedTime(Math.floor(account.observed_at_ns / 1_000_000))}
      </p>
      <div className="trading-fact-grid">
        <Fact label="DEMO 保证金权益" value={moneyLabel(account.equity_usdt)} />
        <Fact label="普通挂单" value={account.orders_total} />
        <Fact label="Algo 挂单" value={account.algos_total} />
      </div>
      {account.positions.length ? (
        <div className="trading-position-list">
          {account.positions.map((position) => {
            const stop = account.algos.find(
              (order) => order.symbol === position.symbol && order.orderType === "STOP_MARKET",
            );
            const takeProfit = account.algos.find(
              (order) =>
                order.symbol === position.symbol && order.orderType === "TAKE_PROFIT_MARKET",
            );
            const guarded = Boolean(stop && takeProfit && stop.owned && takeProfit.owned);
            return (
              <article
                className="trading-position-row"
                key={`${position.symbol}:${position.positionSide ?? "BOTH"}`}
              >
                <div className="trading-position-identity">
                  <b>{position.symbol}</b>
                  <span>{Number(position.positionAmt) > 0 ? "多仓" : "空仓"}</span>
                  {!position.owned ? <span data-tone="alert">未认领仓位</span> : null}
                </div>
                <div className="trading-position-facts">
                  <Fact label="数量" value={position.positionAmt} />
                  <Fact label="入场均价" value={position.entryPrice ?? "未取得"} />
                  <Fact label="标记价格" value={position.markPrice ?? "未取得"} />
                  <Fact label="未实现盈亏" value={moneyLabel(position.unRealizedProfit)} />
                </div>
                <TradingPriceRange
                  position={{
                    stop_trigger_price: stop?.triggerPrice ?? null,
                    take_profit_trigger_price: takeProfit?.triggerPrice ?? null,
                    entry_price: position.entryPrice ?? null,
                    mark_price: position.markPrice ?? null,
                  }}
                  stale={unconfirmed}
                />
                <div
                  className="trading-protection-strip"
                  data-tone={guarded && !unconfirmed ? "protected" : "caution"}
                >
                  <b>
                    {unconfirmed ? "保护待重新核实" : guarded ? "止损及止盈已挂" : "保护不完整"}
                  </b>
                  <span>止损 {stop?.triggerPrice ?? "未挂"}</span>
                  <span>止盈 {takeProfit?.triggerPrice ?? "未挂"}</span>
                </div>
              </article>
            );
          })}
        </div>
      ) : (
        <EmptyNote className="trading-empty-note">
          该次签名读取未见非零仓位；下一次读取前状态可能变化。
        </EmptyNote>
      )}
      {account.orders
        .map((order) => ({
          symbol: order.symbol,
          clientId: order.clientOrderId,
          side: order.side,
          status: order.status,
          owned: order.owned,
        }))
        .concat(
          account.algos.map((order) => ({
            symbol: order.symbol,
            clientId: order.clientAlgoId,
            side: order.orderType,
            status: order.algoStatus,
            owned: order.owned,
          })),
        )
        .map((order) => (
          <p className="trading-current-order-row" key={order.clientId}>
            <b>{order.symbol}</b> · {order.side ?? "—"} · {order.status ?? "—"} · {order.clientId}
            {!order.owned ? " · 未认领" : ""}
          </p>
        ))}
    </>
  );
}

function Fact({ label, value }: { label: string; value: ReactNode }) {
  return (
    <span className="trading-fact">
      <small>{label}</small>
      <b>{value}</b>
    </span>
  );
}

function safety(value: boolean, stale: boolean): string {
  return stale ? "待确认" : value ? "是" : "否";
}

function observedTime(value: number | null | undefined): string {
  return value == null ? "未取得" : new Date(value).toLocaleString("zh-CN");
}
