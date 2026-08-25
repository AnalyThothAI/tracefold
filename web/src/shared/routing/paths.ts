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

export function newsReviewPath(): string {
  return "/news/review";
}
