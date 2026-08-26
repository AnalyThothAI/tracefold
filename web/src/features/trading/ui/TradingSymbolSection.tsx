import { Card } from "@shared/ui/Card";

import { useTradingOrdersWithToken } from "../api/tradingQueries";
import { CASE_STATE_ZH, ORDER_STATE_NOTE, REGIME_ZH, STRATEGY_ZH } from "../model/tradingLabels";

import { TradingEmptyNote, TradingSourceLine } from "./TradingChrome";

import "./trading.css";

/**
 * 交易复盘 — every case the capital lane opened for one token (#207 PR-W1's design, PR-W4's endpoint).
 *
 * Rendered on the token page, which belongs to News. The section is owned here rather than there because
 * the vocabulary is the capital lane's: case states, order states and the rule keys a case stopped on. A
 * copy of those words inside News would be a second place they could drift.
 *
 * `underlying` is the base symbol; the endpoint resolves it to the ledger's `crypto:{BASE}` key so the
 * browser never constructs a Trading identity of its own.
 */
export function TradingSymbolSection({ base, token }: { base: string; token: string }) {
  const query = useTradingOrdersWithToken(token, base);
  const orders = query.data?.orders ?? [];
  const cases = query.data?.cases_without_orders ?? [];
  const rows = [
    ...orders.map((order) => ({
      id: order.order_id,
      kind: order.strategy_id,
      note: order.state_reason ?? ORDER_STATE_NOTE[order.state] ?? "",
      order: `${order.side === "buy" ? "买入" : "卖出"} ${order.notional_usd} @ ${order.average_price ?? order.entry_reference} · 止损 ${order.stop_price}`,
      realized: order.realized_bps,
      regime: order.regime,
      state: order.state,
      stateZh: order.state,
      time: order.created_at_ms,
    })),
    ...cases.map((row) => ({
      id: row.case_id,
      kind: row.strategy_id,
      note: row.policy_reason ?? row.policy_decision ?? "",
      order: "未成案 · 未下单",
      realized: null,
      regime: row.regime,
      state: row.state,
      stateZh: CASE_STATE_ZH[row.state] ?? row.state,
      time: row.created_at_ms,
    })),
  ].sort((a, b) => b.time - a.time);

  return (
    <Card
      flush
      hint="冻结在 cutoff 的输入，判到哪一步、为什么停，都有名字"
      title="交易复盘 · 这个代币的所有案例"
    >
      {rows.length === 0 ? (
        <TradingEmptyNote>
          过去 24 小时资本通道没有为这个代币开过案。
          {query.data && !orders.length && !cases.length ? "（通道未启用时这里恒为空。）" : null}
        </TradingEmptyNote>
      ) : (
        <div className="trading-table">
          <div aria-hidden className="trading-case-head">
            <span>案例</span>
            <span>状态</span>
            <span>订单</span>
            <span>停在哪条规则</span>
          </div>
          {rows.map((row) => (
            <article className="trading-case-row" key={row.id}>
              <span className="trading-kind">{STRATEGY_ZH[row.kind] ?? row.kind}</span>
              <span className="trading-state" data-state={row.state}>
                {row.stateZh}
              </span>
              <span className="trading-num">{row.order}</span>
              <span className="trading-note">
                {row.note ? <code>{row.note}</code> : <span>—</span>}
                {row.regime ? <small>{REGIME_ZH[row.regime] ?? row.regime}</small> : null}
              </span>
            </article>
          ))}
        </div>
      )}
      <TradingSourceLine path="GET /api/trading/orders?underlying={base} → orders[] · cases_without_orders[]" />
    </Card>
  );
}
