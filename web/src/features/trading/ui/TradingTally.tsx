import { Card } from "@shared/ui/Card";
import { SourceLine } from "@shared/ui/SourceLine";

import type {
  TradingExecutionReadiness,
  TradingExecutionRow,
  TradingRealizedTotals,
} from "../api/tradingQueries";
import { entrySplit } from "../model/tradingCases";
import { moneyLabel, moneyTone } from "../model/tradingLabels";

/**
 * ② Today, in four numbers: what the account made, what it risked, and what is still on it (#604 T4).
 *
 * The realized pair is `totals` — the server's own sums over the `position` observations it closed, one
 * bounded to the current UTC day and one unbounded. The desk used to print a third number instead: the
 * sum of the realized column on the rows it happened to be showing, which is neither today's result nor
 * the account's. Both of these include the manual entries an operator typed at the CLI, and the caption
 * says so, because leaving them out would make the desk disagree with the venue about the same account.
 *
 * 今日入场 and 当前敞口 are counts of what is already on screen — the entries in the window split by whether
 * the Runtime took them, and the positions and orders the account holds. Neither is an opinion; a zero is
 * a real answer and reads as one.
 */
export function TradingTally({
  execution,
  executions,
  executionsFailed,
  executionsPending,
  totals,
}: {
  execution: TradingExecutionReadiness | undefined;
  executions: readonly TradingExecutionRow[];
  executionsFailed: boolean;
  executionsPending: boolean;
  totals: TradingRealizedTotals | undefined;
}) {
  const account = execution?.current_account;
  const venue = entrySplit(executions);
  const unread = executionsPending ? "读取中" : executionsFailed ? "读取失败" : "UNAVAILABLE";
  return (
    <Card
      data-block="tally"
      hint="含手工入场，按 UTC 日界聚合；已记录成交手续费计入，资金费未计入"
      title="今日战况"
    >
      {/*
       * `.trading-fact-grid` rather than `@shared/ui/FactGrid`: that primitive drops a pair whose value is
       * empty, and a desk that answers with capital on the line must print UNAVAILABLE where a number is
       * missing. A silently absent tile and a tile reading zero are the two things this band may not confuse.
       */}
      <div className="trading-fact-grid" data-columns="4">
        <span className="trading-fact" data-tone={moneyTone(totals?.realized_known_today_usd)}>
          <small>今日已知已实现盈亏</small>
          <b>{totals ? moneyLabel(totals.realized_known_today_usd) : unread}</b>
          <small>
            {totals
              ? `平仓 ${totals.closed_today} · 已知 ${totals.pnl_known_today} · 缺失 ${totals.pnl_missing_today}`
              : "读自 totals"}
          </small>
        </span>
        <span className="trading-fact" data-tone={moneyTone(totals?.realized_known_total_usd)}>
          <small>累计已知已实现盈亏</small>
          <b>{totals ? moneyLabel(totals.realized_known_total_usd) : unread}</b>
          <small>
            {totals
              ? `平仓 ${totals.closed_total} · 已知 ${totals.pnl_known_total} · 缺失 ${totals.pnl_missing_total}`
              : "读自 totals"}
          </small>
        </span>
        <span className="trading-fact">
          <small>所列入场</small>
          <b>{executionsPending || executionsFailed ? unread : executions.length}</b>
          <small>
            受理 {venue.accepted} · 拒绝 {venue.refused}
          </small>
        </span>
        <span
          className="trading-fact"
          data-tone={account?.positions?.length ? "caution" : undefined}
        >
          <small>当前敞口</small>
          <b>{account ? (account.positions?.length ?? 0) : "UNAVAILABLE"}</b>
          <small>{account ? `挂单 ${account.open_orders_count}` : "读自 /status"}</small>
        </span>
      </div>
      {totals && !totals.pnl_complete_total ? (
        <p className="trading-empty-note" data-tone="caution">
          盈亏或成交历史不完整，以上仅为已知部分，不能视为账户完整净利润。
        </p>
      ) : null}
      <SourceLine path="GET /api/trading/executions → totals · GET /api/trading/status → execution.current_account" />
    </Card>
  );
}
