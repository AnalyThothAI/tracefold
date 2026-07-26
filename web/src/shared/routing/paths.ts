import type {
  RadarSortMode,
  ScopeKey,
  TokenPostRange,
  TokenPostSortMode,
  WindowKey,
} from "@lib/types";

import { compactSearch } from "./searchParams";

export function livePath(
  params: {
    window?: WindowKey;
    scope?: ScopeKey;
    handles?: string;
    sort?: RadarSortMode;
  } = {},
): string {
  const search = compactSearch(params);
  return "/" + (search ? `?${search}` : "");
}

export function searchPath({
  q,
  window = "24h",
  scope = "all",
}: {
  q: string;
  window?: WindowKey;
  scope?: ScopeKey;
}): string {
  const search = compactSearch({ q, window, scope });
  return "/search" + (search ? `?${search}` : "");
}

export function watchlistPath(params: { handle?: string | null } = {}): string {
  const search = compactSearch(params);
  return "/watchlist" + (search ? `?${search}` : "");
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

export function stocksPath({
  window = "1h",
  scope = "all",
}: {
  window?: WindowKey;
  scope?: ScopeKey;
} = {}): string {
  const search = compactSearch({ window, scope });
  return "/stocks" + (search ? `?${search}` : "");
}

export function tokenTargetPath({
  targetType,
  targetId,
  window = "24h",
  scope = "all",
  postRange,
  postSort,
}: {
  targetType: string;
  targetId: string;
  window?: WindowKey;
  scope?: ScopeKey;
  postRange?: TokenPostRange;
  postSort?: TokenPostSortMode;
}): string {
  const search = compactSearch({
    window: window === "24h" ? undefined : window,
    scope: scope === "all" ? undefined : scope,
    postRange,
    postSort,
  });
  return `/token/${encodeURIComponent(targetType)}/${encodeURIComponent(targetId)}${
    search ? `?${search}` : ""
  }`;
}
