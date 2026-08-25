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

export function newsReviewPath(): string {
  return "/news/review";
}
