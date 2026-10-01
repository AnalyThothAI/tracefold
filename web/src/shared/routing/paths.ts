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
 * 链上钱包 (#572 PR-3). The wallet tape's own surface: its roster, its ingest position, the cards its
 * rules opened and what those cards were worth an hour and four hours later. A single wallet observation
 * is still read on the market detail page — this one answers what the tape is doing, not what one card said.
 */
export function newsWalletsPath(): string {
  return "/news/wallets";
}

/**
 * The token page (#207 PR-W1) accepts catalogue base symbols, including unlisted tickers. A source may
 * also name a contract address: keep that visible without offering a catalogue URL it cannot answer.
 */
export function canOpenNewsSymbol(base: string): boolean {
  return /^[A-Z0-9._-]{1,24}$/.test(base);
}

export function newsSymbolPath(base: string): string {
  return `/news/symbols/${encodeURIComponent(base)}`;
}

/**
 * The read-only Alpha and execution-observation workbench. It has no Runtime switch.
 */
export function tradingPath(): string {
  return "/trading";
}
