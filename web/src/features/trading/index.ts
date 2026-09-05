/*
 * The Signal lane's public surface: exactly what another feature imports, and nothing else.
 *
 * Only the desk page now. The Case badge, the admission ledger's read and the helpers that turned one
 * of its rows into a cell left with their single consumer: `/news/oi`'s frame table joined each
 * admission row to the OI Event on the same line, and #553 PR-1 removed that join with the Events
 * themselves. A market observation carries no `event_id`, so nothing outside this feature can ask the
 * ledger anything, and the Event detail's badge could only ever have rendered null. The lane's Chinese
 * vocabularies and Case model are not re-exported either: after #528 PR-2 nothing outside this feature
 * reads them, and a barrel that lists them invites the copy this boundary exists to prevent.
 */
export { TradingPage } from "./ui/TradingPage";
