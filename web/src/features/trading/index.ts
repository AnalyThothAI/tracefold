export { TradingPage } from "./ui/TradingPage";
export { TradingCaseBadge } from "./ui/TradingCaseBadge";
export { TradingSymbolSection } from "./ui/TradingSymbolSection";
export {
  useTradingOrdersWithToken,
  type TradingCase,
  type TradingOrder,
  type TradingOrders,
} from "./api/tradingQueries";
export {
  policyRuleZh,
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
  ORDER_STATE_NOTE,
  REGIME_ZH,
  STRATEGY_ZH,
  holdCeiling,
  isActiveOrder,
  stopVerified,
  strategyCaseLabel,
} from "./model/tradingLabels";
