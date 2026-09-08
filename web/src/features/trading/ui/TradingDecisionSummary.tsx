import { Card } from "@shared/ui/Card";
import { EmptyNote } from "@shared/ui/EmptyNote";
import { Metric, MetricRow } from "@shared/ui/Metric";

import type { TradingCases } from "../api/tradingQueries";
import {
  admissionReasonLabel,
  admissionStatusLabel,
  policyReasonLabel,
  ledgerSentence,
} from "../model/tradingLabels";

export function TradingDecisionSummary({
  cases,
  failed,
  pending,
  onReason,
}: {
  cases: TradingCases | undefined;
  failed: boolean;
  pending: boolean;
  onReason: (reason: string) => void;
}) {
  if (!cases)
    return <EmptyNote>{ledgerSentence({ failed, pending, subject: "策略判定" })}</EmptyNote>;
  return (
    <Card
      title="最近 24 小时 · 判定分布"
      hint="来源准入与策略判定分别计数，未与成交记录混算转化率。"
      flush
    >
      <MetricRow columns={3} label="策略判定分布">
        <Metric eyebrow="不交易" value={cases.state_counts_24h?.NO_TRADE ?? 0} />
        <Metric eyebrow="已发出信号" value={cases.state_counts_24h?.SIGNAL_EMITTED ?? 0} />
        <Metric eyebrow="判定受阻" value={cases.state_counts_24h?.BLOCKED ?? 0} />
      </MetricRow>
      <div className="trading-count-list">
        {Object.entries(cases.reason_counts_24h ?? {}).map(([reason, count]) => (
          <button
            className="trading-count-row"
            key={reason}
            type="button"
            onClick={() => onReason(reason)}
          >
            <span>{policyReasonLabel(reason)}</span>
            <b>{count} · 查看判定</b>
          </button>
        ))}
      </div>
      <details className="trading-admission-details">
        <summary>来源准入分布 · 未成案的来源记录</summary>
        <div className="trading-count-list">
          {(cases.admission_counts_24h ?? []).map((row) => (
            <span className="trading-count-row" key={`${row.status}:${row.reason}`}>
              <span>
                {admissionStatusLabel(row.status)} · {admissionReasonLabel(row.reason)}
              </span>
              <b>{row.count}</b>
            </span>
          ))}
        </div>
      </details>
    </Card>
  );
}
