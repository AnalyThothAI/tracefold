import type { TokenPostRange, WindowKey } from "@lib/types";

import { compactSearch } from "./searchParams";

export function searchPath({ q, window = "24h" }: { q: string; window?: WindowKey }): string {
  const search = compactSearch({ q, window });
  return "/search" + (search ? `?${search}` : "");
}

export function newsPath(): string {
  return "/news";
}

export function macroPath(): string {
  return "/macro";
}

export function newsStoryPath(storyId: string): string {
  return `/news/stories/${encodeURIComponent(storyId)}`;
}

export function newsBriefPath(): string {
  return "/news/brief";
}

export function newsStatusPath(): string {
  return "/news/status";
}

export function newsSourcesPath(): string {
  return "/news/sources";
}

export function stocksPath({
  window = "1h",
}: {
  window?: WindowKey;
} = {}): string {
  const search = compactSearch({ window });
  return "/stocks" + (search ? `?${search}` : "");
}

export function tokenTargetPath({
  targetType,
  targetId,
  window = "24h",
  postRange,
  focus,
  triggerEventId,
}: {
  targetType: string;
  targetId: string;
  window?: WindowKey;
  postRange?: TokenPostRange;
  focus?: "trigger";
  triggerEventId?: string;
}): string {
  const search = compactSearch({
    window: window === "24h" ? undefined : window,
    postRange,
    focus,
    trigger_event_id: triggerEventId,
  });
  return `/token/${encodeURIComponent(targetType)}/${encodeURIComponent(targetId)}${
    search ? `?${search}` : ""
  }`;
}
