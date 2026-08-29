import type { TradingGate, TradingGateDecision } from "../api/tradingQueries";

import { GATE_STATUS_ZH, caseClock, gateReasonLabel } from "./tradingLabels";

/**
 * What the Source/Admission surface knows about one OI frame (#331).
 *
 * Only the admission aggregate. The lookup this replaces also carried a Case's state and an Intent's
 * execution lifecycle, which put three durable objects behind one cell — and made "未成案" mean "no
 * Case", "no Intent" and "we could not ask" at once. A Case link is a link; following it is how a reader
 * gets a decision.
 */
export type TradingAdmissionLookup = {
  /** Whether the admission batch covered the window, as opposed to being truncated. */
  complete: boolean;
  decision: TradingGateDecision | undefined;
  eventId: string;
  /**
   * Whether the ledger answered at all. Without it an absent decision is ambiguous between "the lane has
   * no row for this frame" and "we could not ask", and only the first is a statement about the frame.
   */
  answered: boolean;
  loadFailed: boolean;
};

export type TradingAdmissionCellCopy = {
  primary: string;
  secondary?: string;
  title?: string;
  /** Present only when the frame authored a Case; the cell renders it as a link, never as a state. */
  caseId?: string;
};

/**
 * Index the window's admission answers by the Event id the server published from the source key.
 *
 * `event_id` is recovered only when `primary_source_key` round-trips as `oi:{event_id}:{metric_version}`,
 * which is exactly the population an Event-first surface can ask about.
 */
export function tradingGateByEventId(
  gate: TradingGate | undefined,
): Map<string, TradingGateDecision> {
  const result = new Map<string, TradingGateDecision>();
  for (const decision of gate?.decisions ?? []) {
    if (decision.event_id) result.set(decision.event_id, decision);
  }
  return result;
}

/** The one cell the frame table draws: what admission answered, and the Case it authored if it did. */
export function tradingAdmissionCellCopy(lookup: TradingAdmissionLookup): TradingAdmissionCellCopy {
  if (lookup.loadFailed) {
    return { primary: "读取失败", title: "准入台账本轮不可用；不能据此断言这一帧没有结论。" };
  }
  if (!lookup.answered) {
    return { primary: "读取中", title: "正在读取准入台账。" };
  }
  const decision = lookup.decision;
  if (!decision) {
    return {
      primary: "未评估",
      title: lookup.complete
        ? "该帧在本窗口内没有任何准入记录：资本通道从未在任何 gate 版本下评估过它。"
        : "准入台账已截断，这一帧可能不在本页；不能据此断言它未被评估。",
    };
  }
  const status = decision.gate_status ?? "";
  const key =
    decision.gate_stage && decision.gate_reason
      ? `${decision.gate_stage}:${decision.gate_reason}`
      : "";
  const label = GATE_STATUS_ZH[status] ?? status;
  const title = key
    ? `${label} · ${gateReasonLabel(key)} · ${caseClock(decision.gate_last_evaluated_at_ms)}`
    : label;
  return {
    primary: label,
    secondary: key || undefined,
    title,
    caseId: decision.case_id ?? undefined,
  };
}

/** The frame's admission row, field by field, for the expanded trace. */
export function tradingAdmissionTraceEntries(
  lookup: TradingAdmissionLookup,
): Array<[string, string]> {
  if (lookup.loadFailed) return [["准入台账", "本轮读取失败"]];
  const decision = lookup.decision;
  if (!decision) {
    return [["准入台账", lookup.answered ? "本窗口无记录" : "读取中"]];
  }
  const key =
    decision.gate_stage && decision.gate_reason
      ? `${decision.gate_stage}:${decision.gate_reason}`
      : "";
  const entries: Array<[string, string]> = [
    ["来源标识", decision.source_key],
    ["结论", GATE_STATUS_ZH[decision.gate_status ?? ""] ?? decision.gate_status ?? "—"],
    ["规则", key ? `${gateReasonLabel(key)}（${key}）` : "—"],
    ["可重试", decision.gate_retryable ? "是" : "否"],
    ["资本权限", decision.research_only ? "仅研究，无资本权限" : "live"],
    ["首次评估", caseClock(decision.gate_first_evaluated_at_ms)],
    ["最近评估", caseClock(decision.gate_last_evaluated_at_ms)],
    ["评估次数", String(decision.gate_attempt_count ?? "—")],
    [
      "准入版本",
      `${decision.gate_version ?? "—"} · ${(decision.gate_config_digest ?? "").slice(0, 12)}`,
    ],
    ["案例", decision.case_id ?? "未开案"],
  ];
  const evidence = decision.gate_evidence;
  if (evidence) {
    for (const [label, value] of [
      ["场所", evidence.venue],
      ["持仓额", evidence.oi_value_usd == null ? "" : String(evidence.oi_value_usd)],
      ["窗口名次", evidence.rank_in_window == null ? "" : String(evidence.rank_in_window)],
      ["地板", evidence.floor == null ? "" : String(evidence.floor)],
      ["帧龄 ms", evidence.age_ms == null ? "" : String(evidence.age_ms)],
    ] as const) {
      if (value) entries.push([label, value]);
    }
  }
  return entries;
}
