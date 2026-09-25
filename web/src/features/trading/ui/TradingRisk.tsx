import { Card } from "@shared/ui/Card";
import { EmptyNote } from "@shared/ui/EmptyNote";
import { Metric, MetricRow } from "@shared/ui/Metric";
import { SourceLine } from "@shared/ui/SourceLine";
import type { ReactNode } from "react";

import type { TradingExecutionReadiness } from "../api/tradingQueries";
import {
  bpsPercent,
  entryBlockReasonLabel,
  moneyLabel,
  orderLegLabel,
  protectionStatusLabel,
} from "../model/tradingLabels";

/**
 * The two blocks `/api/trading/status` owns, and the reason they are not adjacent on the desk (#604 T4).
 *
 * ① is the strip an operator reads first and ⑤ is the detail they open only when something is on the
 * account; the tally, the funnel and the ledger sit between them. They stay one module because they are
 * one read and one vocabulary — split across two files, the second would eventually consult a second
 * source for the same account.
 *
 * `stale` is the page's one freshness comparison — `Date.now() > execution.facts_expire_at_ms`, the instant
 * the server itself published as the end of this projection's budget (the Runtime heartbeat plus five
 * seconds). Past it the safety words are not what the response says they are, so they read 待确认 rather
 * than the browser recomputing a heartbeat age of its own and disagreeing with the server about it.
 */
export function TradingSafetyStrip({
  execution,
  stale,
}: {
  execution: TradingExecutionReadiness;
  stale: boolean;
}) {
  return (
    <div className="trading-risk" data-block="safety">
      {/*
       * Two words. `当前仓位可保护 / 退出` was the third and answered `execution_safe`, a claim about the
       * Runtime's private account proof; Nautilus owns execution state now and reconciles the venue itself
       * (#680), so the proof and the tile went together. What remains is whether the process is alive and
       * whether it will take a new entry — and if not, the reason it names.
       */}
      <MetricRow className="trading-safety-grid" columns={2} label="执行安全状态">
        <Metric
          eyebrow="执行服务在线"
          value={safety(execution.alive, stale)}
          caption="执行进程与事件循环"
          tone={!stale && execution.alive ? "accent" : "caution"}
        />
        <Metric
          eyebrow="允许新增仓位"
          value={safety(execution.entries_armed, stale)}
          caption={stale ? "等待新状态" : entryBlockReasonLabel(execution.entry_block_reason)}
          tone={!stale && execution.entries_armed ? "accent" : "caution"}
        />
      </MetricRow>
      {stale ? (
        <p role="status" className="trading-alert-line" data-tone="caution">
          状态待确认：未取得有效期内的新状态，无法确认当前运行状态；下方保留上次读取的仓位与订单。
        </p>
      ) : null}
      <p className="trading-routes-line">
        可执行市场 {execution.routes_count} 个 · 账户槽位 <code>{execution.account_slot}</code>
      </p>
    </div>
  );
}

/**
 * ⑤ Exposure and protection, closed while there is nothing on the account.
 *
 * The positions list, the order list and the protection strip can all be empty. The
 * block opens itself the moment the account holds a position or an order, or the Runtime reports exposure
 * no trade plan claims, and stays open until that is no longer true.
 *
 * Everything here is the Runtime's own Nautilus Cache as the last heartbeat published it (#680). A
 * position is protected when both its stop and its take-profit rest on the venue; the strip prints the two
 * trigger prices rather than a coverage verdict the browser would have to compute. An empty list is what
 * the Cache shows, stated as such — the desk makes no separate claim that the account is flat.
 */
export function TradingExposure({
  execution,
  stale,
}: {
  execution: TradingExecutionReadiness;
  stale: boolean;
}) {
  const account = execution.current_account;
  const positions = account?.positions ?? [];
  const orders = account?.orders ?? [];
  const open = positions.length > 0 || orders.length > 0 || execution.unexpected_exposure;
  return (
    <Card data-block="exposure" flush title="当前仓位与保护">
      <details className="trading-exposure" open={open}>
        <summary>
          <span>
            仓位 {positions.length} · 挂单 {account?.open_orders_count ?? "—"} · 保护{" "}
            {stale ? "待确认" : protectionStatusLabel(execution.protection_status)}
          </span>
          <small>
            {open
              ? "当前账户仓位与订单"
              : account == null
                ? "未取得 Runtime 账户快照"
                : stale
                  ? "上次读取未见仓位"
                  : "Runtime 当前未见仓位"}
          </small>
        </summary>

        {execution.unexpected_exposure ? (
          <p className="trading-alert-line" data-tone="alert">
            Runtime 报告了无计划认领的敞口，新入场已被阻止；当前敞口需要由运维核查。
          </p>
        ) : null}

        <div className="trading-fact-grid">
          <Fact label="账户权益" value={moneyLabel(account?.equity_usd)} />
          <Fact
            label="当日回撤"
            value={
              account?.daily_drawdown_usd == null
                ? "未取得"
                : `${moneyLabel(account.daily_drawdown_usd)} · ${bpsPercent(account.daily_drawdown_bps)}`
            }
            warn={Number(account?.daily_drawdown_usd ?? 0) > 0}
          />
          <Fact
            label="账户事实"
            value={account?.complete ? "完整" : account ? "部分资料缺失" : "未取得"}
            warn={!account?.complete}
          />
          <Fact label="在途订单" value={account?.inflight_orders_count ?? "—"} />
        </div>

        {positions.length ? (
          <div className="trading-position-list">
            {positions.map((position) => {
              const guarded =
                position.stop_trigger_price != null && position.take_profit_trigger_price != null;
              return (
                <article className="trading-position-row" key={position.position_id}>
                  <div className="trading-position-identity">
                    <b>{position.instrument_id}</b>
                    <span data-tone={position.side === "long" ? "long" : "short"}>
                      {position.side === "long" ? "多仓" : "空仓"}
                    </span>
                    {!position.owned ? <span data-tone="alert">无计划认领</span> : null}
                  </div>
                  <div className="trading-position-facts">
                    <Fact label="数量" value={position.quantity} />
                    <Fact label="入场均价" value={position.entry_price} />
                    <Fact label="标记价格" value={position.mark_price ?? "未取得"} />
                    <Fact
                      label="未实现盈亏"
                      value={moneyLabel(position.unrealized_pnl_usd)}
                      warn={position.unrealized_pnl_usd == null}
                    />
                  </div>
                  <div
                    className="trading-protection-strip"
                    data-tone={!stale && guarded ? "protected" : "caution"}
                  >
                    <b>
                      {stale
                        ? "保护事实已过期"
                        : protectionStatusLabel(guarded ? "protected" : "unprotected")}
                    </b>
                    <span>止损 {position.stop_trigger_price ?? "未挂"}</span>
                    <span>止盈 {position.take_profit_trigger_price ?? "未挂"}</span>
                  </div>
                </article>
              );
            })}
          </div>
        ) : (
          <EmptyNote className="trading-empty-note">
            {account == null
              ? "未取得 Runtime 账户快照，不能据此断言没有仓位。"
              : stale
                ? "上次读取时 Runtime 未见仓位；当前状态待确认。"
                : "Runtime 当前未见仓位。"}
          </EmptyNote>
        )}

        {orders.length ? (
          <div className="trading-current-order-list">
            {orders.map((order) => (
              <article className="trading-current-order-row" key={order.client_order_id}>
                <b>{order.instrument_id}</b>
                <span>{order.state.toUpperCase()}</span>
                <span data-tone={order.leg === "unknown" ? "caution" : undefined}>
                  {orderLegLabel(order.leg)} · Qty {order.quantity}
                </span>
                <span>Trigger {order.trigger_price ?? "—"}</span>
                <span data-tone={!order.owned ? "caution" : undefined}>
                  {order.owned ? "OWNED" : "无计划认领"}
                  {order.reduce_only ? " · REDUCE ONLY" : ""}
                </span>
              </article>
            ))}
          </div>
        ) : (
          <p className="trading-inline-empty">
            {account == null
              ? "未取得挂单与在途订单。"
              : stale
                ? "上次读取时未见挂单或在途订单。"
                : "Runtime 当前未见挂单或在途订单。"}
          </p>
        )}
      </details>
      <SourceLine path="GET /api/trading/status → execution.current_account" />
    </Card>
  );
}

function Fact({ label, value, warn = false }: { label: string; value: ReactNode; warn?: boolean }) {
  return (
    <span className="trading-fact" data-tone={warn ? "caution" : undefined}>
      <small>{label}</small>
      <b>{value}</b>
    </span>
  );
}

function safety(value: boolean, stale: boolean): string {
  if (stale) return "待确认";
  return value ? "是" : "否";
}
