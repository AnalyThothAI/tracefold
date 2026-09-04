import type { TradingCase, TradingCases, TradingPolicyCheck } from "../api/tradingQueries";

import { CASE_STATE_ZH, bpsPercent, policyReasonLabel } from "./tradingLabels";

/**
 * The Case/Decision surface's whole model.
 *
 * It derives nothing. Every threshold is frozen onto the Case, so the page renders what the server
 * already decided: a Case frozen last week must not be re-measured against a floor edited yesterday.
 */
export type CaseFigure = {
  key: string;
  label: string;
  value: string;
  tone: "plain" | "accent" | "caution";
};

/**
 * The desk's one Case card, every figure a durable count the server aggregated (#331).
 *
 * `0 成案` is a legitimate output of the current rules and is presented as one. What it must never be
 * presented as is "no data": the admission figures beside it say how many facts the lane actually saw.
 */
export function caseFigures(data: TradingCases | undefined): CaseFigure[] {
  const states = data?.state_counts_24h ?? {};
  const total = Object.values(states).reduce((sum, value) => sum + value, 0);
  return [
    { key: "cases", label: "24h 成案", value: String(total), tone: "plain" },
    {
      key: "emitted",
      label: "已发出 Signal",
      value: String(states.SIGNAL_EMITTED ?? 0),
      tone: (states.SIGNAL_EMITTED ?? 0) > 0 ? "accent" : "plain",
    },
    { key: "no_trade", label: "不交易", value: String(states.NO_TRADE ?? 0), tone: "plain" },
    {
      key: "blocked",
      label: "无法判定",
      value: String(states.BLOCKED ?? 0),
      tone: (states.BLOCKED ?? 0) > 0 ? "caution" : "plain",
    },
  ];
}

/** `smart_money_ratio_below_or_equal_floor · 12` — the durable reason distribution, largest first. */
export function caseReasonRows(data: TradingCases | undefined): Array<[string, number]> {
  return Object.entries(data?.reason_counts_24h ?? {})
    .filter(([reason]) => reason !== "undecided")
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .slice(0, 8);
}

function caseStateLabel(item: TradingCase): string {
  return CASE_STATE_ZH[item.state] ?? item.state;
}

/** The one sentence a Case's terminal answer deserves, in the vocabulary that decided it. */
export function caseVerdict(item: TradingCase): string {
  if (item.state === "SIGNAL_EMITTED") return "LONG · 已发出 Signal";
  if (item.state === "NO_TRADE") return `不交易 · ${policyReasonLabel(item.policy_reason)}`;
  if (item.state === "BLOCKED") return `无法安全判定 · ${policyReasonLabel(item.policy_reason)}`;
  return `${caseStateLabel(item)}${item.policy_reason ? ` · ${policyReasonLabel(item.policy_reason)}` : ""}`;
}

export type CaseCheckRow = TradingPolicyCheck & { threshold_label: string; measured_label: string };

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
