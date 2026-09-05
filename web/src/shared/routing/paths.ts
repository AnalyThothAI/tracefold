export function newsPath(): string {
  return "/news";
}

export function newsEventPath(eventId: string): string {
  return `/news/events/${encodeURIComponent(eventId)}`;
}

export function newsStatusPath(): string {
  return "/news/status";
}

/**
 * 市场事实 (#553 PR-1). Market observations are facts read from `/api/news/market`, not Events, so this is
 * the only surface that reads them and there is no `/news/oi` behind it.
 */
export function newsMarketPath(): string {
  return "/news/market";
}

/**
 * The token page (#207 PR-W1). Every `base_symbol` on the console routes here, including one the universe
 * has never listed — the endpoint answers `known: false` rather than 404, so a struck-through chip is a
 * link like any other.
 */
export function newsSymbolPath(base: string): string {
  return `/news/symbols/${encodeURIComponent(base)}`;
}

/**
 * The read-only Alpha and execution-observation workbench. It has no Runtime switch.
 */
export function tradingPath(): string {
  return "/trading";
}
