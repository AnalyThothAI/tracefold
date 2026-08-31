import { useLocation } from "react-router-dom";

/**
 * Where a secondary surface came from, so its way back is the way in (#256).
 *
 * The token page is reached from four different places — the Event feed, the Event drawer and detail, the
 * OI audit and the Alpha ledger — and a back link that always said 事件流 was wrong three times out of
 * four: it named a page the reader had never been on and, from the feed, dropped the filters they arrived
 * with. React Router already carries per-navigation state; this is the whole contract for it.
 *
 * `search` rides along so a filtered feed comes back filtered. It is the link's own state, not URL state:
 * a shared token-page URL has no referrer and correctly falls back to the feed.
 */
export type RouteReferrer = { label: string; to: string };

const REFERRER_LABELS: Array<[RegExp, string]> = [
  [/^\/news\/events\//, "事件详情"],
  [/^\/news\/status$/, "流水线状态"],
  [/^\/news\/oi$/, "OI 来源与准入审计"],
  [/^\/news\/alpha$/, "Alpha 判定"],
  [/^\/news$/, "事件流"],
  [/^\/trading$/, "Alpha / Execution"],
];

const FEED_REFERRER: RouteReferrer = { label: "事件流", to: "/news" };

/** The current route as a referrer, for a link that is about to leave it. */
export function useRouteReferrer(): RouteReferrer {
  const { pathname, search } = useLocation();
  const label = REFERRER_LABELS.find(([pattern]) => pattern.test(pathname))?.[1];
  // A route with no name of its own is not offered as a destination: a back link reading `/news/symbols/BTC`
  // would be worse than one reading 事件流.
  if (!label) return FEED_REFERRER;
  return { label, to: `${pathname}${search}` };
}

/**
 * The referrer a navigation carried, or the feed.
 *
 * Validated rather than trusted: `location.state` is whatever the last `navigate` put there, including
 * across a browser restore, and a back link is a real navigation. Only same-origin absolute paths that this
 * console actually serves are accepted.
 */
export function routeReferrerFromState(state: unknown): RouteReferrer {
  if (state === null || typeof state !== "object") return FEED_REFERRER;
  const candidate = state as Partial<RouteReferrer>;
  if (typeof candidate.label !== "string" || typeof candidate.to !== "string") return FEED_REFERRER;
  const pathname = candidate.to.split("?")[0];
  if (!REFERRER_LABELS.some(([pattern]) => pattern.test(pathname))) return FEED_REFERRER;
  return { label: candidate.label, to: candidate.to };
}
