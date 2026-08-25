/**
 * What the route shell may read from Trading.
 *
 * The sidebar shows the lane's mode as a badge, and that is all the frame needs. Exporting the page from
 * here would make the trading route's code eager on every route, exactly as it would for News.
 */
export { useTradingStatusWithToken } from "./api/tradingQueries";
export type { TradingStatus } from "./api/tradingQueries";
