export { TradingPage } from "./ui/TradingPage";
export { TradingCaseBadge } from "./ui/TradingCaseBadge";
export { TradingSymbolSection } from "./ui/TradingSymbolSection";
export {
  useTradingCasesWithToken,
  useTradingGateSourceWithToken,
  useTradingGateWithToken,
  useTradingIntentsWithToken,
  useTradingStatusWithToken,
  type TradingCase,
  type TradingCases,
  type TradingGate,
  type TradingGateConfig,
  type TradingGateDecision,
  type TradingIntent,
  type TradingIntents,
  type TradingPolicyCheck,
  type TradingRuntimeCounts,
  type TradingStatus,
} from "./api/tradingQueries";
/*
 * The capital lane's own words, read by the surfaces News composes over the same ledger (#256). Exported
 * as the lane's vocabulary rather than copied, so a state, a rule or a policy cannot be named one thing
 * on the execution workbench and another on 资本判定.
 */
export {
  CASE_STATE_ZH,
  GATE_STATUS_ZH,
  INTENT_STATE_NOTE,
  bpsPercent,
  caseClock,
  gateReasonLabel,
  isActiveIntent,
  policyLabel,
  policyReasonLabel,
  stopVerified,
} from "./model/tradingLabels";
export {
  CASE_TABS,
  caseChecks,
  caseFigures,
  caseReasonRows,
  caseStateLabel,
  caseTabCount,
  caseVerdict,
  casesForTab,
  defaultCaseTab,
  parseCaseTab,
  type CaseCheckRow,
  type CaseFigure,
  type CaseTab,
} from "./model/tradingCases";
export {
  tradingAdmissionCellCopy,
  tradingAdmissionTraceEntries,
  tradingGateByEventId,
  type TradingAdmissionLookup,
} from "./model/tradingAdmission";
