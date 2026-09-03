/*
 * The Signal lane's public surface: exactly what another feature imports, and nothing else.
 *
 * Everything News composes over is here — the desk page itself, the two badges it renders on News
 * surfaces, and the admission ledger's read plus the helpers that turn one of its rows into a cell. The
 * lane's Chinese vocabularies and Case model are no longer re-exported: after #528 PR-2 nothing outside
 * this feature reads them, and a barrel that lists them invites the copy this boundary exists to prevent.
 */
export { TradingPage } from "./ui/TradingPage";
export { TradingCaseBadge } from "./ui/TradingCaseBadge";
export { TradingSymbolSection } from "./ui/TradingSymbolSection";
export {
  useTradingGateWithToken,
  type TradingGate,
  type TradingGateDecision,
} from "./api/tradingQueries";
export {
  tradingAdmissionCellCopy,
  tradingAdmissionTraceEntries,
  tradingGateByEventId,
  type TradingAdmissionLookup,
} from "./model/tradingAdmission";
