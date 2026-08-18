export function newsPath(): string {
  return "/news";
}

export function macroPath(): string {
  return "/macro";
}

export function newsEventPath(eventId: string): string {
  return `/news/events/${encodeURIComponent(eventId)}`;
}

export function newsStatusPath(): string {
  return "/news/status";
}
