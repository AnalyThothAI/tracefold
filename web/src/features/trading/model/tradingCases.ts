import type {
  TradingCase,
  TradingCases,
  TradingExecutionRow,
  TradingPolicyCheck,
} from "../api/tradingQueries";

import { CASE_STATE_ZH, bpsPercent, policyReasonLabel } from "./tradingLabels";

/**
 * The Case/Decision surface's whole model.
 *
 * It derives nothing about a Case. Every threshold is frozen onto the Case, so the page renders what the
 * server already decided: a Case frozen last week must not be re-measured against a floor edited
 * yesterday. The funnel below counts, which is a different act — every figure in it is a server aggregate
 * or a count of the ledger rows the desk is already showing, never a re-judgement of one.
 */
type FunnelStep = {
  key: string;
  label: string;
  value: number;
  /** The last three steps are the venue's answer, not the lane's; the strip marks where that changes. */
  side: "lane" | "venue";
};

/**
 * 帧 → 成案 → 不交易 → 发出 → 受理 → 成交 → 平仓, over the same rolling 24 h window.
 *
 * The first four are `/api/trading/cases` aggregates: `admission_counts_24h` is how many frames admission
 * looked at at all (#604 T3), and `state_counts_24h` is what the policy did with the ones that became
 * Cases. The last three are counted from the execution rows already on the desk, because that response is
 * the only place the venue's answer per entry exists. A step is a count, never a rate: the desk states
 * seven numbers and leaves the division to the reader.
 */
export function funnelSteps(
  cases: TradingCases | undefined,
  executions: readonly TradingExecutionRow[],
): FunnelStep[] {
  const states = cases?.state_counts_24h ?? {};
  const frames = (cases?.admission_counts_24h ?? []).reduce((sum, item) => sum + item.count, 0);
  const decided = Object.values(states).reduce((sum, value) => sum + value, 0);
  const accepted = executions.filter((row) => !REFUSED_STAGES.has(row.stage)).length;
  const filled = executions.filter((row) => row.fill_quantity != null).length;
  const closed = executions.filter((row) => row.stage === "closed").length;
  return [
    { key: "frames", label: "帧", value: frames, side: "lane" },
    { key: "cases", label: "成案", value: decided, side: "lane" },
    { key: "no_trade", label: "不交易", value: states.NO_TRADE ?? 0, side: "lane" },
    { key: "emitted", label: "发出", value: states.SIGNAL_EMITTED ?? 0, side: "lane" },
    { key: "accepted", label: "受理", value: accepted, side: "venue" },
    { key: "filled", label: "成交", value: filled, side: "venue" },
    { key: "closed", label: "平仓", value: closed, side: "venue" },
  ];
}

/** The two stages that mean the entry never reached the venue, as `tracefold/trading/stages.py` derives them. */
const REFUSED_STAGES = new Set(["rejected", "expired"]);

type FunnelReasonRow = { count: number; key: string; label: string };

/**
 * The three rules that refused most Cases in the window, in the language the desk is read in.
 *
 * The page printed the raw keys, so the seven translations in `POLICY_RULE_ZH` could not reach a reader at
 * all. `undecided` is not a rule and is dropped; a key with no translation still renders as itself,
 * because that string is what an operator greps.
 */
export function funnelReasonRows(data: TradingCases | undefined): FunnelReasonRow[] {
  return Object.entries(data?.reason_counts_24h ?? {})
    .filter(([reason]) => reason !== "undecided")
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .slice(0, 3)
    .map(([reason, count]) => ({ count, key: reason, label: policyReasonLabel(reason) }));
}

/** `准入拒绝 · 持仓价值低于地板 · 61` — why frames never became Cases, largest first. */
export function admissionRefusalRows(
  data: TradingCases | undefined,
): Array<{ count: number; key: string; reason: string | null; status: string }> {
  return (data?.admission_counts_24h ?? [])
    .filter((item) => item.status !== "CASE_CREATED")
    .slice(0, 3)
    .map((item) => ({
      count: item.count,
      key: `${item.status}:${item.reason ?? ""}`,
      reason: item.reason,
      status: item.status,
    }));
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
