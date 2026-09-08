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
 * the server itself published as the end of this projection's budget. Past it the safety words are not what
 * the response says they are, so they read 过期 rather than the browser recomputing a heartbeat age and a
 * reconciliation age of its own and disagreeing with the server about both.
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
       * Three words, not four. `FLAT` was the fourth and has read `NOT PROVEN` around the clock in
       * production: `ExecutionRuntimeState.account_flat` stays false with zero positions, so the tile was a
       * permanently amber quarter of the strip that no operator could act on. That is a writer-side defect
       * and it is tracked as one; here the proof is a footnote of the exposure block, where an empty
       * position list is the claim it qualifies.
       */}
      <MetricRow className="trading-safety-grid" columns={3} label="执行安全状态">
        <Metric
          eyebrow="执行服务在线"
          value={safety(execution.alive, stale)}
          caption="执行进程与事件循环"
          tone={!stale && execution.alive ? "accent" : "caution"}
        />
        <Metric
          eyebrow="当前仓位可保护 / 退出"
          value={safety(execution.execution_safe, stale)}
          caption="不代表账户资料完整"
          tone={!stale && execution.execution_safe ? "accent" : "caution"}
        />
        <Metric
          eyebrow="允许新增仓位"
          value={safety(execution.entries_armed, stale)}
          caption={stale ? "事实已过期" : entryBlockReasonLabel(execution.entry_block_reason)}
          tone={!stale && execution.entries_armed ? "accent" : "caution"}
        />
      </MetricRow>
      <p className="trading-routes-line">
        可执行市场 {execution.routes_count} 个 · 账户槽位 <code>{execution.account_slot}</code>
        {stale ? " · 本次读取的事实已过期" : ""}
      </p>
    </div>
  );
}

/**
 * ⑤ Exposure and protection, closed while there is nothing on the account.
 *
 * `max_positions` is 1 and the lane emits a handful of Signals a day, so the positions list, the order list
 * and the protection strip are empty most of the time — three empty regions holding a full card each. The
 * block opens itself the moment the account holds a position or an order, or the Runtime reports exposure
 * it does not own, and stays open until that is no longer true.
 *
 * The audit tile went with it. `audit_healthy` is true around the clock, so `审计写入 HEALTHY` was a
 * constant occupying a cell; an unhealthy audit is now an alert line, which is the only state a reader has
 * to act on. `unexpected_exposure` gets the same treatment and had no rendering at all before.
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
            {protectionStatusLabel(execution.protection_status)}
          </span>
          <small>
            {open
              ? "当前账户仓位与订单"
              : !stale && execution.account_flat_proven
                ? "已核实空仓"
                : "未见仓位，账户为空尚未证实"}
          </small>
        </summary>

        {execution.unexpected_exposure ? (
          <p className="trading-alert-line" data-tone="alert">
            Runtime 报告了无主敞口；在处置之前不要解除 ARMED 之外的任何限制。
          </p>
        ) : null}
        {account != null && !account.audit_healthy ? (
          <p className="trading-alert-line" data-tone="alert">
            账户事实写入审计失败 · {account.audit_failure_reason ?? "UNHEALTHY"}
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
          <Fact label="总风险金额" value={moneyLabel(account?.aggregate_risk_usd)} />
          <Fact
            label="私有对账距今"
            value={
              execution.reconciliation_age_ms == null
                ? "未取得"
                : `${execution.reconciliation_age_ms.toLocaleString("en-US")} ms`
            }
            warn={
              execution.reconciliation_age_ms == null || execution.reconciliation_age_ms > 10_000
            }
          />
          <Fact
            label="账户事实"
            value={account?.complete ? "完整" : account ? "部分资料缺失" : "未取得"}
            warn={!account?.complete}
          />
          <Fact
            label="处理中 / 未知订单"
            value={`${account?.inflight_orders_count ?? "—"} / ${account?.unknown_orders_count ?? "—"}`}
            warn={Boolean(account?.unknown_orders_count)}
          />
        </div>

        {positions.length ? (
          <div className="trading-position-list">
            {positions.map((position) => (
              <article className="trading-position-row" key={position.position_id}>
                <div className="trading-position-identity">
                  <b>{position.instrument_id}</b>
                  <span data-tone={position.side === "long" ? "long" : "short"}>
                    {position.side === "long" ? "多仓" : "空仓"}
                  </span>
                  {!position.owned ? <span data-tone="alert">归属未确认</span> : null}
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
                  data-tone={!stale && position.protection_full_coverage ? "protected" : "caution"}
                >
                  <b>
                    {stale ? "保护事实已过期" : protectionStatusLabel(position.protection_status)}
                  </b>
                  <span>Qty {position.protection_quantity ?? "—"}</span>
                  <span>Trigger {position.protection_trigger_price ?? "—"}</span>
                  <span>{position.protection_full_coverage ? "全部覆盖" : "未全部覆盖"}</span>
                </div>
              </article>
            ))}
          </div>
        ) : (
          /*
           * The FLAT footnote. `account_flat_proven` is one fresh private reconciliation saying the slot
           * holds nothing — it qualifies this empty list and nothing else, which is why it is a sentence
           * here rather than a permanently amber word in the safety strip.
           */
          <EmptyNote className="trading-empty-note">
            {!stale && execution.account_flat_proven
              ? "当前账户无仓位，且新鲜 Binance 私有对账已证明账户为空。"
              : "未见当前仓位；这本身不能证明账户为空。"}
          </EmptyNote>
        )}

        {orders.length ? (
          <div className="trading-current-order-list">
            {orders.map((order) => (
              <article className="trading-current-order-row" key={order.client_order_id}>
                <b>{order.instrument_id}</b>
                <span>{order.state.toUpperCase()}</span>
                <span data-tone={order.leg === "unknown" ? "caution" : undefined}>
                  {order.leg.toUpperCase()} · Qty {order.quantity}
                </span>
                <span>Trigger {order.trigger_price ?? "—"}</span>
                <span data-tone={!order.owned ? "caution" : undefined}>
                  {order.owned ? "OWNED" : "归属未确认"}
                  {order.reduce_only ? " · REDUCE ONLY" : ""}
                </span>
              </article>
            ))}
          </div>
        ) : (
          <p className="trading-inline-empty">未见 open / inflight order；不据此推断账户为空。</p>
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
  if (stale) return "过期";
  return value ? "是" : "否";
}
