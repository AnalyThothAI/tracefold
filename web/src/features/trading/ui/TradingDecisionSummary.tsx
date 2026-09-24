import { EmptyNote } from "@shared/ui/EmptyNote";

import type { TradingCases } from "../api/tradingQueries";
import { ledgerSentence } from "../model/tradingLabels";

const sum = (values: number[]) => values.reduce((total, value) => total + value, 0);

/** Each number keeps its own ledger scope: a Case, a Decision, and a Signal are different facts. */
export function TradingDecisionSummary({
  cases,
  failed,
  pending,
  onBrowse,
}: {
  cases: TradingCases | undefined;
  failed: boolean;
  pending: boolean;
  onBrowse: () => void;
}) {
  if (!cases)
    return <EmptyNote>{ledgerSentence({ failed, pending, subject: "策略判定" })}</EmptyNote>;

  const states = cases.state_counts_24h ?? {};
  const hasDecisionCounts = cases.decision_counts_24h != null;
  const decisions = cases.decision_counts_24h ?? [];
  const admissions = cases.admission_counts_24h ?? [];
  const count = (action: string, publishStatus?: string) =>
    sum(
      decisions
        .filter(
          (row) =>
            row.action === action &&
            (publishStatus === undefined || row.publish_status === publishStatus),
        )
        .map((row) => row.count),
    );
  const trade = count("TRADE");
  const published = count("TRADE", "published");
  const shadow = count("TRADE", "shadow");
  const withheld = trade - published - shadow;
  const caseCount = sum(Object.values(states));
  const admitted = sum(
    admissions.filter((row) => row.status === "CASE_CREATED").map((row) => row.count),
  );
  const stopped = sum(
    admissions.filter((row) => row.status !== "CASE_CREATED").map((row) => row.count),
  );

  return (
    <section className="trading-journey" aria-labelledby="trading-journey-title">
      <div className="trading-journey-heading">
        <div>
          <span className="trading-eyebrow">DECISION PIPELINE · 最近 24 小时</span>
          <h2 id="trading-journey-title">一条市场线索，如何走到交易</h2>
          <p>
            先筛选来源，再冻结 Case，由 DSPy Agent 给出判断；只有发布的交易判断才会形成 Signal。
          </p>
        </div>
        <button type="button" className="trading-journey-link" onClick={onBrowse}>
          查看判定记录 <span aria-hidden="true">↗</span>
        </button>
      </div>
      <div className="trading-journey-grid">
        <div className="trading-journey-step">
          <span className="trading-journey-number">01 / 来源准入</span>
          <strong>{admitted}</strong>
          <span className="trading-journey-main">OI 来源形成 Case</span>
          <small>{stopped} 条 OI 来源被拒绝、过期或暂缓</small>
        </div>
        <div className="trading-journey-step">
          <span className="trading-journey-number">02 / 冻结事实</span>
          <strong>{caseCount}</strong>
          <span className="trading-journey-main">全部来源 Case 创建</span>
          <small>
            {states.EXCLUDED ?? 0} 条目标排除 · {states.DONE ?? 0} 条完成分析
          </small>
        </div>
        <div className="trading-journey-step trading-journey-step-agent">
          <span className="trading-journey-number">03 / DSPy AGENT</span>
          <strong>{hasDecisionCounts ? sum(decisions.map((row) => row.count)) : "—"}</strong>
          <span className="trading-journey-main">已保存的分析判断</span>
          <small>
            {hasDecisionCounts
              ? `交易 ${trade} · 观察 ${count("WATCH")} · 不交易 ${count("NO_TRADE")}`
              : "等待新接口提供判断分布"}
          </small>
        </div>
        <div className="trading-journey-step">
          <span className="trading-journey-number">04 / 发布信号</span>
          <strong>{hasDecisionCounts ? published : "—"}</strong>
          <span className="trading-journey-main">TRADE 判断已发布</span>
          <small>
            {hasDecisionCounts
              ? `影子 ${shadow} · 阻断或失效 ${withheld}`
              : "等待新接口提供发布状态"}
          </small>
        </div>
        <div className="trading-journey-step trading-journey-step-final">
          <span className="trading-journey-number">05 / 执行与复盘</span>
          <span className="trading-journey-final-icon" aria-hidden="true">
            ↗
          </span>
          <span className="trading-journey-main">交给独立执行链路</span>
          <small>订单、成交与盈亏以执行账本为准</small>
        </div>
      </div>
      <p className="trading-journey-footnote">
        来源准入只统计 OI；Case 包含其他来源和观察复核。各环节有独立口径，数字不表示逐级转化；Signal
        也不等于成交。
      </p>
    </section>
  );
}
