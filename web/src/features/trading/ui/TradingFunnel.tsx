import { Card } from "@shared/ui/Card";
import { EmptyNote } from "@shared/ui/EmptyNote";
import { SourceLine } from "@shared/ui/SourceLine";

import type { TradingCases, TradingExecutionRow } from "../api/tradingQueries";
import { admissionRefusalRows, funnelReasonRows, funnelSteps } from "../model/tradingCases";
import { admissionReasonLabel, admissionStatusLabel, ledgerSentence } from "../model/tradingLabels";

/**
 * ③ The 24 h funnel: seven counts from frame to closed position, and why the lane stopped (#604 T4).
 *
 * This block replaces the four-tile Case card. That card answered "how many Cases" and left the two ends
 * of the chain unstated — how many frames admission ever looked at, and what the venue did with the
 * Signals the policy emitted — so an operator could not tell a quiet market from a stalled lane. The first
 * four counts are `/api/trading/cases` aggregates and the last three are counted from the execution rows
 * on the desk below; the strip marks where the lane's answer ends and the venue's begins.
 *
 * The reason rows are the second repair. The page printed `smart_money_ratio_below_or_equal_floor`, so the
 * seven Chinese rule names in `POLICY_RULE_ZH` could never reach a reader; they go through
 * `policyReasonLabel` now. They are **not** links: `reason_counts_24h` and `admission_counts_24h` publish
 * counts, not identities, and a NO_TRADE Case has no execution row to carry its id — so the desk holds no
 * `case_id` for any of these rows and will not invent one. The Signal rows in the ledger below remain the
 * drawer's way in.
 */
export function TradingFunnel({
  cases,
  executions,
  failed,
  pending,
}: {
  cases: TradingCases | undefined;
  executions: readonly TradingExecutionRow[];
  failed: boolean;
  pending: boolean;
}) {
  const steps = funnelSteps(cases, executions);
  const reasons = funnelReasonRows(cases);
  const admissions = admissionRefusalRows(cases);
  return (
    <Card
      data-block="funnel"
      flush
      hint="帧与成案是服务端 24h 聚合；受理 / 成交 / 平仓数的是下方账本的行"
      title="24h 漏斗"
    >
      <div aria-label="24h 漏斗" className="trading-funnel" role="group">
        {steps.map((step) => (
          <span className="trading-funnel-step" data-side={step.side} key={step.key}>
            <small>{step.label}</small>
            <b>{step.value}</b>
          </span>
        ))}
      </div>

      {reasons.length || admissions.length ? (
        <div className="trading-count-list">
          {reasons.map((row) => (
            <span className="trading-count-row" key={row.key}>
              <small>不交易 · {row.label}</small>
              <b>{row.count}</b>
            </span>
          ))}
          {admissions.map((row) => (
            <span className="trading-count-row" key={row.key}>
              <small>
                {admissionStatusLabel(row.status)}
                {row.reason ? ` · ${admissionReasonLabel(row.reason)}` : ""}
              </small>
              <b>{row.count}</b>
            </span>
          ))}
        </div>
      ) : (
        <EmptyNote className="trading-empty-note">
          {ledgerSentence({ failed, pending, subject: "Case" })}
        </EmptyNote>
      )}
      <SourceLine path="GET /api/trading/cases → admission_counts_24h · state_counts_24h · reason_counts_24h" />
    </Card>
  );
}
