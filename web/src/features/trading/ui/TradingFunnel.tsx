import { newsSymbolPath } from "@shared/routing/paths";
import { Card } from "@shared/ui/Card";
import { Link } from "react-router-dom";

import type { TradingCase, TradingCounts, TradingFloors } from "../api/tradingQueries";
import { CASE_KIND_ZH, CASE_STATE_ZH, REGIME_ZH } from "../model/tradingLabels";

import { TradingEmptyNote, TradingSourceLine } from "./TradingChrome";

/**
 * Where today's cases went, and — for the ones that stopped — the rule they stopped on.
 *
 * The counts and the named rows come from two different reads of the same 24 h window, and the rejected
 * rows are the informative half: `POLICY_REJECTED` is where the capital floors actually bite, and those
 * cases have no order to join through. A funnel that only counted them would leave the console able to say
 * "four were rejected" and unable to say which four or why.
 *
 * The floors are shown beside them because a rejection reason means nothing without the number it failed.
 * They are the *capital* lane's thresholds and never the News gates — two sets, always side by side, neither
 * impersonating the other (#207 §4).
 */
export function TradingFunnel({
  cases,
  counts,
  floors,
}: {
  cases: readonly TradingCase[];
  counts: TradingCounts;
  floors: TradingFloors;
}) {
  const byState = counts.cases_by_state ?? {};
  const total = Object.values(byState).reduce((sum, value) => sum + value, 0);
  const bars: Array<[string, number]> = [
    ["成案", total],
    ["已下单", byState.ORDER_PREPARED ?? 0],
    ["不交易（带外）", byState.NO_TRADE ?? 0],
    ["地板拒绝", byState.POLICY_REJECTED ?? 0],
    ["已了结", counts.closed_orders ?? 0],
  ];
  const max = Math.max(1, ...bars.map(([, value]) => value));

  return (
    <Card flush title="今日案例去向" titleStyle="eyebrow">
      <div className="trading-funnel">
        {bars.map(([label, value]) => (
          <div className="trading-funnel-row" key={label}>
            <small>{label}</small>
            <span className="trading-funnel-track">
              <span style={{ width: `${Math.round((value / max) * 100)}%` }} />
            </span>
            <b>{value}</b>
          </div>
        ))}
        <p className="trading-funnel-note">
          <code>oi_only</code> 与 <code>news_only</code> 永不
          live——它们只在模拟仓里攒证据；只有对齐的 <code>news_oi</code> 才有资格走{" "}
          <code>live_reviewed</code>。
        </p>
      </div>

      <div className="trading-floors">
        <small>交易地板</small>
        <span>
          鲸鱼盈利 ≥ {(floors.min_whale_long_profit_bps / 100).toFixed(0)}% · 持仓 ≥{" "}
          {floors.min_oi_value_usd} · 帧前 {floors.min_price_move_bps}–{floors.max_price_move_bps}{" "}
          bps 带内
        </span>
      </div>

      {cases.length === 0 ? (
        <TradingEmptyNote>过去 24 小时没有停在判定之前的案例。</TradingEmptyNote>
      ) : (
        <div className="trading-table">
          <div aria-hidden className="trading-case-head">
            <span>标的</span>
            <span>案例</span>
            <span>状态</span>
            <span>停在哪条规则</span>
          </div>
          {cases.map((row) => (
            <article className="trading-case-row" key={row.case_id}>
              <span className="trading-symbol">
                <Link to={newsSymbolPath(row.base_symbol)}>{row.base_symbol}</Link>
              </span>
              <span className="trading-kind">{CASE_KIND_ZH[row.case_kind] ?? row.case_kind}</span>
              <span className="trading-state" data-state={row.state}>
                {CASE_STATE_ZH[row.state] ?? row.state}
              </span>
              <span className="trading-note">
                {/* The rule key verbatim: it is what an operator greps for, and it has no Chinese synonym. */}
                <code>{row.policy_reason ?? row.policy_decision ?? "—"}</code>
                {row.regime ? <small>{REGIME_ZH[row.regime] ?? row.regime}</small> : null}
              </span>
            </article>
          ))}
        </div>
      )}

      <TradingSourceLine path="GET /api/trading/status → counts · floors · GET /api/trading/orders → cases_without_orders" />
    </Card>
  );
}
