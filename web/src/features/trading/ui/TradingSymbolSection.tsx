import { Card } from "@shared/ui/Card";
import { ChevronRight } from "lucide-react";
import { useState } from "react";

import { useTradingOrdersWithToken } from "../api/tradingQueries";
import {
  CASE_STATE_ZH,
  ORDER_STATE_NOTE,
  REGIME_ZH,
  caseClock,
  preMoveLabel,
  realizedLabel,
  strategyCaseLabel,
} from "../model/tradingLabels";
import { policyRuleZh, tradingLedgerEntries } from "../model/tradingOiLedger";

import { TradingEmptyNote, TradingSourceLine } from "./TradingChrome";

import "./trading.css";
import "./tradingSymbolSection.css";

/**
 * 交易复盘 — every case the capital lane opened for one token (artifact v8, #282).
 *
 * Rendered on the token page, which belongs to News. The section is owned here rather than there because
 * the vocabulary is the capital lane's: case states, order states and the rule keys a case stopped on. A
 * copy of those words inside News would be a second place they could drift.
 *
 * Seven columns, in the order the lane decided them: when the fact was observed, which strategy read it,
 * the quadrant and the move that had already happened, how far the case got, the rule it stopped on, the
 * intent it authored, and what came of it. Expanding a row shows the rule in Chinese beside the thresholds
 * that case was frozen against.
 *
 * The artifact draws a `thesis_zh` and an `invalidation_zh` in that expansion. This lane writes neither:
 * `oi_smart_money_momentum_v1` is a pure rule and produces no narrative, and `thesis_zh` exists only for
 * the model lane (#256 settled the same question for 杠杆异动). The expansion names the rule instead of
 * paraphrasing a sentence nobody wrote.
 *
 * `underlying` is the base symbol; the endpoint resolves it to the ledger's `crypto:{BASE}` key so the
 * browser never constructs a Trading identity of its own.
 */
export function TradingSymbolSection({ base, token }: { base: string; token: string }) {
  const query = useTradingOrdersWithToken(token, base);
  const [open, setOpen] = useState<string | null>(null);
  const rows = [...tradingLedgerEntries(query.data).values()]
    .map((entry) => {
      const value = entry.value;
      const order = entry.kind === "order" ? entry.value : null;
      return {
        config: value.strategy_config ?? {},
        id: value.case_id,
        observedAtMs:
          entry.kind === "order"
            ? (entry.value.case_observed_at_ms ?? entry.value.created_at_ms)
            : entry.value.observed_at_ms,
        order: order
          ? `${order.mode} ${order.side === "buy" ? "买入" : "卖出"} ${order.notional_usd} @ ${order.average_price ?? order.entry_reference} · 止损 ${order.stop_price}`
          : "未成单",
        note: order ? (order.state_reason ?? ORDER_STATE_NOTE[order.state] ?? "") : "",
        preMoveBps: value.pre_move_bps ?? null,
        realized: order?.realized_bps ?? null,
        regime: value.regime,
        rule: value.policy_reason,
        state: order ? order.state : entry.value.state,
        stateZh: order ? order.state : (CASE_STATE_ZH[entry.value.state] ?? entry.value.state),
        strategyId: value.strategy_id,
      };
    })
    .sort((a, b) => b.observedAtMs - a.observedAtMs);

  return (
    <Card
      flush
      hint="冻结在 cutoff 的输入，判到哪一步、为什么停，都有名字"
      title="交易复盘 · 这个代币的案例"
    >
      {rows.length === 0 ? (
        <TradingEmptyNote>
          这个窗口里资本通道没有为这个代币开过案。
          {query.data && !query.data.orders?.length && !query.data.cases_without_orders?.length
            ? "（通道未启用时这里恒为空。）"
            : null}
        </TradingEmptyNote>
      ) : (
        <div className="trading-table">
          <div aria-hidden className="trading-case-head">
            <span>CUTOFF</span>
            <span>策略</span>
            <span>象限 · 帧前已走</span>
            <span>案例状态</span>
            <span>停在哪条规则</span>
            <span>订单</span>
            <span className="trading-num">结果</span>
          </div>
          {rows.map((row) => (
            <article className="trading-case" data-open={open === row.id || undefined} key={row.id}>
              <button
                aria-expanded={open === row.id}
                className="trading-case-row"
                onClick={() => setOpen((current) => (current === row.id ? null : row.id))}
                type="button"
              >
                <span className="trading-case-time">
                  <ChevronRight aria-hidden />
                  {caseClock(row.observedAtMs)}
                </span>
                <span className="trading-kind">{strategyCaseLabel(row.strategyId)}</span>
                <span className="trading-case-regime">
                  <b>{row.regime ? (REGIME_ZH[row.regime] ?? row.regime) : "象限未定"}</b>
                  <small>{preMoveLabel(row.preMoveBps, row.config)}</small>
                </span>
                <span className="trading-state" data-state={row.state}>
                  {row.stateZh}
                </span>
                <span className="trading-note">
                  {row.rule ? <code>{policyRuleZh(row.rule)}</code> : <span>—</span>}
                </span>
                <span className="trading-num trading-case-order">{row.order}</span>
                <b className="trading-num" data-realized={realizedTone(row.realized)}>
                  {realizedLabel(row.realized)}
                </b>
              </button>
              {open === row.id ? (
                <div className="trading-case-detail">
                  <div>
                    <small>停在哪条规则 · NAMED RULE</small>
                    <p>{row.rule ? policyRuleZh(row.rule) : "账本没有记录停在哪条规则上。"}</p>
                    {row.note ? <p>{row.note}</p> : null}
                  </div>
                  <div>
                    <small>冻结清单 · STRATEGY_CONFIG</small>
                    {Object.keys(row.config).length === 0 ? (
                      <p>这条案例冻结在 #273 之前，账本没有留下它当时被什么阈值判过。</p>
                    ) : (
                      <dl className="trading-case-manifest">
                        {Object.entries(row.config).map(([key, value]) => (
                          <div key={key}>
                            <dt>{key}</dt>
                            <dd>{value}</dd>
                          </div>
                        ))}
                      </dl>
                    )}
                  </div>
                </div>
              ) : null}
            </article>
          ))}
        </div>
      )}
      <TradingSourceLine path="GET /api/trading/orders?underlying={base} → orders[] · cases_without_orders[]" />
    </Card>
  );
}

/** Red is 利多 on this console, so a gain is red and a loss is green — the mainland reading. */
function realizedTone(bps: number | null): "up" | "down" | undefined {
  if (bps == null || bps === 0) return undefined;
  return bps > 0 ? "up" : "down";
}
