import type { TradingCase, TradingOrder, TradingOrders } from "../api/tradingQueries";

import { CASE_STATE_ZH, REGIME_ZH } from "./tradingLabels";

export type TradingOiLedgerEntry =
  | { kind: "order"; value: TradingOrder }
  | { kind: "case"; value: TradingCase };

export type TradingOiLookup = {
  complete: boolean;
  entry: TradingOiLedgerEntry | undefined;
  eventId: string;
  loadFailed: boolean;
  loaded: boolean;
};

export type TradingOiCellCopy = {
  primary: string;
  secondary?: string;
  title?: string;
};

/** Index only the Event identities the server explicitly published from deterministic OI source keys. */
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

/** Capital-lane vocabulary for the compact OI table cell; News supplies no policy interpretation. */
export function tradingOiCellCopy(lookup: TradingOiLookup): TradingOiCellCopy {
  if (lookup.loadFailed) return { primary: "账本不可用" };
  if (!lookup.loaded) return { primary: "读取中" };
  if (!lookup.entry) {
    return lookup.complete
      ? { primary: "未成案" }
      : { primary: "未确认", title: "交易账本批次已截断" };
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
        ? `拒 · ${policyReasonLabel(value.policy_reason)}`
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
    return [
      ["event_id", lookup.eventId],
      ["case", lookup.complete ? "未成案" : "未确认（账本批次已截断）"],
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

function policyReasonLabel(value: string | null | undefined): string {
  if (!value) return "交易地板";
  if (value.startsWith("regime_no_entry:deleveraging")) return "减仓无可跟";
  if (value.startsWith("regime_no_entry:")) return "市场状态不允许";
  return (
    {
      move_above_band_chasing: "追高（走势带外）",
      move_below_band: "帧前走势未达带",
      no_price_fail_closed: "帧前价格缺失",
      oi_context_missing: "OI 上下文缺失",
      oi_direction_unknown: "OI 方向未知",
      oi_value_below_floor: "OI 规模未达地板",
      regime_no_side: "市场状态无方向",
      short_disabled_long_only: "方向非只多",
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
