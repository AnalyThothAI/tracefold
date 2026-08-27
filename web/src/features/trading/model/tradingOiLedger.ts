import type {
  TradingCase,
  TradingGate,
  TradingGateDecision,
  TradingOrder,
  TradingOrders,
} from "../api/tradingQueries";

import { CASE_STATE_ZH, GATE_STATUS_ZH, REGIME_ZH, gateReasonLabel } from "./tradingLabels";

export type TradingOiLedgerEntry =
  | { kind: "order"; value: TradingOrder }
  | { kind: "case"; value: TradingCase };

export type TradingOiLookup = {
  complete: boolean;
  entry: TradingOiLedgerEntry | undefined;
  eventId: string;
  /**
   * The admission answer for this frame (#269). Its own read, and its own absence: a frame with a
   * ledger row and no case has a *named reason*, which is a different fact from a frame the lane has
   * never evaluated, and both are different from a frame whose case exists.
   */
  gate: TradingGateDecision | undefined;
  /**
   * Whether the admission ledger answered at all — a separate read from the order batch, and a
   * separately failable one. Without it an absent `gate` is ambiguous between "the lane has no row for
   * this frame" and "we could not ask", and only the first of those is a statement about the frame.
   */
  gateAnswered: boolean;
  /** Whether the admission batch covered the window, as opposed to the order batch's `complete`. */
  gateComplete: boolean;
  loadFailed: boolean;
  loaded: boolean;
};

export type TradingOiCellCopy = {
  primary: string;
  secondary?: string;
  title?: string;
};

/**
 * Every case and order the ledger batch holds, keyed by `case_id`.
 *
 * `case_id` is the ledger's own identity and is always present. `event_id` is not: the server recovers it
 * only when `primary_source_key` round-trips as `oi:{event_id}:{metric_version}`, so a news- or
 * liquidation-triggered case publishes `null` by design (a model-lane source key is a content hash and is
 * not joinable). Indexing the lane by `event_id` therefore drops every case that did not come from the
 * deterministic OI trigger — which is most of them.
 */
export function tradingLedgerEntries(
  trading: TradingOrders | undefined,
): Map<string, TradingOiLedgerEntry> {
  const result = new Map<string, TradingOiLedgerEntry>();
  for (const value of trading?.cases_without_orders ?? []) {
    result.set(value.case_id, { kind: "case", value });
  }
  // An order supersedes the case row it was authored from; the API returns the two sets disjoint, and
  // keying both by `case_id` keeps them that way if it ever stops.
  for (const value of trading?.orders ?? []) {
    result.set(value.case_id, { kind: "order", value });
  }
  return result;
}

/**
 * Index only the Event identities the server explicitly published from deterministic OI source keys.
 *
 * This is the *frame-join* index and is correct only for a surface that starts from Events — the OI audit
 * table asking "did this frame become a case". Anything enumerating the lane itself wants
 * `tradingLedgerEntries`.
 */
export function tradingOiLedgerByEventId(
  trading: TradingOrders | undefined,
): Map<string, TradingOiLedgerEntry> {
  const result = new Map<string, TradingOiLedgerEntry>();
  for (const value of trading?.cases_without_orders ?? []) {
    if (value.event_id) result.set(value.event_id, { kind: "case", value });
  }
  for (const value of trading?.orders ?? []) {
    if (value.event_id) result.set(value.event_id, { kind: "order", value });
  }
  return result;
}

/**
 * The admission answers in the window, keyed by the Event id the server recovered from each source key.
 *
 * A decision whose source key is not the deterministic OI contract publishes `event_id: null` and is not
 * in this index — it is one of the lane's answers and the distributions count it, but no frame on this
 * page is the frame it is about.
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

/**
 * Capital-lane vocabulary for the compact OI table cell; News supplies no policy interpretation.
 *
 * Four answers, and the reason the whole column existed is that three of them used to be one (#269):
 *
 *   拒/过期/待重试 · <named gate reason>   admission refused the frame, and says on which rule
 *   未评估                                 the lane holds no row for it under any gate version
 *   拒 · <policy rule> | <case state>      a case exists and the strategy decided it
 *   多/空 · <order state>                  an order exists
 *
 * The gate is consulted only when there is no case: a frame that produced one has an admission row too
 * (`freeze:case_created`), and showing 已开案 where the case's own state belongs would hide the decision.
 */
export function tradingOiCellCopy(lookup: TradingOiLookup): TradingOiCellCopy {
  if (lookup.loadFailed) return { primary: "账本不可用" };
  if (!lookup.loaded) return { primary: "读取中" };
  if (!lookup.entry) {
    const gate = lookup.gate;
    if (!lookup.gateAnswered) {
      // The admission ledger is its own read and can fail on its own. 未评估 would be this console
      // asserting something about the frame on the strength of a request that never came back.
      return { primary: "未确认", title: "准入台账未读到" };
    }
    if (gate?.gate_status && gate.gate_status !== "CASE_CREATED") {
      const key = `${gate.gate_stage}:${gate.gate_reason}`;
      return {
        primary: `${GATE_STATUS_ZH[gate.gate_status] ?? gate.gate_status} · ${gateReasonLabel(key)}`,
        secondary: gate.gate_stage ?? undefined,
        title: `${key} · ${gate.gate_version ?? ""}`.trim(),
      };
    }
    if (gate?.gate_status === "CASE_CREATED") {
      // The gate opened a case and this batch does not hold it — a page boundary, not a refusal.
      return { primary: "已开案", title: gate.case_id ?? "案例不在本批账本内" };
    }
    // 未评估 is a claim about this frame and only the *admission* batch can support it. Reading the
    // order batch's completeness here would have blamed a truncated order page for a gap in a ledger
    // that had answered in full, and vice versa.
    return lookup.gateComplete
      ? { primary: "未评估", title: "资本通道尚未在任何 gate 版本下评估过这一帧" }
      : { primary: "未确认", title: "准入台账批次已截断" };
  }
  if (lookup.entry.kind === "order") {
    const value = lookup.entry.value;
    const realized = value.realized_bps == null ? "" : ` ${signedBps(value.realized_bps)}`;
    return {
      primary: `${value.side === "buy" ? "多" : "空"} · ${value.state}${realized}`,
      secondary: regimeLabel(value.regime),
      title: `${value.case_id} · ${value.order_id}`,
    };
  }
  const value = lookup.entry.value;
  return {
    primary:
      value.state === "POLICY_REJECTED"
        ? `拒 · ${policyRuleZh(value.policy_reason)}`
        : (CASE_STATE_ZH[value.state] ?? value.state),
    secondary: regimeLabel(value.regime),
    title: value.policy_reason ?? value.state,
  };
}

/** The exact case/order ledger fields shown in the expanded Trading trace. */
export function tradingOiTraceEntries(lookup: TradingOiLookup): Array<[string, string]> {
  if (lookup.loadFailed) return [["ledger", "读取失败"]];
  if (!lookup.loaded) return [["ledger", "读取中"]];
  if (!lookup.entry) {
    const gate = lookup.gate;
    if (!gate) {
      return [
        ["event_id", lookup.eventId],
        // Two batches, two completeness answers, and neither may speak for the other: the case line is
        // the order batch's, the gate line is the admission ledger's.
        ["case", lookup.complete ? "未成案" : "未确认（交易账本批次已截断）"],
        ["gate", gateAbsenceNote(lookup)],
        ["join", "只按已发布 event_id；不按 symbol/time 猜"],
      ];
    }
    // The durable row, verbatim. `attempt_count` is how many times the scanner *saw* this source, not
    // how many times it retried: a terminal row is re-read every turn inside the overlap window.
    return [
      ["event_id", lookup.eventId],
      ["gate_status", gate.gate_status ?? "—"],
      ["gate_stage", gate.gate_stage ?? "—"],
      ["gate_reason", gate.gate_reason ?? "—"],
      ["gate_retryable", String(gate.gate_retryable ?? false)],
      ["gate_version", gate.gate_version ?? "—"],
      ["gate_config_digest", (gate.gate_config_digest ?? "").slice(0, 12) || "—"],
      ["attempt_count", String(gate.gate_attempt_count ?? 0)],
      ...gateEvidenceEntries(gate),
      ["join", "只按已发布 event_id；不按 symbol/time 猜"],
    ];
  }
  if (lookup.entry.kind === "order") {
    const value = lookup.entry.value;
    return [
      ["event_id", value.event_id ?? lookup.eventId],
      ["case_id", value.case_id],
      ["case_state", value.case_state],
      ["strategy", value.strategy_id],
      ["regime", value.regime ? `${regimeLabel(value.regime)} (${value.regime})` : "—"],
      ["policy_decision", value.policy_decision ?? "—"],
      ["policy_reason", value.policy_reason ?? "—"],
      ["order_id", value.order_id],
      ["order_state", value.state],
      ["side", value.side],
    ];
  }
  const value = lookup.entry.value;
  return [
    ["event_id", value.event_id ?? lookup.eventId],
    ["case_id", value.case_id],
    ["case_state", value.state],
    ["strategy", value.strategy_id],
    ["regime", value.regime ? `${regimeLabel(value.regime)} (${value.regime})` : "—"],
    ["policy_decision", value.policy_decision ?? "—"],
    ["policy_reason", value.policy_reason ?? "—"],
  ];
}

function regimeLabel(value: string | null | undefined): string | undefined {
  return value ? (REGIME_ZH[value] ?? value) : undefined;
}

/** Why there is no admission row to show — three different absences, never collapsed into one. */
function gateAbsenceNote(lookup: TradingOiLookup): string {
  if (!lookup.gateAnswered) return "准入台账这一轮没有读到，无法回答为什么没有案例";
  if (!lookup.gateComplete) return "准入台账批次已截断，本帧可能在未列出的部分";
  return "本帧在任何 gate 版本下都没有落库的准入判定";
}

/**
 * The threshold the rule compared against, when the rule published one.
 *
 * Only the two comparison keys, not the whole evidence document: the four measurements beside them are
 * already the frame's own columns in this table, and repeating them under the trace would be the same
 * numbers twice. What the row does not otherwise say is what the gate measured them *against*.
 */
function gateEvidenceEntries(gate: TradingGateDecision): Array<[string, string]> {
  const evidence = gate.gate_evidence;
  const entries: Array<[string, string]> = [];
  if (evidence?.floor != null) entries.push(["evidence.floor", String(evidence.floor)]);
  if (evidence?.limit != null) entries.push(["evidence.limit", String(evidence.limit)]);
  if (evidence?.max_age_ms != null)
    entries.push(["evidence.max_age_ms", String(evidence.max_age_ms)]);
  if (evidence?.enabled?.length) entries.push(["evidence.enabled", evidence.enabled.join(", ")]);
  return entries;
}

/**
 * The named rule a case stopped on, in Chinese.
 *
 * Public because two surfaces read it now (#256): the OI audit's 交易判定 cell and 杠杆异动's own sentence.
 * A second copy of this map is how one rule ends up with two meanings on two pages.
 */
export function policyRuleZh(value: string | null | undefined): string {
  if (!value) return "交易地板";
  if (value.startsWith("regime_no_entry:deleveraging")) return "减仓无可跟";
  if (value.startsWith("regime_no_entry:")) return "市场状态不允许";
  return (
    {
      move_above_band_chasing: "追高（走势带外）",
      // `candidate.py` writes these two directly; before #256 they only leaked into a compact table cell,
      // and 杠杆异动 splices the same string into a Chinese sentence.
      order_blocked: "已成判断，但下单被拦（日内上限 / 同标的已有仓 / 黑名单 / 规模拒绝）",
      strategy_permission: "策略权限不允许",
      move_below_band: "帧前走势未达带",
      no_price_fail_closed: "帧前价格缺失",
      not_oi_rise: "持仓不是上升",
      oi_context_missing: "OI 上下文缺失",
      oi_direction_unknown: "OI 方向未知",
      oi_value_below_floor: "OI 规模未达地板",
      price_direction_not_confirmed: "价格方向未确认",
      regime_no_side: "市场状态无方向",
      short_disabled_long_only: "方向非只多",
      /*
       * The smart-money family (#264/#265/#273). It reached this map late and until then every compact
       * cell on the console printed `smart_money_ratio_below_or_equal_floor` verbatim — which is most of
       * a production day's rows, so the two surfaces that use short labels were effectively untranslated.
       * `tradingReadout` writes the long sentence with the measurement in it; this is the same rule at
       * label length, and it is the only other place the vocabulary is allowed to live.
       */
      smart_money_momentum_long: "四项条件全过",
      smart_money_oi_change_below_floor: "持仓增幅未达地板",
      smart_money_profit_not_positive: "大户盈利不为正",
      smart_money_ratio_below_or_equal_floor: "大户占比未过地板",
      source_window_mismatch: "窗口口径不符",
      strategy_permission_shadow_or_paper: "策略权限不允许",
      whale_long_profit_below_floor: "鲸盈利未达地板",
      // Older fixtures/ledgers used the shorter spelling; keep its meaning without changing the raw trace.
      whale_profit_below_floor: "鲸盈利未达地板",
    }[value] ?? value
  );
}

function signedBps(value: number): string {
  return `${value < 0 ? "−" : value > 0 ? "+" : ""}${Math.abs(value)}bps`;
}
