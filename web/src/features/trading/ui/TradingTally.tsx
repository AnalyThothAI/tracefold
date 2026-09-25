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
 * The realized pair is `totals` — the server's own sums over every trade plan the slot opened and closed,
 * one bounded to the current UTC day and one unbounded. Each plan's result is folded from its fill journal
 * (#680): exit minus entry notional, signed by direction, less every commission the venue charged. PAPER
 * adds actual venue funding only after complete coverage. A closed plan with missing inputs is counted as missing, and
 * the caution line below says so whenever one exists. The desk used to print a third number instead: the
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
  const paper = (totals?.paper_closed_total ?? 0) > 0;
  const today = paper ? totals?.paper_net_known_today_usd : totals?.realized_known_today_usd;
  const total = paper ? totals?.paper_net_known_total_usd : totals?.realized_known_total_usd;
  const unread = executionsPending ? "读取中" : executionsFailed ? "读取失败" : "UNAVAILABLE";
  return (
    <Card
      data-block="tally"
      hint="含手工入场，按 UTC 日界聚合；PAPER 净值需场所资金费完整覆盖"
      title="今日战况"
    >
      {/*
       * `.trading-fact-grid` rather than `@shared/ui/FactGrid`: that primitive drops a pair whose value is
       * empty, and a desk that answers with capital on the line must print UNAVAILABLE where a number is
       * missing. A silently absent tile and a tile reading zero are the two things this band may not confuse.
       */}
      <div className="trading-fact-grid" data-columns="4">
        <span className="trading-fact" data-tone={moneyTone(today)}>
          <small>{paper ? "今日已知 PAPER 净收益" : "今日已知手续费后盈亏"}</small>
          <b>{totals ? moneyLabel(today) : unread}</b>
          <small>
            {totals
              ? paper
                ? `PAPER 平仓 ${totals.paper_closed_today} · 已知 ${totals.paper_net_known_today} · 缺失 ${totals.paper_net_missing_today}`
                : `平仓 ${totals.closed_today} · 已知 ${totals.pnl_known_today} · 缺失 ${totals.pnl_missing_today}`
              : "读自 totals"}
          </small>
        </span>
        <span className="trading-fact" data-tone={moneyTone(total)}>
          <small>{paper ? "累计已知 PAPER 净收益" : "累计已知手续费后盈亏"}</small>
          <b>{totals ? moneyLabel(total) : unread}</b>
          <small>
            {totals
              ? paper
                ? `PAPER 平仓 ${totals.paper_closed_total} · 已知 ${totals.paper_net_known_total} · 缺失 ${totals.paper_net_missing_total}`
                : `平仓 ${totals.closed_total} · 已知 ${totals.pnl_known_total} · 缺失 ${totals.pnl_missing_total}`
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
      {totals && (paper ? totals.paper_net_missing_total : totals.pnl_missing_total) > 0 ? (
        <p className="trading-empty-note" data-tone="caution">
          {paper
            ? `${totals.paper_net_missing_total} 笔 PAPER 平仓交易缺少完整成交、手续费或资金费归因；已知部分不能视为账户完整净利润。`
            : `${totals.pnl_missing_total} 笔已平仓交易的成交或手续费记录不全，盈亏未计入；以上仅为已知部分，不能视为账户完整净利润，资金费未计入。`}
        </p>
      ) : null}
      <SourceLine path="GET /api/trading/executions → totals · GET /api/trading/status → execution.current_account" />
    </Card>
  );
}
