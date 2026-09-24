import type { TradingCase, TradingExecutionRow, TradingPolicyCheck } from "../api/tradingQueries";

import { CASE_STATE_ZH, bpsPercent, policyReasonLabel } from "./tradingLabels";

/**
 * The Case/Decision surface's whole model.
 *
 * It derives nothing about a Case. Every threshold is frozen onto the Case, so the page renders what the
 * server already decided: a Case frozen last week must not be re-measured against a floor edited
 * yesterday. The funnel below counts, which is a different act — every figure in it is a server aggregate
 * or a count of the ledger rows the desk is already showing, never a re-judgement of one.
 */
/**
 * How far the venue took the entries in the window, counted once for the two blocks that state it.
 *
 * `rejected` and `expired` are the stages that mean the entry never opened anything, as
 * `tracefold/trading/stages.py` derives them — refused before submission, refused by the venue itself, or
 * out of time. `ordered` onward is an entry the venue accepted; `pending` is neither yet.
 */
export function entrySplit(executions: readonly TradingExecutionRow[]): {
  accepted: number;
  closed: number;
  filled: number;
  refused: number;
} {
  const refused = executions.filter((row) => row.stage === "rejected" || row.stage === "expired");
  return {
    accepted: executions.filter((row) =>
      ["ordered", "filled", "protected", "closed"].includes(row.stage),
    ).length,
    closed: executions.filter((row) => row.stage === "closed").length,
    filled: executions.filter((row) => row.fill_quantity != null).length,
    refused: refused.length,
  };
}

function caseStateLabel(item: TradingCase): string {
  return CASE_STATE_ZH[item.state] ?? item.state;
}

/** The one sentence a Case's terminal answer deserves, in the vocabulary that decided it. */
export function caseVerdict(item: TradingCase): string {
  if (item.trigger_id) {
    const action = item.analysis_action ?? item.analysis_decision?.action;
    if (action === "TRADE") {
      const publication = item.analysis_publish_status ?? item.analysis_decision?.publish_status;
      const label =
        publication === "shadow"
          ? "影子判断"
          : publication === "blocked"
            ? "发布被阻断"
            : publication === "superseded"
              ? "原判断已失效"
              : publication === "published"
                ? "Signal 已发布"
                : "交易判断";
      const side = item.analysis_side ?? item.analysis_decision?.decision?.side;
      return `${label}${side === "short" ? " · 做空" : side === "long" ? " · 做多" : ""}`;
    }
    if (action === "NO_TRADE") return "主动不交易";
    if (action === "WATCH") return "等待条件复核";
    return caseStateLabel(item);
  }
  if (item.state === "SIGNAL_EMITTED") return "做多 · 已发出入场信号";
  if (item.state === "NO_TRADE") return `不交易 · ${policyReasonLabel(item.policy_reason)}`;
  if (item.state === "BLOCKED") return `无法安全判定 · ${policyReasonLabel(item.policy_reason)}`;
  return `${caseStateLabel(item)}${item.policy_reason ? ` · ${policyReasonLabel(item.policy_reason)}` : ""}`;
}

type CaseCheckRow = TradingPolicyCheck & { threshold_label: string; measured_label: string };

/**
 * The frozen checks, with the two basis-point fields rendered as percentages.
 *
 * Only fields the Case itself carries. Nothing here consults the running configuration, which is the
 * whole reason a Case decided under a 6% ceiling no longer reads as a conflict under a 10% one.
 */
export function caseChecks(item: TradingCase): CaseCheckRow[] {
  return (item.policy_checks ?? []).map((check) => ({
    ...check,
    threshold_label: asPercent(check.check) ? bpsPercent(Number(check.threshold)) : check.threshold,
    measured_label:
      check.measured == null
        ? "未测量"
        : asPercent(check.check)
          ? bpsPercent(Number(check.measured))
          : check.measured,
  }));
}

function asPercent(check: string): boolean {
  return check.endsWith("_bps");
}
