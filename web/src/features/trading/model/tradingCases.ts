import type { TradingCase, TradingCases, TradingPolicyCheck } from "../api/tradingQueries";

import { CASE_STATE_ZH, bpsPercent, policyReasonLabel } from "./tradingLabels";

/**
 * The Case/Decision surface's whole model (#331).
 *
 * It is small because it derives nothing. The 988-line module this replaces rebuilt Capital in
 * the browser: it re-ran threshold comparisons against `/api/trading/status`, inferred a phase from an
 * Intent's execution state, and printed 冲突 on rows whose Case had passed — because it was measuring a
 * Case frozen last week with a floor edited yesterday. Every threshold is now frozen onto the Case, so
 * the page's job is to render what the server already decided.
 */
export type CaseTab = "all" | "emitted" | "no_trade" | "blocked";

export const CASE_TABS: Record<CaseTab, { label: string; states: readonly string[] }> = {
  all: { label: "全部", states: [] },
  emitted: { label: "已发出 Signal", states: ["SIGNAL_EMITTED"] },
  no_trade: { label: "不交易", states: ["NO_TRADE"] },
  blocked: { label: "无法判定", states: ["BLOCKED", "POLICY_REJECTED", "ORDER_PREPARED"] },
};

const TAB_KEYS = Object.keys(CASE_TABS) as CaseTab[];

export function parseCaseTab(value: string | null): CaseTab | null {
  return value && TAB_KEYS.includes(value as CaseTab) ? (value as CaseTab) : null;
}

export function casesForTab(cases: readonly TradingCase[], tab: CaseTab): TradingCase[] {
  const states = CASE_TABS[tab].states;
  return states.length ? cases.filter((item) => states.includes(item.state)) : [...cases];
}

/**
 * The first tab that has rows, or `all` when nothing does.
 *
 * A fixed default of `已形成意图` on a lane that legitimately emits nothing greets every reader with an
 * empty page and teaches them the console is broken. `全部` leads the strip and therefore wins this
 * search whenever anything has rows at all — which is the intended answer, because it shows the
 * refusals beside the emissions. The search below it is what keeps that true if the strip ever gains a
 * filtered tab above `全部`. A URL that names a tab always wins: a shared link points at what its
 * author was looking at.
 */
export function defaultCaseTab(cases: readonly TradingCase[], requested: CaseTab | null): CaseTab {
  if (requested) return requested;
  return TAB_KEYS.find((tab) => casesForTab(cases, tab).length > 0) ?? "all";
}

export function caseTabCount(cases: readonly TradingCase[], tab: CaseTab): number {
  return casesForTab(cases, tab).length;
}

export type CaseFigure = {
  key: string;
  label: string;
  value: string;
  tone: "plain" | "accent" | "caution";
};

/**
 * The title row's figures, every one of them a durable count the server aggregated (#331).
 *
 * `0 成案` is a legitimate output of the current rules and is presented as one. What it must never be
 * presented as is "no data": the Source figure beside it says how many facts the lane actually saw.
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

export function caseStateLabel(item: TradingCase): string {
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
