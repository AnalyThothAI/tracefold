export { TradingPage } from "./ui/TradingPage";
export { TradingCaseBadge } from "./ui/TradingCaseBadge";
export { TradingSymbolSection } from "./ui/TradingSymbolSection";
export {
  useTradingCasesWithToken,
  useTradingGateSourceWithToken,
  useTradingGateWithToken,
  useTradingObservationsWithToken,
  useTradingSignalsWithToken,
  useTradingStatusWithToken,
  type TradingCase,
  type TradingCases,
  type TradingExecutionObservation,
  type TradingExecutionObservations,
  type TradingGate,
  type TradingGateConfig,
  type TradingGateDecision,
  type TradingPolicyCheck,
  type TradingRuntimeCounts,
  type TradingSignal,
  type TradingSignals,
  type TradingStatus,
} from "./api/tradingQueries";
/*
 * The Signal lane's own words, read by the surfaces News composes over the same ledger. Exported as the
 * lane's vocabulary rather than copied, so a state, rule, or policy cannot be named one thing on the
 * execution workbench and another on Alpha 判定.
 */
export {
  CASE_STATE_ZH,
  GATE_STATUS_ZH,
  bpsPercent,
  caseClock,
  gateReasonLabel,
  policyLabel,
  policyReasonLabel,
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
