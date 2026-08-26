export { TradingPage } from "./ui/TradingPage";
export { TradingCaseBadge } from "./ui/TradingCaseBadge";
export { TradingSymbolSection } from "./ui/TradingSymbolSection";
export {
  useTradingGateWithToken,
  useTradingOrdersWithToken,
  useTradingStatusWithToken,
  type TradingCase,
  type TradingCounts,
  type TradingGate,
  type TradingGateConfig,
  type TradingGateDecision,
  type TradingOrder,
  type TradingOrders,
  type TradingStatus,
  type TradingStrategyConfig,
} from "./api/tradingQueries";
export {
  policyRuleZh,
  tradingGateByEventId,
  tradingLedgerEntries,
  tradingOiCellCopy,
  tradingOiLedgerByEventId,
  tradingOiTraceEntries,
  type TradingOiLedgerEntry,
  type TradingOiLookup,
} from "./model/tradingOiLedger";
/*
 * The capital lane's own words, read by the surfaces News composes over the same ledger (#256). Exported as
 * the lane's vocabulary rather than copied, so a state, a regime or a strategy cannot be named one thing on
 * 模拟仓 and another on 杠杆异动.
 */
export {
  CASE_STATE_ZH,
  GATE_STATUS_ZH,
  ORDER_STATE_NOTE,
  REGIME_ZH,
  STRATEGY_ZH,
  gateReasonLabel,
  holdCeiling,
  isActiveOrder,
  stopVerified,
  strategyCaseLabel,
} from "./model/tradingLabels";
