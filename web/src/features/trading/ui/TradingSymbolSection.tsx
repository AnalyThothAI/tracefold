import { Card } from "@shared/ui/Card";
import { ChevronRight } from "lucide-react";
import { useState } from "react";

import { useTradingOrdersWithToken } from "../api/tradingQueries";
import {
  CASE_STATE_ZH,
  ORDER_STATE_NOTE,
  REGIME_ZH,
  STRATEGY_ZH,
  caseClock,
  preMoveLabel,
  realizedLabel,
  strategyCaseLabel,
} from "../model/tradingLabels";
import {
  policyRuleZh,
  tradingLedgerEntries,
  type TradingOiLedgerEntry,
} from "../model/tradingOiLedger";

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
          ? `${order.state} · ${order.mode} ${order.side === "buy" ? "买入" : "卖出"} ${order.notional_usd} @ ${order.average_price ?? order.entry_reference} · 止损 ${order.stop_price}`
          : "未成单",
        note: order ? (order.state_reason ?? ORDER_STATE_NOTE[order.state] ?? "") : "",
        preMoveBps: value.pre_move_bps ?? null,
        realized: order?.realized_bps ?? null,
        regime: value.regime,
        rule: value.policy_reason,
        /*
         * The case's state under the 案例状态 heading, for both halves. An order row used to put its own
         * ORDER state here — a disjoint vocabulary — so one column showed `CLOSED` (English, an order
         * fact, and not a case state at all) beside 地板拒绝 (Chinese, a case fact). The endpoint has
         * published `case_state` since #207 PR-W4 and nothing read it. The order's own state keeps its
         * place in the 订单 column, which is where order facts belong.
         */
        state: caseState(entry),
        stateZh: CASE_STATE_ZH[caseState(entry)] ?? caseState(entry),
        strategyId: value.strategy_id,
      };
    })
    .sort((a, b) => b.observedAtMs - a.observedAtMs);

  return (
    <Card
      flush
      hint={`过去 ${query.data?.window_hours ?? 24} 小时开的案，加上仍在场的旧单；冻结在 cutoff 的输入，判到哪一步、为什么停，都有名字`}
      title="交易复盘 · 这个代币的案例"
    >
      {rows.length === 0 ? (
        <TradingEmptyNote>
          {/* An unanswered read is not an empty ledger; #282's review caught both saying the same thing. */}
          {query.isError && !query.data
            ? "资本通道的账本这次没读到——这不是「没有案例」，是没读到；下一轮轮询会再问一次。"
            : query.data == null
              ? "正在读资本通道的账本…"
              : `过去 ${query.data.window_hours ?? 24} 小时资本通道没有为这个代币开过案，也没有更早的单还在场。` +
                (!query.data.orders?.length && !query.data.cases_without_orders?.length
                  ? "（通道未启用时这里恒为空。）"
                  : "")}
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
                {/* The chip is the artifact's compact code and the panel above names the same strategy in
                    Chinese; the title carries both so one strategy is not two names on one screen. */}
                <span
                  className="trading-kind"
                  title={`${STRATEGY_ZH[row.strategyId] ?? row.strategyId} · strategy_id: ${row.strategyId}`}
                >
                  {strategyCaseLabel(row.strategyId)}
                </span>
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
                <span className="trading-num">
                  {/* The lane's shared `.trading-num b[data-tone]`, not a second contract beside it: the
                      fork dropped the shared weight and disagreed with 今日已了结 about zero. */}
                  {row.realized == null ? (
                    <span className="trading-unmeasured" title="出场未被测量，不进入已实现口径">
                      —
                    </span>
                  ) : (
                    <b data-tone={row.realized >= 0 ? "up" : "down"}>
                      {realizedLabel(row.realized)}
                    </b>
                  )}
                </span>
              </button>
              {open === row.id ? (
                <div className="trading-case-detail">
                  <div>
                    <small>停在哪条规则 · NAMED RULE</small>
                    {/*
                     * `policy_reason` is written only by `settle_case`, and only from PENDING/RUNNING, so
                     * a null one means the case has not stopped yet — not that the ledger holds no record
                     * of where it stopped. Backlogged turns and a worker that died mid-decision both
                     * leave rows in exactly that state, and they render here.
                     */}
                    <p>
                      {row.rule
                        ? policyRuleZh(row.rule)
                        : row.state === "PENDING" || row.state === "RUNNING"
                          ? "这条案例还没有判完——停在哪条规则要等它终结时才写入账本。"
                          : "账本没有记录停在哪条规则上。"}
                    </p>
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
/** The case's own state, whichever half of the batch the row came from. */
function caseState(entry: TradingOiLedgerEntry): string {
  return entry.kind === "order" ? entry.value.case_state : entry.value.state;
}
