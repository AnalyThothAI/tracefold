import { Card } from "@shared/ui/Card";
import { Metric, MetricRow } from "@shared/ui/Metric";
import type { ReactNode } from "react";

import type { TradingExecutionReadiness } from "../api/tradingQueries";
import { bpsPercent, entryBlockReasonLabel, moneyLabel } from "../model/tradingLabels";

/**
 * RISK: is the lane safe, and what is on the account (#537 PR-5, blocks 1 and 2 of #528 PR-2 merged).
 *
 * One strip of four safety words, four numbers beside it, and the positions the venue actually holds
 * with the protection covering each. The open / inflight / unknown order counts are that table's own
 * header rather than a card of their own — they are three integers about the same account, and a card
 * between the equity and the positions made them read as a fifth safety answer.
 *
 * `stale` is the page's one freshness comparison — `Date.now() > execution.facts_expire_at_ms`, the instant
 * the server itself published as the end of this projection's budget. Past it the four safety words are
 * not what the response says they are, so all four read 过期 rather than the browser recomputing a
 * heartbeat age and a reconciliation age of its own and disagreeing with the server about both.
 */
export function TradingRisk({
  execution,
  stale,
}: {
  execution: TradingExecutionReadiness;
  stale: boolean;
}) {
  const account = execution.current_account;
  const positions = account?.positions ?? [];
  const orders = account?.orders ?? [];
  return (
    <div className="trading-risk">
      <MetricRow className="trading-safety-grid" columns={4} label="执行安全状态">
        <Metric
          eyebrow="ALIVE"
          value={safety(execution.alive, stale, "YES", "NO")}
          caption="进程 / Node / event loop"
          tone={!stale && execution.alive ? "accent" : "caution"}
        />
        <Metric
          eyebrow="SAFE"
          value={safety(execution.execution_safe, stale, "YES", "NO")}
          caption="现有 exposure 可保护和退出"
          tone={!stale && execution.execution_safe ? "accent" : "caution"}
        />
        <Metric
          eyebrow="ARMED"
          value={safety(execution.entries_armed, stale, "YES", "NO")}
          caption={stale ? "事实已过期" : entryBlockReasonLabel(execution.entry_block_reason)}
          tone={!stale && execution.entries_armed ? "accent" : "caution"}
        />
        <Metric
          eyebrow="FLAT"
          value={safety(execution.account_flat_proven, stale, "PROVEN", "NOT PROVEN")}
          caption="新鲜 Binance 私有对账"
          tone={!stale && execution.account_flat_proven ? "accent" : "caution"}
        />
      </MetricRow>
      <p className="trading-routes-line">
        Runtime 可执行市场 {execution.routes_count} 个 · 账户槽位{" "}
        <code>{execution.account_slot}</code>
        {stale ? " · 本次读取的事实已过期" : ""}
      </p>

      <Card title="账户与风险">
        <div className="trading-fact-grid">
          <Fact label="Equity" value={moneyLabel(account?.equity_usd)} />
          <Fact
            label="当日 Drawdown"
            value={
              account?.daily_drawdown_usd == null
                ? "UNAVAILABLE"
                : `${moneyLabel(account.daily_drawdown_usd)} · ${bpsPercent(account.daily_drawdown_bps)}`
            }
            warn={Number(account?.daily_drawdown_usd ?? 0) > 0}
          />
          <Fact label="Aggregate risk" value={moneyLabel(account?.aggregate_risk_usd)} />
          <Fact
            label="Private reconcile age"
            value={
              execution.reconciliation_age_ms == null
                ? "UNAVAILABLE"
                : `${execution.reconciliation_age_ms.toLocaleString("en-US")} ms`
            }
            warn={
              execution.reconciliation_age_ms == null || execution.reconciliation_age_ms > 10_000
            }
          />
          <Fact
            label="账户事实"
            value={account?.complete ? "COMPLETE" : account ? "PARTIAL" : "UNAVAILABLE"}
            warn={!account?.complete}
          />
          <Fact
            /*
             * The audit the Runtime writes its own facts through. Unhealthy means the account read
             * happened and could not be recorded, which is a different fact from a missing snapshot —
             * so it names its own reason rather than degrading into UNAVAILABLE beside it.
             */
            label="审计写入"
            value={
              account == null
                ? "UNAVAILABLE"
                : account.audit_healthy
                  ? "HEALTHY"
                  : (account.audit_failure_reason ?? "UNHEALTHY")
            }
            warn={account != null && !account.audit_healthy}
          />
        </div>
      </Card>

      <Card hint="仓位与保护逐项展示；数量完整覆盖才标记 protected" title="当前仓位与挂单">
        <div className="trading-order-counts">
          <Fact label="Open" value={account?.open_orders_count ?? "—"} />
          <Fact label="Inflight" value={account?.inflight_orders_count ?? "—"} />
          <Fact
            label="Unknown"
            value={account?.unknown_orders_count ?? "—"}
            warn={Boolean(account?.unknown_orders_count)}
          />
          <Fact
            label="Protection"
            value={protectionLabel(execution.protection_status)}
            warn={["pending", "unprotected", "unknown"].includes(execution.protection_status)}
          />
        </div>

        {positions.length ? (
          <div className="trading-position-list">
            {positions.map((position) => (
              <article className="trading-position-row" key={position.position_id}>
                <div className="trading-position-identity">
                  <b>{position.instrument_id}</b>
                  <span data-tone={position.side === "long" ? "long" : "short"}>
                    {position.side.toUpperCase()}
                  </span>
                  {!position.owned ? <span data-tone="caution">UNCLAIMED</span> : null}
                </div>
                <div className="trading-position-facts">
                  <Fact label="Qty" value={position.quantity} />
                  <Fact label="Entry" value={position.entry_price} />
                  <Fact label="Mark" value={position.mark_price ?? "UNAVAILABLE"} />
                  <Fact
                    label="Unrealized PnL"
                    value={moneyLabel(position.unrealized_pnl_usd)}
                    warn={position.unrealized_pnl_usd == null}
                  />
                </div>
                <div
                  className="trading-protection-strip"
                  data-tone={position.protection_full_coverage ? "protected" : "caution"}
                >
                  <b>{protectionLabel(position.protection_status)}</b>
                  <span>Qty {position.protection_quantity ?? "—"}</span>
                  <span>Trigger {position.protection_trigger_price ?? "—"}</span>
                  <span>
                    {position.protection_full_coverage ? "FULL COVERAGE" : "NOT FULLY COVERED"}
                  </span>
                </div>
              </article>
            ))}
          </div>
        ) : (
          <p className="trading-empty-note">
            {!stale && execution.account_flat_proven
              ? "当前账户无仓位，且新鲜 Binance 私有对账已证明账户为空。"
              : "未见当前仓位；这本身不能证明账户为空。"}
          </p>
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
                  {order.owned ? "OWNED" : "UNCLAIMED"}
                  {order.reduce_only ? " · REDUCE ONLY" : ""}
                </span>
              </article>
            ))}
          </div>
        ) : (
          <p className="trading-inline-empty">未见 open / inflight order；不据此推断账户为空。</p>
        )}
      </Card>
    </div>
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

function safety(value: boolean, stale: boolean, yes: string, no: string): string {
  if (stale) return "过期";
  return value ? yes : no;
}

function protectionLabel(value: string): string {
  return (
    {
      not_applicable: "NOT APPLICABLE",
      pending: "PROTECTION PENDING",
      protected: "PROTECTED",
      unknown: "PROTECTION UNKNOWN",
      unprotected: "UNPROTECTED",
    }[value] ?? value.toUpperCase()
  );
}
