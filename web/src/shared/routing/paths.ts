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
 * 资本判定 (#256). The capital lane's reading of the deterministic OI frames — a different question from
 * `/news/oi`, which audits whether the frames themselves parsed and cleared the push gates.
 */
export function newsLeveragePath(): string {
  return "/news/leverage";
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
 * The Binance USD-M Demo execution workbench. It is read-only and has no runtime switch.
 */
export function tradingPath(): string {
  return "/trading";
}
