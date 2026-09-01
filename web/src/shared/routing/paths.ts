export function newsPath(): string {
  return "/news";
}

export function newsEventPath(eventId: string): string {
  return `/news/events/${encodeURIComponent(eventId)}`;
}

export function newsStatusPath(): string {
  return "/news/status";
}

export function newsOiPath(): string {
  return "/news/oi";
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

/**
 * One Case's frozen evidence on the Trading workbench. It was `/news/alpha?case=` until #460 removed
 * that page; the Case card there opens the linked Case and scrolls no differently from a bare visit.
 */
export function tradingCasePath(caseId: string): string {
  return `/trading?case=${encodeURIComponent(caseId)}`;
}
